"""流水线中各 Agent 之间传递的数据结构。

所有 Agent 只通过这里定义的结构通信，不直接读写彼此的内部状态。
这样做的目的有两个：一是编排层可以对每一步的输入输出做 schema 校验，
二是评测脚本可以把中间数据原样落盘，作为提交材料里的"协同决策中间数据"。
"""

from dataclasses import dataclass, field, asdict
from typing import Any


VERDICT_SUPPORTED = "supported"      # 有知识库依据
VERDICT_UNSUPPORTED = "unsupported"  # 检索不到依据
VERDICT_CONTRADICTED = "contradicted"  # 与知识库冲突

# 辩论环节的共识等级。写进最终资源，是"知识溯源"可展示的一部分。
CONSENSUS_BOTH = "both"        # 两位专家独立提出了同一条，置信最高
CONSENSUS_SINGLE = "single"    # 仅一方提出，证据成立但缺少交叉印证
CONSENSUS_ARBITRATED = "arbitrated"  # 双方冲突，由裁判依据知识库仲裁得出

RESOURCE_KINDS = ("lecture", "sop", "quiz")


@dataclass
class Chunk:
    """知识库切片。

    verified 是本项目里最要紧的一个字段。
    整套幻觉检测的逻辑是"断言必须被知识库支撑"，
    而**知识库本身如果是错的，审核闸只会把错误认证为正确**。
    幻觉率为零的真实含义是"生成内容与知识库一致"，不是"内容正确"。

    所以每条切片必须标明是否经过核实。评测据此把指标拆成
    "基于已核实切片"与"基于未核实切片"两部分分别报告 ——
    只有前者才是能拿出去讲的数。
    """
    id: str
    kp: str
    title: str
    source: str
    text: str
    verified: bool = False
    source_note: str = ""
    # 来源：手工录入还是从原始文档摄入。摄入的可回溯到文件与位置。
    origin: str = "manual"
    # 资料失效或尚未完成来源核验时，可保留原始切片供内部复核，
    # 但不得让它进入在线服务、离线快照或答辩展示。
    demo_eligible: bool = True

    @property
    def publicly_verified(self) -> bool:
        """Return the verification state that is safe to expose in a Demo.

        ``verified`` preserves the historical human-review record on the raw
        knowledge-base item. A source that has since become unavailable must
        not be presented as currently verifiable, even if that raw record is
        still retained for internal review.
        """
        return self.verified and self.demo_eligible


@dataclass
class Mastery:
    """单个知识点的掌握度。"""
    kp: str
    name: str
    level: int          # 知识点固有难度 1-5
    asked: int          # 前测中考查了几题
    correct: int
    score: float        # 0.0-1.0 掌握概率（BKT 后验）
    status: str         # blind / weak / ok / strong
    confidence: float = 0.0          # 估计可信度，只由观测条数决定
    curve: list[float] = field(default_factory=list)  # 掌握度逐题演进，供前端画曲线
    # 证据强度。点估计单独看会得出荒唐结论：答对 2/2 点估计 0.896、
    # 判"掌握牢固"，可纯蒙达到 2/2 的概率有 6.2%。
    # 下面三项让"我瞎蒙也能考成这样吗"这个问题有明确答案。
    lower: float = 0.0               # 掌握概率的区间下界，对外只能声称到这
    upper: float = 1.0
    luck: float = 1.0                # 完全不会的人蒙到该成绩的概率
    evidence: str = "untested"       # 见 bkt.evidence_state
    evidence_why: str = ""

    def is_gap(self) -> bool:
        return self.status in ("blind", "weak")


@dataclass
class Diagnosis:
    """学情诊断结果。"""
    profile_id: str
    mastery: list[Mastery]
    gaps: list[str]              # 盲区知识点 id，已按学习顺序排好
    overall: float
    entry_level: int             # 建议的资源起始难度 1-5
    prior: float = 0.0           # 由背景画像推出的初始掌握概率 p_L0
    low_confidence: list[str] = field(default_factory=list)  # 证据不足的知识点
    narrative: str = ""          # 大模型生成的自然语言诊断，仅用于展示

    def by_kp(self, kp: str) -> Mastery | None:
        for m in self.mastery:
            if m.kp == kp:
                return m
        return None


@dataclass
class Claim:
    """一条事实性断言。生成 Agent 的最小输出单元。

    text 必须是可独立判真伪的陈述句，source_id 指向支撑它的知识库切片。
    没有 source_id 的断言一律视为幻觉，在审核环节被拦掉。
    """
    text: str
    source_id: str | None = None
    verdict: str | None = None
    audit_note: str = ""
    evidence_score: float = 0.0
    consensus: str | None = None     # both / single / arbitrated
    proposed_by: list[str] = field(default_factory=list)  # 哪几位专家提出过
    rival: str = ""                  # 被仲裁掉的对立说法，答辩时可展示


@dataclass
class Resource:
    """生成的一份学习资源。"""
    kind: str                    # lecture / sop / quiz
    kp: str
    title: str
    difficulty: int              # 1-5，用于和学习者掌握度做适配度计算
    # primary  首轮按适配窗口生成，纳入"画像-资源难度适配准确率"统计
    # remedial 反馈触发的降维补救内容，**故意**低于窗口，不纳入适配统计
    # advanced 反馈触发的进阶挑战，**故意**高于窗口，同样不纳入
    # 不区分这三类会得到一个假性偏低的适配率：补救内容本来就该更简单，
    # 把它算作"不适配"等于在惩罚系统做对的事。
    variant: str = "primary"
    claims: list[Claim] = field(default_factory=list)
    body: str = ""
    items: list[dict] = field(default_factory=list)   # quiz 专用
    dropped: list[Claim] = field(default_factory=list)  # 被审核拦下的断言

    def sources(self) -> list[str]:
        return sorted({c.source_id for c in self.claims if c.source_id})


@dataclass
class Event:
    """编排层的一步。前端时间线和评测中间数据都读这个。"""
    seq: int
    state: str
    agent: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    ms: int = 0


@dataclass
class Session:
    """一次完整的学习会话。"""
    profile_id: str
    diagnosis: Diagnosis | None = None
    path: list[str] = field(default_factory=list)      # 学习路径，知识点 id 序列
    resources: list[Resource] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    debates: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

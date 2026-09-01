"""核实 Agent：用外部证据校验知识库切片。

────────────────────────────────────────────────────────────────────
先说这个模块最容易走歪的地方
────────────────────────────────────────────────────────────────────

直觉做法是：把切片丢给大模型问一句"这条对不对"，模型说对就标成已核实。

**这么做比现在不核实还糟。**

知识库本来就是大模型写出来的。再让大模型来判它对不对，等于让同一个来源
自己给自己背书 —— 模型倾向于认可符合自己先验的说法，而那些说法恰恰是它
自己生成的。结果是把"未核实"洗成了"已核实"，标签变了，可信度一点没变。
错误从此戴上一顶合规的帽子，比裸奔更难被发现。

所以本模块的铁律：

  **模型的判断本身永远不能成为核实依据。核实依据只能是外部证据。**

模型在这里只干两件事：把切片改写成适合检索的查询串；读取检索回来的外部
资料，判断它是否支持该切片。第二件事仍然是判断，但它判断的对象是**别人写的
资料**，不是自己的记忆 —— 这是关键区别。没有检索到资料，就没有判断的余地，
直接判"查不到"，绝不允许回落到模型的记忆。

────────────────────────────────────────────────────────────────────
状态取值：机器核实与人工核实必须是两个词
────────────────────────────────────────────────────────────────────

  unverified      未核实（默认）
  machine_checked 机器核实通过：找到了外部证据且多来源一致
  disputed        存疑：证据不足、来源单一，或多来源互相矛盾
  refuted         被推翻：找到明确矛盾的外部证据
  verified        **人工确认**。机器永远不写这个值

最后一条是整个设计的支点。机器核实过的条目仍然要人过一遍才升级为 verified，
因为检索到的网页本身也可能是错的 —— 核实 FANUC 报警码时就遇到过某服务商
网站把 SRVO-001 说成"伺服放大器过载"，与多份官方资料矛盾。
机器能做的是**把证据摆到人面前并排好优先级**，不是替人拍板。

────────────────────────────────────────────────────────────────────
多来源要求
────────────────────────────────────────────────────────────────────

单一来源不足以定案，这不是理论顾虑，是上面那个真实案例。
因此判定 machine_checked 需要**至少两个互相独立的域名**给出一致结论。
只有一个来源支持时判 disputed，附上证据交人复核。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import config
from core.llm import parse_json

# ---- 状态 ----
ST_UNVERIFIED = "unverified"
ST_MACHINE = "machine_checked"
ST_DISPUTED = "disputed"
ST_REFUTED = "refuted"
ST_VERIFIED = "verified"          # 仅人工可写

MACHINE_WRITABLE = {ST_MACHINE, ST_DISPUTED, ST_REFUTED, ST_UNVERIFIED}

# 判定一致所需的独立域名数。见模块顶部说明。
MIN_INDEPENDENT_SOURCES = 2
# 证据摘录上限。只留定位用的最短片段，不整段搬运他人内容。
SNIPPET_MAX = 60

_QUERY_SYS = (
    "你把一条技术陈述改写成 1 到 3 个用于网络检索的查询串，目的是找到能印证或"
    "推翻它的外部资料。查询串要短、含关键术语与型号，不要包含判断性措辞。"
    '只输出 JSON：{"queries":["...","..."]}'
)

_JUDGE_SYS = (
    "你判断一段外部资料是否支持给定的技术陈述。"
    "只依据提供的资料判断，**不要使用你自己的知识补充或纠正**；"
    "资料没提到就判 unknown，这是正确答案而不是失败。"
    '只输出 JSON：{"verdict":"support|refute|unknown","reason":"简短理由",'
    '"quote":"资料中最相关的一小段，不超过30字"}'
)


@dataclass
class Evidence:
    url: str
    domain: str
    verdict: str                 # support / refute / unknown
    reason: str = ""
    quote: str = ""

    def __post_init__(self):
        if len(self.quote) > SNIPPET_MAX:
            self.quote = self.quote[:SNIPPET_MAX] + "…"


@dataclass
class CheckResult:
    chunk_id: str
    status: str
    support_domains: list[str] = field(default_factory=list)
    refute_domains: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    note: str = ""
    queries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


class SearchBackend:
    """检索后端接口。

    刻意做成接口而不是写死某一家：容器内没有网络，学生机器上的可用检索源
    也各不相同（模型自带的联网工具、独立搜索 API、企业内网检索）。
    实现这个接口即可接入。

    search(query) 返回若干条 {"url", "title", "text"}。
    text 是页面正文摘录，交给模型判断用。
    """

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        raise NotImplementedError


class NullSearch(SearchBackend):
    """无检索源时的占位实现。

    返回空列表，于是所有条目都判为 disputed（查不到证据），
    **绝不会**回落到"让模型凭记忆判断"。没有证据就是没有证据。
    """

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        return []


class FactCheckAgent:
    name = "核实Agent"

    def __init__(self, llm, search: SearchBackend | None = None):
        self.llm = llm
        self.search = search or NullSearch()
        self.stats = {"checked": 0, "machine": 0, "disputed": 0,
                      "refuted": 0, "no_evidence": 0, "llm_errors": 0}

    # ---- 第一步：把切片改写成查询 ----

    def make_queries(self, chunk) -> list[str]:
        raw = ""
        try:
            raw = self.llm.run(
                task="factcheck_query", system=_QUERY_SYS,
                user=json.dumps({"陈述": chunk.text[:300], "标题": chunk.title},
                                ensure_ascii=False),
                context={"chunk": {"title": chunk.title, "text": chunk.text}},
                json_mode=True,
            )
        except Exception:                                   # noqa: BLE001
            self.stats["llm_errors"] += 1
        qs = [q for q in (parse_json(raw).get("queries") or []) if isinstance(q, str)]
        if not qs:
            # 兜底：用标题加上正文里的型号与数值，够检索用
            codes = re.findall(r"[A-Z]{2,}[-–]?\d{2,}", chunk.text)
            nums = re.findall(r"\d+(?:\.\d+)?\s*(?:毫米|米|小时|年|度|层)", chunk.text)
            qs = [" ".join([chunk.title] + codes[:2] + nums[:2]).strip()]
        return qs[:3]

    # ---- 第二步：逐条外部资料判断 ----

    def judge(self, statement: str, doc: dict) -> Evidence:
        url = doc.get("url", "")
        raw = ""
        try:
            raw = self.llm.run(
                task="factcheck_judge", system=_JUDGE_SYS,
                user=json.dumps({"陈述": statement[:300],
                                 "资料": (doc.get("text") or "")[:1200]},
                                ensure_ascii=False),
                context={"statement": statement, "doc": doc},
                json_mode=True,
            )
        except Exception:                                   # noqa: BLE001
            self.stats["llm_errors"] += 1
        d = parse_json(raw)
        v = d.get("verdict")
        if v not in ("support", "refute", "unknown"):
            v = "unknown"
        return Evidence(url=url, domain=domain_of(url), verdict=v,
                        reason=(d.get("reason") or "")[:80],
                        quote=(d.get("quote") or ""))

    # ---- 第三步：汇总定状态 ----

    def check(self, chunk) -> CheckResult:
        self.stats["checked"] += 1
        queries = self.make_queries(chunk)

        docs, seen = [], set()
        for q in queries:
            for d in self.search.search(q, top_k=5):
                u = d.get("url", "")
                if u and u not in seen:
                    seen.add(u)
                    docs.append(d)

        if not docs:
            self.stats["no_evidence"] += 1
            self.stats["disputed"] += 1
            return CheckResult(
                chunk_id=chunk.id, status=ST_DISPUTED, queries=queries,
                note="未检索到任何外部资料。**不回落到模型记忆**，判为存疑待人工核实")

        ev = [self.judge(chunk.text, d) for d in docs]
        sup = sorted({e.domain for e in ev if e.verdict == "support" and e.domain})
        ref = sorted({e.domain for e in ev if e.verdict == "refute" and e.domain})

        if ref:
            # 只要有明确矛盾就压到最严的一档，哪怕支持方更多。
            # 一条被外部资料直接反驳的内容，绝不能因为"多数支持"就放行。
            status = ST_REFUTED
            note = f"检索到 {len(ref)} 个来源给出矛盾结论，须人工裁定后重写"
            self.stats["refuted"] += 1
        elif len(sup) >= MIN_INDEPENDENT_SOURCES:
            status = ST_MACHINE
            note = (f"{len(sup)} 个独立来源一致支持。"
                    "仍需人工确认后才可升级为 verified")
            self.stats["machine"] += 1
        elif sup:
            status = ST_DISPUTED
            note = ("仅 1 个来源支持，不满足多来源要求。"
                    "单一来源不足以定案 —— 核实报警码时曾遇到单一网站与多份官方资料矛盾")
            self.stats["disputed"] += 1
        else:
            status = ST_DISPUTED
            note = "检索到资料但均未涉及该陈述，判为存疑"
            self.stats["disputed"] += 1

        return CheckResult(chunk_id=chunk.id, status=status, support_domains=sup,
                           refute_domains=ref, evidence=ev, note=note,
                           queries=queries)

    # ---- 批量 ----

    def check_all(self, chunks, only_unverified: bool = True) -> list[CheckResult]:
        out = []
        for c in chunks:
            if only_unverified and getattr(c, "verified", False):
                continue
            out.append(self.check(c))
        return out


def apply_results(kb_path, results: list[CheckResult]) -> dict:
    """把核实结果写回知识库。

    **只写机器可写的状态**，permanent 的 verified 字段一律不碰 ——
    那是人工确认才能给的标签。机器核实通过的条目写进 check_status，
    人工复核之后再由人把 verified 置为 true。

    这个分离不是形式主义：一旦机器能写 verified，
    整条溯源链就退化成"模型说它查过了"，与不核实无异。
    """
    import pathlib
    p = pathlib.Path(kb_path)
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    idx = {r.chunk_id: r for r in results}
    changed = 0
    for c in lines:
        r = idx.get(c["id"])
        if r is None:
            continue
        assert r.status in MACHINE_WRITABLE, f"机器不得写入状态 {r.status}"
        c["check_status"] = r.status
        c["check_note"] = r.note
        c["check_sources"] = r.support_domains
        if r.refute_domains:
            c["check_conflicts"] = r.refute_domains
        changed += 1
    p.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in lines) + "\n",
                 encoding="utf-8")
    return {"updated": changed, "total": len(lines)}

"""能力图谱：把知识点掌握度聚合成雷达图的六个维度。

界面上逐条列出十五个知识点的掌握概率，信息是全的，但没人读得进去 ——
一屏数字，看不出这个人强在哪、弱在哪。雷达图解决的是这个问题：
六条轴，一眼看出能力形状。

但雷达图有个众所周知的毛病：它很好看，因而特别容易骗人。
本模块用三条设计压住这一点。

**一、轴不能太少也不能太碎。**
不直接用知识点标签做轴 —— 现有十三个标签里有八个只覆盖一个知识点，
画出来大部分轴由单点决定，一道题的对错就能让整条轴塌掉，
看着像能力图谱，其实是噪声图。归并到六维、每维至少三个知识点才有平均的意义。

**二、必须画两层。**
外层是点估计，内层是区间下界。两层之间的间隙就是"我们还不确定的部分"。
只画点估计的雷达图会让四道题答对三道看起来和真正掌握一模一样 ——
而这两件事的证据强度差着数量级。

**三、未测的维度不能画成零。**
零和"没测过"在雷达图上长得一样，但含义相反：前者是"确定不会"，
后者是"不知道"。未测部分单独用虚线标出，并在图例里写明。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import config


@dataclass
class DimScore:
    id: str
    name: str
    desc: str
    score: float            # 点估计，0-1
    lower: float            # 区间下界，对外只能声称到这
    tested: int             # 该维度下已实测的知识点数
    total: int              # 该维度下的知识点总数
    worst_kp: str = ""      # 最薄弱的知识点，用于给出改进方向
    worst_name: str = ""
    inferred: int = 0       # 由前置推断得出的知识点数

    @property
    def coverage(self) -> float:
        return self.tested / self.total if self.total else 0.0

    @property
    def evidence_gap(self) -> float:
        """点估计与下界之间的间隙。间隙越大，说明证据越薄。"""
        return round(self.score - self.lower, 3)


@dataclass
class AbilityProfile:
    dims: list[DimScore] = field(default_factory=list)
    overall: float = 0.0
    overall_lower: float = 0.0
    untested_dims: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dims": [{
                "id": d.id, "name": d.name, "desc": d.desc,
                "score": d.score, "lower": d.lower,
                "tested": d.tested, "total": d.total,
                "coverage": round(d.coverage, 3),
                "gap": d.evidence_gap,
                "worst_kp": d.worst_kp, "worst_name": d.worst_name,
                "inferred": d.inferred,
            } for d in self.dims],
            "overall": self.overall,
            "overall_lower": self.overall_lower,
            "untested_dims": self.untested_dims,
        }


def load_dimensions(path: Path | None = None) -> list[dict]:
    p = path or (config.DATA / "dimensions.json")
    return json.loads(p.read_text(encoding="utf-8"))["dimensions"]


def build(diagnosis, dims: list[dict] | None = None) -> AbilityProfile:
    """由诊断结果算出能力图谱。

    维度得分取该维度下各知识点的平均，**未测的知识点不计入平均**，
    而是单独记进覆盖率。把未测当零会让没考的维度显示成"完全不会"，
    这是雷达图最常见的误导方式。
    """
    dims = dims or load_dimensions()
    by_kp = {m.kp: m for m in diagnosis.mastery}
    prof = AbilityProfile()

    for d in dims:
        kps = [k for k in d["kps"] if k in by_kp]
        tested = [by_kp[k] for k in kps if by_kp[k].asked > 0]
        inferred = sum(1 for k in kps
                       if by_kp[k].asked == 0 and getattr(by_kp[k], "inferred_from", None))

        if tested:
            score = sum(m.score for m in tested) / len(tested)
            lower = sum(m.lower for m in tested) / len(tested)
            worst = min(tested, key=lambda m: m.score)
        else:
            # 全维度未测：得分留空而不是零。调用方据此画虚线。
            score, lower, worst = 0.0, 0.0, None

        prof.dims.append(DimScore(
            id=d["id"], name=d["name"], desc=d.get("desc", ""),
            score=round(score, 3), lower=round(lower, 3),
            tested=len(tested), total=len(kps),
            worst_kp=worst.kp if worst else "",
            worst_name=worst.name if worst else "",
            inferred=inferred,
        ))
        if not tested:
            prof.untested_dims.append(d["name"])

    live = [d for d in prof.dims if d.tested]
    if live:
        prof.overall = round(sum(d.score for d in live) / len(live), 3)
        prof.overall_lower = round(sum(d.lower for d in live) / len(live), 3)
    return prof


def narrate(prof: AbilityProfile) -> str:
    """给能力图谱配一段说明。规则拼句，不调模型 —— 数字必须可复算。"""
    live = [d for d in prof.dims if d.tested]
    if not live:
        return "本次测评未覆盖任何能力维度，无法给出能力图谱。"
    strong = max(live, key=lambda d: d.lower)
    weak = min(live, key=lambda d: d.score)
    parts = [f"六个维度中，「{strong.name}」证据最扎实，"
             f"下界 {strong.lower:.0%}；"
             f"「{weak.name}」最薄弱，主要短板在「{weak.worst_name}」。"]
    thin = [d for d in live if d.evidence_gap >= 0.35]
    if thin:
        parts.append(f"其中 {len(thin)} 个维度的点估计与下界相差超过三成五，"
                     "说明作答量不足、结论尚不稳固，图上表现为两层之间的间隙较宽。")
    if prof.untested_dims:
        parts.append("未覆盖的维度为：" + "、".join(prof.untested_dims)
                     + "，图上以虚线表示，不代表能力为零。")
    return "".join(parts)

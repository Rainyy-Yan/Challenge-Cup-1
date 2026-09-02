"""学情诊断 Agent。

设计上最要紧的一条：掌握度是算出来的，不是模型说出来的。

大模型在这里只干一件事，把算好的数字翻译成人话。原因是"画像-资源难度适配
准确率"这个指标要能复算，如果掌握度本身来自模型的自由发挥，同一份前测跑两遍
结果不一样，指标就没意义了，评委追问也答不上来。

估计器用的是 BKT（贝叶斯知识追踪），不是答对率。区别见 core/bkt.py 顶部注释，
一句话概括：BKT 显式建模了蒙对和失误，答对率把这两件事都算错。
"""

from __future__ import annotations

import json

import config
from core import bkt
from core.schema import Diagnosis, Mastery


class DiagnoseAgent:
    name = "学情诊断Agent"

    def __init__(self, llm, kp_path=config.KP_PATH, pretest_path=config.PRETEST_PATH,
                 *, items: list[dict] | None = None):
        self.llm = llm
        self.kps = json.loads(kp_path.read_text(encoding="utf-8"))["points"]
        self.items = (list(items) if items is not None else
                      json.loads(pretest_path.read_text(encoding="utf-8"))["items"])
        self._kp_index = {k["id"]: k for k in self.kps}
        self._item_index = {i["id"]: i for i in self.items}

    def _params(self) -> bkt.BKTParams:
        p = bkt.BKTParams(p_T=config.BKT_P_T, p_S=config.BKT_P_S, p_G=config.BKT_P_G)
        p.validate()
        return p

    def _status(self, score: float, asked: int) -> str:
        if asked == 0:
            return "blind" if config.UNTESTED_AS_GAP else "unknown"
        if score < config.MASTERY_BLIND:
            return "blind"
        if score < config.MASTERY_WEAK:
            return "weak"
        if score < config.MASTERY_OK:
            return "ok"
        return "strong"

    def _order_gaps(self, gaps: list[str]) -> list[str]:
        """按前置关系和难度排学习顺序。前置没补的知识点不会排在后面的前面。"""
        remaining = list(gaps)
        ordered: list[str] = []
        guard = 0
        while remaining and guard < 100:
            guard += 1
            layer = [
                kp for kp in remaining
                if all(p not in remaining for p in self._kp_index[kp]["prereq"])
            ]
            if not layer:                      # 出现环，按难度兜底排完
                layer = sorted(remaining, key=lambda k: self._kp_index[k]["level"])
            layer.sort(key=lambda k: (self._kp_index[k]["level"], k))
            ordered.extend(layer)
            remaining = [k for k in remaining if k not in layer]
        return ordered

    def run(self, profile: dict) -> Diagnosis:
        responses = profile.get("responses", {})
        params = self._params()
        bg = profile.get("background", {})
        p_L0 = bkt.prior_from_background(bg) if config.BKT_USE_PRIOR else params.p_L0

        # 按题号排序吸收，保证同一份作答的估计结果可复现
        seq: dict[str, list[bool]] = {k["id"]: [] for k in self.kps}
        for qid in sorted(responses):
            item = self._item_index.get(qid)
            if item is None:
                continue
            seq[item["kp"]].append(responses[qid] == item["answer"])

        mastery: list[Mastery] = []
        for kp in self.kps:
            obs = seq[kp["id"]]
            score = bkt.trace(obs, params, p_L0=p_L0) if obs else 0.0
            k, n = sum(obs), len(obs)
            lo, hi = bkt.mastery_interval(k, n, params) if n else (0.0, 1.0)
            lk = bkt.luck_probability(k, n) if n else 1.0
            ev, why = bkt.evidence_state(score, lo, lk, n,
                                         config.MASTERY_OK, config.MASTERY_BLIND)
            mastery.append(Mastery(
                kp=kp["id"], name=kp["name"], level=kp["level"],
                asked=n, correct=k,
                score=round(score, 3), status=self._status(score, n),
                confidence=bkt.confidence(n),
                curve=[round(x, 3) for x in bkt.trace_curve(obs, params, p_L0=p_L0)],
                lower=lo, upper=hi, luck=round(lk, 4),
                evidence=ev, evidence_why=why,
            ))

        gaps = self._order_gaps([m.kp for m in mastery if m.is_gap()])
        tested = [m for m in mastery if m.asked > 0]
        overall = round(sum(m.score for m in tested) / len(tested), 3) if tested else 0.0

        # 起始难度：整体掌握概率映射到 1-5，实操学时给一档加成
        hours = bg.get("hands_on_hours", 0)
        entry = 1 + int(overall * 3) + (1 if hours >= 60 else 0)
        entry = max(config.DIFFICULTY_MIN, min(config.DIFFICULTY_MAX, entry))

        diag = Diagnosis(
            profile_id=profile["id"], mastery=mastery, gaps=gaps,
            overall=overall, entry_level=entry, prior=round(p_L0, 3),
        )
        # 证据不足的知识点单独标出来，避免拿一道题的结论当结论
        diag.low_confidence = [m.kp for m in mastery
                               if m.asked > 0 and m.confidence < 0.75]
        diag.narrative = self.llm.run(
            task="diagnose_narrative",
            system="你是职业技能培训的学情分析师。只依据给定的数据描述现状，不要编造未提供的信息。",
            user=json.dumps({
                "background": bg, "overall": overall,
                "gaps": [self._kp_index[g]["name"] for g in gaps],
            }, ensure_ascii=False),
            context={
                "background": f"{bg.get('education', '')}{bg.get('major', '')}",
                "gap_names": [self._kp_index[g]["name"] for g in gaps],
                "strong_names": [m.name for m in mastery if m.status == "strong"],
            },
        )
        return diag

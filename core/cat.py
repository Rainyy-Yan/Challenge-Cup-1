"""自适应测评：选题、追问、停止。

原来的做法是把 47 道题一股脑发给学习者，答完再算。问题有三个：
  1. 太长。没人愿意为一次培训入门答 47 道题。
  2. 浪费。已经明显掌握的知识点还在反复问，明显是盲区的也在反复问，
     这些题带来的信息量接近零。
  3. 没有交互。这正是榜题里"打破静态资源单向输入局限"要解决的东西。

所以改成自适应：每次只发一道，根据已有作答实时挑下一道。

**选题依据**：BKT 当前估计下，这道题答对的预测概率是

    P(答对) = p_L·(1-p_S) + (1-p_L)·p_G

信息量在 P 接近 0.5 时最大 —— 有把握答对或答错的题，问了也没告诉你新东西。
所以从候选里挑 |P - 0.5| 最小的。这是项目反应理论里最大信息量选题的思路，
在 BKT 上的直接对应。

再叠一个不确定性权重：某个知识点问得越少，估计越不可信，越该优先问。
两者相乘作为最终优先级。

**追问**：当一次作答与当前估计明显冲突时，就地追加同知识点的一道题。
  已估为掌握却答错  → 分不清是失误还是真不会，追问一次
  已估为盲区却答对  → 分不清是真会还是蒙对，追问一次
这就是"动态追问"落到算法上的样子：追问不是随便加一道，是**为了消解歧义**
而加。BKT 的 p_S / p_G 正好把"失误"和"蒙对"显式建模了，所以歧义可以量化。

**停止**：所有知识点的估计可信度都达标，或达到题量上限。
不追求答完题库，追求把不确定性压到够用。
"""

from __future__ import annotations

import config
from core import bkt

# 追问触发阈值。冲突越强越该追问。
PROBE_HIGH = 0.65      # 掌握概率高于此值却答错 → 追问
PROBE_LOW = 0.35       # 掌握概率低于此值却答对 → 追问
MAX_PROBES_PER_KP = 1  # 同一知识点最多追问几次，防止在一个点上纠缠


def p_correct(p_L: float, params: bkt.BKTParams) -> float:
    """当前估计下答对的预测概率。"""
    return p_L * (1 - params.p_S) + (1 - p_L) * params.p_G


def item_information(p_L: float, params: bkt.BKTParams) -> float:
    """一道题的信息量代理值，越大越值得问。"""
    p = p_correct(p_L, params)
    return 1.0 - abs(p - 0.5) * 2.0     # P=0.5 时为 1，P=0 或 1 时为 0


class AdaptiveSession:
    """一次自适应测评的状态。

    刻意不依赖大模型：选题、追问、停止全是算术。这样同一串作答重放两遍
    结果完全一致，演示、录像、答辩复现都靠得住，离线也能跑。
    """

    def __init__(self, items: list[dict], kps: list[dict], background: dict,
                 max_items: int = 16, target_conf: float = 0.75,
                 examiner=None, max_generated: int | None = None):
        self.items = {i["id"]: i for i in items}
        self.by_kp: dict[str, list[dict]] = {}
        for i in items:
            self.by_kp.setdefault(i["kp"], []).append(i)
        for lst in self.by_kp.values():
            lst.sort(key=lambda x: x["level"])

        self.kps = {k["id"]: k for k in kps}
        self.params = bkt.BKTParams(p_T=config.BKT_P_T, p_S=config.BKT_P_S,
                                    p_G=config.BKT_P_G)
        self.params.validate()
        self.prior = (bkt.prior_from_background(background)
                      if config.BKT_USE_PRIOR else self.params.p_L0)

        self.max_items = max_items
        self.target_conf = target_conf
        self.state: dict[str, dict] = {
            k: {"p": self.prior, "n": 0, "correct": 0,
                "curve": [round(self.prior, 3)], "probes": 0}
            for k in self.kps
        }
        self.asked: list[str] = []
        self.log: list[dict] = []
        self.pending_probe: str | None = None      # 待追问的知识点

        # 命题 Agent。给了就能在题库缺档时现场出题；不给就退回纯题库模式。
        # 默认不给：纯题库模式是确定性的，一致性测试和评测都跑这一条路径。
        self.examiner = examiner
        self.max_generated = (config.ITEM_MAX_GENERATED if max_generated is None
                              else max_generated)
        self.generated: list[str] = []

    # ---- 选题 ----

    def _unasked(self, kp: str) -> list[dict]:
        return [i for i in self.by_kp.get(kp, []) if i["id"] not in self.asked]

    def next_item(self) -> dict | None:
        """挑下一道题。返回 None 表示可以结束了。"""
        if len(self.asked) >= self.max_items:
            return None

        # 追问优先：歧义没消解掉之前，先把这个知识点问清楚
        if self.pending_probe:
            kp = self.pending_probe
            cand = self._unasked(kp)
            self.pending_probe = None
            if cand:
                st = self.state[kp]
                pick = min(cand, key=lambda it: abs(
                    p_correct(st["p"], self.params) - 0.5))
                return {**pick, "_reason": "probe", "_kp_name": self.kps[kp]["name"]}

        if self._done():
            return None

        blocked = self.blocked_by_prereq()
        best, best_score = None, -1.0
        for kp, st in self.state.items():
            if kp in blocked and st["n"] == 0:
                continue                       # 由前置推断，不占题量
            cand = self._unasked(kp)
            if not cand:
                continue
            info = item_information(st["p"], self.params)

            # 首次触及该知识点给一次性加成，保证测评先摊开、再深入。
            # 加成只给 n==0，不随 n 递减 —— 早期版本用
            #   unc = 1 - confidence(n)
            # 做连续权重，结果 15 个知识点各问一道就用光了题量，
            # 每个点 n=1、可信度只有 0.4，等于哪个点都没问清楚。
            # 广度是有价值，但摊到 15 个点上就只剩广度了。
            first = 0.22 if st["n"] == 0 else 0.0

            # 前置未测的知识点降权，让测评顺着知识结构走
            pre = self.kps[kp].get("prereq", [])
            gate = 0.75 if any(self.state[p]["n"] == 0 for p in pre if p in self.state) else 1.0
            score = (info + first) * gate
            for it in cand:
                # 题目难度与当前估计水平接近的优先
                lvl_fit = 1.0 - abs(it["level"] - (1 + st["p"] * 4)) / 4.0
                s = score * (0.7 + 0.3 * max(0.0, lvl_fit))
                if s > best_score:
                    best, best_score = it, s
        # 题库在某个知识点上问光了，但那个点的证据还不够 —— 这正是固定题库的
        # 天花板：每个知识点就 3 道题，问完了想再确认一次都没得问。
        # 这种情况优先交给命题 Agent 现场补题，比换个已经问清楚的知识点更有价值。
        starved = self._starved()
        if starved and len(self.generated) < self.max_generated:
            gen = self._generate_for(starved)
            if gen is not None:
                return {**gen, "_reason": "generated",
                        "_kp_name": self.kps[gen["kp"]]["name"]}

        if best is None:
            return None

        # 题库缺档时现场命题。
        #
        # 触发条件不是"没题了"，而是"没有难度合适的题"。固定题库每个知识点
        # 只有 3 道、难度档位有限，学习者水平落在两道题之间时，硬用一道偏难或
        # 偏易的，那一问的信息量就白费了 —— 太难必错、太易必对，两种情况
        # 都告诉不了你新东西。
        #
        # 生成的题照样要过 ExaminerAgent.vet 的四关审核，过不了就用原来那道。
        gen = self._maybe_generate(best)
        if gen is not None:
            best = gen
        return {**best, "_reason": "select", "_kp_name": self.kps[best["kp"]]["name"]}

    def _starved(self) -> str | None:
        """已有作答但证据不足、且题库已经问光的知识点，取最不确定的那个。"""
        cand = [kp for kp, st in self.state.items()
                if st["n"] >= 1
                and bkt.confidence(st["n"]) < self.target_conf
                and not self._unasked(kp)]
        if not cand:
            return None
        return min(cand, key=lambda k: abs(p_correct(self.state[k]["p"],
                                                     self.params) - 0.5))

    def _generate_for(self, kp: str) -> dict | None:
        if self.examiner is None:
            return None
        item = self.examiner.make_item(
            kp, self._target_level(kp),
            avoid={self.items[i]["stem"] for i in self.asked if i in self.items})
        if item is None:
            return None
        self.items[item["id"]] = item
        self.by_kp.setdefault(kp, []).append(item)
        self.generated.append(item["id"])
        return item

    def _target_level(self, kp: str) -> int:
        """该知识点当前最该问的难度：贴着估计水平走。"""
        p = self.state[kp]["p"]
        return max(config.DIFFICULTY_MIN,
                   min(config.DIFFICULTY_MAX, round(1 + p * 4)))

    def _maybe_generate(self, picked: dict) -> dict | None:
        if self.examiner is None or len(self.generated) >= self.max_generated:
            return None
        kp = picked["kp"]
        want = self._target_level(kp)
        if abs(picked["level"] - want) <= 1:
            return None                       # 现成的题够贴合，不必生成
        item = self.examiner.make_item(
            kp, want, avoid={self.items[i]["stem"] for i in self.asked
                             if i in self.items})
        if item is None:
            return None
        self.items[item["id"]] = item
        self.by_kp.setdefault(kp, []).append(item)
        self.generated.append(item["id"])
        return item

    def blocked_by_prereq(self) -> dict[str, str]:
        """前置已确认是盲区的知识点，不必再测，直接推断为盲区。

        依据是知识点表里的前置关系：连坐标系都分不清的人，
        不可能会 TCP 四点标定。花一道题去确认这件事是浪费。

        这一条是被题量逼出来的，但它本身站得住。15 个知识点要各测到
        可信度 0.75 需要每点 3 题、合计 45 题，没有学习者愿意答。
        实测里不加这条，16 道题会被 15 个知识点的广度吃光，
        每点 n=1、可信度只有 0.4，等于哪个点都没问清楚，
        动态追问也永远没有触发的机会。

        推断出来的盲区会在诊断结果里标明来源，**不冒充实测结果** ——
        界面上单独标注，报告里也要分开统计，不能混进"已测知识点"里。
        """
        out: dict[str, str] = {}
        for kp, node in self.kps.items():
            for pre in node.get("prereq", []):
                st = self.state.get(pre)
                if st and st["n"] >= 1 and st["p"] < config.MASTERY_BLIND:
                    out[kp] = pre
                    break
        return out

    def _done(self) -> bool:
        """需要实测的知识点都达到目标可信度即可结束。

        由前置推断为盲区的知识点不计入，否则永远结束不了。
        """
        blocked = self.blocked_by_prereq()
        need = [kp for kp in self.state
                if not (kp in blocked and self.state[kp]["n"] == 0)]
        if any(self.state[kp]["n"] == 0 for kp in need):
            return False
        return all(bkt.confidence(self.state[kp]["n"]) >= self.target_conf
                   for kp in need)

    # ---- 作答 ----

    def answer(self, item_id: str, choice: int) -> dict:
        """吸收一次作答，返回这一步发生了什么（供前端展示）。"""
        item = self.items[item_id]
        kp = item["kp"]
        st = self.state[kp]
        correct = (choice == item["answer"])

        # 生成题用更保守的失误率。它过了审核，但实际区分度没有经过验证，
        # 不该和人工命题等量齐观。调高 p_S 等价于让这一问的证据打个折。
        params = self.params
        if item.get("origin") == "generated":
            params = bkt.BKTParams(p_T=self.params.p_T,
                                   p_S=config.BKT_P_S_GENERATED,
                                   p_G=self.params.p_G)
            params.validate()

        before = st["p"]
        pred = p_correct(before, params)
        st["p"] = bkt.update(before, correct, params)
        st["n"] += 1
        st["correct"] += int(correct)
        st["curve"].append(round(st["p"], 3))
        self.asked.append(item_id)

        # 追问判定：作答与估计冲突时，就地消歧
        probe_reason = ""
        # 关键：必须已经有该知识点的作答证据才追问（st["n"] 在自增前 >= 1）。
        #
        # 不加这个门槛的话，先验偏低（比如零基础学员的 0.20）时，
        # 任何一道答对都满足"估计为盲区却答对"，第一题就触发追问。
        # 但那时候手上一条证据都没有，所谓"冲突"是跟一个通用先验冲突，
        # 不是跟观测冲突 —— 消解不了任何歧义，只是白白多问一道题。
        # 实测里这会让 16 题中有 7 题是追问，覆盖率被吃掉近一半。
        #
        # 追问的意义是化解**证据之间**的矛盾，所以必须先有证据。
        if (st["n"] - 1) >= 1 and st["probes"] < MAX_PROBES_PER_KP and self._unasked(kp):
            if before >= PROBE_HIGH and not correct:
                probe_reason = "估计已掌握却答错，需分辨是一次失误还是确实没掌握"
            elif before <= PROBE_LOW and correct:
                probe_reason = "估计为盲区却答对，需分辨是真会还是四选一蒙中"
        if probe_reason:
            self.pending_probe = kp
            st["probes"] += 1

        step = {
            "item_id": item_id, "kp": kp, "kp_name": self.kps[kp]["name"],
            "correct": correct,
            "predicted": round(pred, 3),
            "before": round(before, 3), "after": round(st["p"], 3),
            "delta": round(st["p"] - before, 3),
            "surprise": round(abs((1.0 if correct else 0.0) - pred), 3),
            "probe": probe_reason,
            "confidence": bkt.confidence(st["n"]),
            "level": item.get("level", 3),
            "origin": item.get("origin", "bank"),
            "source_id": item.get("source_id"),
        }
        self.log.append(step)
        return step

    # ---- 导出 ----

    def responses(self) -> dict[str, int]:
        """导出成 profile 的 responses 结构，交给 DiagnoseAgent 复算。

        注意这里存的是学习者实际选的选项号，不是对错。诊断 Agent 会自己
        比对答案，两边算出来的掌握度必须一致 —— 测试里有这一条。
        """
        out = {}
        for step, iid in zip(self.log, self.asked):
            item = self.items[iid]
            out[iid] = item["answer"] if step["correct"] else (
                (item["answer"] + 1) % len(item["options"]))
        return out

    def snapshot(self) -> dict:
        return {
            "prior": round(self.prior, 3),
            "asked": len(self.asked),
            "max_items": self.max_items,
            "done": self._done() or len(self.asked) >= self.max_items,
            "coverage": sum(1 for st in self.state.values() if st["n"] > 0),
            "inferred": self.blocked_by_prereq(),
            "generated": list(self.generated),
            "kp_total": len(self.state),
            "state": {k: {"p": round(v["p"], 3), "n": v["n"],
                          "correct": v["correct"], "curve": v["curve"],
                          "confidence": bkt.confidence(v["n"]),
                          "name": self.kps[k]["name"]}
                      for k, v in self.state.items()},
            "log": self.log,
        }

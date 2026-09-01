"""交叉验证与辩论 Agent。

榜题在技术创新性那 25 分里点名了"基于多 Agent 交叉验证与辩论机制解决垂直
领域知识生成的幻觉问题"，这个文件就是对着那句话写的。

先说不做什么：**不做自由辩论**。让两个模型来回吵三轮，token 烧掉一大把，
准确率不见得涨，过程也没法测。那是表演，不是机制。

这里做的是**断言粒度的对抗加仲裁**，三步：

  1. 两位领域专家 Agent 独立起草。关键在"独立"：两者用不同的检索视角，
     专家甲按知识点过滤后取 top-k（窄而准），专家乙全库检索后按知识点重排
     （宽而全）。视角不同，犯的错才不同，交叉验证才有意义。
     如果两个 Agent 看到完全一样的上下文，那是同一个模型问两遍，没有信息增益。

  2. 对齐。把两份断言按相似度配对，分成三类：
       双方都提 → 交叉印证，置信最高
       仅一方提 → 单方证据，走常规审核
       双方冲突 → 讲同一件事但说法不同，进入仲裁

  3. 仲裁。裁判不投票、不看谁说得漂亮，只回到知识库比证据分。
     分差不够就两条都毙掉，宁可少说也不赌。

为什么这样设计站得住：冲突意味着至少有一方错了，而两位专家的检索视角不同，
一方基于窄上下文编造时，另一方大概率不会编出同一句。所以冲突本身就是
高价值的幻觉信号，这一点在 evalkit/redteam.py 里有量化。
"""

from __future__ import annotations

import config
from core.retrieval import Retriever, numbers_in, overlap_ratio, tokenize
from core.schema import (Claim, CONSENSUS_ARBITRATED, CONSENSUS_BOTH,
                         CONSENSUS_SINGLE)


def similarity(a: str, b: str) -> float:
    """两条断言的二元组 Jaccard。用来判断它们是不是在讲同一件事。"""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class DebateAgent:
    name = "交叉验证裁判Agent"

    def __init__(self, retriever: Retriever, auditor):
        self.retriever = retriever
        self.auditor = auditor

    # ---- 第二步：对齐 ----

    def align(self, left: list[Claim], right: list[Claim]) -> dict:
        """把两份断言配对。返回一致、冲突、单方三类。

        配对策略是贪心最大相似度。断言数量在几条到十几条的量级，
        不值得上匈牙利算法，贪心的结果也更好解释。
        """
        pairs: list[tuple[Claim, Claim, float]] = []
        used_r: set[int] = set()

        # 配对阈值分两档：引用同一个切片时放宽。
        #
        # 二元组 Jaccard 认不出改写，这是它的硬伤。
        #   甲「工件坐标系采用三点法标定并按右手定则生成Z轴」
        #   乙「工件坐标系通过三点法建立，Z轴依右手定则确定」
        # 讲的是同一件事，相似度只有 0.41，按单一阈值会各自漏成"单方"，
        # 白白丢掉一次交叉验证的机会。
        # 但两条都引 KB-008，出处相同本身就是"在讲同一段材料"的强证据，
        # 所以同源时用更低的门槛配对。
        #
        # 这仍然是权宜之计。真正的改法是把对齐换成句向量或 NLI 蕴含判断，
        # 接了真模型之后应当优先做这一项，见 README 的待办清单。
        cand = []
        for i, lc in enumerate(left):
            for j, rc in enumerate(right):
                s = similarity(lc.text, rc.text)
                same_src = bool(lc.source_id) and lc.source_id == rc.source_id
                floor = (config.DEBATE_ALIGN_MIN_SAME_SRC if same_src
                         else config.DEBATE_ALIGN_MIN)
                if s >= floor:
                    cand.append((s, i, j))
        cand.sort(reverse=True)

        used_l: set[int] = set()
        for s, i, j in cand:
            if i in used_l or j in used_r:
                continue
            used_l.add(i)
            used_r.add(j)
            pairs.append((left[i], right[j], s))

        agreed, conflicts = [], []
        for lc, rc, s in pairs:
            # 判一致必须同时满足三条：文本高度相似、出处相同、**数值集合相同**。
            #
            # 第三条是单元测试逼出来的，而且是这个文件里最重要的一行。
            # 原来只看前两条，结果
            #   甲「限速250毫米每秒」/ 乙「限速600毫米每秒」
            # 文本相似度 0.95、出处都是 KB-004，被判成"双方印证"直接放行。
            # 数值篡改恰恰是文本最像的一类冲突，只看相似度等于把最危险的
            # 情形当成最可信的情形，方向完全反了。
            same_nums = numbers_in(lc.text) == numbers_in(rc.text)
            if s >= 0.85 and lc.source_id == rc.source_id and same_nums:
                agreed.append((lc, rc, s))
            else:
                conflicts.append((lc, rc, s))

        singles = ([("甲", left[i]) for i in range(len(left)) if i not in used_l]
                   + [("乙", right[j]) for j in range(len(right)) if j not in used_r])
        return {"agreed": agreed, "conflicts": conflicts, "singles": singles}

    # ---- 第三步：仲裁 ----

    def _evidence(self, claim: Claim) -> float:
        """这条断言在全库范围内能拿到的最高证据分。"""
        best = 0.0
        for chunk in self.retriever.chunks:
            r = overlap_ratio(claim.text, f"{chunk.title} {chunk.text}")
            best = max(best, r)
        return best

    def _best_chunk(self, claim: Claim):
        best, score = None, 0.0
        for chunk in self.retriever.chunks:
            r = overlap_ratio(claim.text, f"{chunk.title} {chunk.text}")
            if r > score:
                best, score = chunk, r
        return best, score

    def _numeric_support(self, claim: Claim) -> tuple[int, int]:
        """（被知识库支持的数值个数，断言中的数值总个数）。

        数值冲突时用这个判，不用整句相似度。两条只差一个数字的断言，
        整句相似度几乎一样，拿它当判据等于抛硬币；而数字在不在知识库里，
        是黑白分明的。
        """
        nums = numbers_in(claim.text)
        if not nums:
            return 0, 0
        pool: set[str] = set()
        chunk, _ = self._best_chunk(claim)
        if chunk is not None:
            pool |= numbers_in(f"{chunk.title} {chunk.text}")
        if claim.source_id:
            cited = self.retriever.get(claim.source_id)
            if cited is not None:
                pool |= numbers_in(f"{cited.title} {cited.text}")
        return len(nums & pool), len(nums)

    def arbitrate(self, left: Claim, right: Claim) -> tuple[Claim | None, str]:
        """冲突仲裁。分两层，先判数值，再比整体证据。

        拿不准就两条都毙掉。这一条是故意保守的：内容要进培训教材，
        少说一句的代价远小于说错一句。
        """
        # 第一层：数值裁决。只要双方的数值集合不同，就以"谁的数字在知识库里"定胜负。
        nl, tl = self._numeric_support(left)
        nr, tr = self._numeric_support(right)
        if numbers_in(left.text) != numbers_in(right.text) and (tl or tr):
            if nl != nr:
                win, lose = (left, right) if nl > nr else (right, left)
                wn, ln = (nl, nr) if nl > nr else (nr, nl)
                win.consensus = CONSENSUS_ARBITRATED
                win.rival = lose.text
                return win, (f"数值裁决：采纳方 {wn} 个数值有知识库依据，"
                             f"对立方仅 {ln} 个，对立说法已记录备查")
            return None, (f"双方数值均无法在知识库中确认（各 {nl}/{tl} 与 {nr}/{tr}），"
                          "无法判定，双方均不采纳")

        # 第二层：整体证据分。
        el, er = self._evidence(left), self._evidence(right)
        gap = abs(el - er)
        if gap < config.DEBATE_CONFLICT_MARGIN:
            return None, (f"双方证据分接近（甲 {el:.2f} / 乙 {er:.2f}，"
                          f"差 {gap:.2f} 未超过 {config.DEBATE_CONFLICT_MARGIN}），"
                          "无法判定，双方均不采纳")
        win, lose, ws, ls = ((left, right, el, er) if el > er
                             else (right, left, er, el))
        win.consensus = CONSENSUS_ARBITRATED
        win.rival = lose.text
        return win, f"证据分 {ws:.2f} 胜 {ls:.2f}，采纳前者，对立说法已记录备查"

    # ---- 主入口 ----

    def run(self, left: list[Claim], right: list[Claim]) -> tuple[list[Claim], dict]:
        """返回（送审的断言列表，辩论过程明细）。

        注意这里返回的断言**还没过审**。辩论解决的是"两位专家谁说得对"，
        审核解决的是"这句话知识库里有没有依据"，两件事，不能互相替代。
        辩论过的断言照样要走 AuditAgent，一条都不能免检。
        """
        al = self.align(left, right)
        out: list[Claim] = []
        log = {"agreed": [], "arbitrated": [], "dropped": [], "singles": []}

        for lc, rc, s in al["agreed"]:
            lc.consensus = CONSENSUS_BOTH
            lc.proposed_by = ["专家甲", "专家乙"]
            out.append(lc)
            log["agreed"].append({"text": lc.text, "sim": round(s, 3),
                                  "source": lc.source_id})

        for lc, rc, s in al["conflicts"]:
            winner, why = self.arbitrate(lc, rc)
            entry = {"甲": lc.text, "乙": rc.text, "sim": round(s, 3), "why": why}
            if winner is None:
                log["dropped"].append(entry)
                continue
            winner.proposed_by = ["专家甲" if winner is lc else "专家乙"]
            out.append(winner)
            entry["采纳"] = winner.text
            log["arbitrated"].append(entry)

        for who, c in al["singles"]:
            c.consensus = CONSENSUS_SINGLE
            c.proposed_by = [f"专家{who}"]
            out.append(c)
            log["singles"].append({"by": who, "text": c.text, "source": c.source_id})

        stats = {
            "left_n": len(left), "right_n": len(right),
            "agreed_n": len(log["agreed"]),
            "arbitrated_n": len(log["arbitrated"]),
            "dropped_n": len(log["dropped"]),
            "single_n": len(log["singles"]),
        }
        return out, {"stats": stats, "log": log}

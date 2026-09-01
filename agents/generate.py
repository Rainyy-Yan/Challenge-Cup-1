"""领域生成 Agent。

分两步：先出断言，再组装资源。

拆开的理由是审核只需要审断言，不需要审排版。断言过审之后，同一批断言套三个
模板出讲义、实操指南、分阶测试题，三种形态共享同一个已验证的事实池。
这样"至少3种形态"这条要求几乎不增加额外的幻觉风险，也不用审三遍。

检索时按知识点硬过滤，模型看不到别的知识点的切片，跨点串味在检索阶段就断了。
"""

from __future__ import annotations

import json

import config
from core.llm import parse_json
from core.retrieval import Retriever
from core.retrieval import numbers_in
from core.schema import (Claim, Diagnosis, Resource, VERDICT_CONTRADICTED)

_SYSTEM = (
    "你是工业机器人领域的技术专家，正在为技能培训编写内容。"
    "严格只使用提供的资料，禁止补充资料以外的任何事实、数字或规范。"
    "把内容拆成若干条可独立判断真伪的陈述句，每条标注它来自哪个切片 id。"
    "只输出 JSON：{\"claims\":[{\"text\":\"...\",\"source_id\":\"KB-xxx\"}]}"
)


def learner_level(mastery) -> int:
    """把掌握概率映射到 1-5 的水平档位。

    评测脚本和生成 Agent 共用这一个函数，避免两边各写一份、算出来对不上。

    映射以盲区阈值为锚：掌握概率低于 MASTERY_BLIND 一律记为 1 级。
    这一条是把估计器从答对率换成 BKT 之后补的，不是为了让指标好看。
    原因是两者的取值语义不同：答对率的下界是 0，BKT 的下界是先验 p_L0，
    一个完全不会的人在 BKT 下也有 0.2 左右的分。沿用 1+round(score*4)
    会把他判成 2 级，而系统给盲区知识点生成的是降一档的入门内容，
    于是"资源难度低于学习者水平"，适配判定假性失败。
    换句话说这是换估计器带来的口径不一致，不是模型效果问题。

    改动记录：与 BKT 上线同一次提交，早于任何一次评测批次。
    """
    score = 0.0 if mastery is None else mastery.score
    if score < config.MASTERY_BLIND:
        return config.DIFFICULTY_MIN
    # 盲区阈值以上的区间线性铺到 1-5
    span = max(1e-6, 1.0 - config.MASTERY_BLIND)
    lvl = 1 + round((score - config.MASTERY_BLIND) / span * 4)
    return max(config.DIFFICULTY_MIN, min(config.DIFFICULTY_MAX, lvl))


def is_adapted(resource_difficulty: int, lvl: int, kp_level: int,
               strong: bool = False) -> bool:
    """判定一份资源的难度对该学习者是否适配。

    基本规则是窗口判定：难度落在 [学员水平, 学员水平+2] 之内算适配。

    但窗口有个前提被最初的规格漏掉了：**知识点本身有难度上限**。
    「安全围栏高度不低于1.4米」是一级知识点，不管学习者水平多高，
    这条知识也变不成三级内容。当学员水平已经高过该知识点能达到的天花板时，
    系统能做的就是给出天花板难度，此时再判"不适配"是在惩罚一个无解的约束。

    这个漏洞是演示界面顶出来的：批量评测按全部盲区平均，掩盖了它；
    演示只取前 4 个盲区（恰好都是低阶知识点），适配率直接掉到 50%。
    这不是模型效果问题，是判定口径没考虑知识点上限。

    所以判定分两种情形：
      天花板 ≥ 学员水平 → 用常规窗口
      天花板 < 学员水平 → 资源难度等于天花板即算适配
    天花板取该知识点在当前掌握状态下能产出的最高难度：掌握牢固时是
    kp_level + 1，否则就是 kp_level 本身 —— 一个一级知识点对着薄弱的学习者，
    再怎么排也只有一级内容可讲。

    改动记录：与 variant 字段同一次提交，早于此后的所有评测批次。
    """
    ceiling = min(config.DIFFICULTY_MAX, kp_level + (1 if strong else 0))
    if ceiling < lvl:
        return resource_difficulty == ceiling
    delta = resource_difficulty - lvl
    return config.ADAPT_WINDOW_LOW <= delta <= config.ADAPT_WINDOW_HIGH


class GenerateAgent:
    name = "领域生成Agent"

    def __init__(self, llm, retriever: Retriever, kp_index: dict, auditor=None):
        self.llm = llm
        self.retriever = retriever
        self.kp_index = kp_index
        # 反向题要靠审核闸认证"假"，没有 auditor 就只出正命题
        self.auditor = auditor

    def target_difficulty(self, kp: str, diag: Diagnosis) -> int:
        """资源难度 = 知识点固有难度，按掌握度做一档修正。

        这就是 config 里预先登记的适配规则，评测时照着复算。
        掌握度为零的知识点不能直接上原始难度，要降一档做入门；
        已经薄弱但不是全空白的，按原始难度走。
        """
        base = self.kp_index[kp]["level"]
        m = diag.by_kp(kp)
        if m is None or m.status == "blind":
            base -= 1
        elif m.status == "strong":
            base += 1
        # 再按适配窗口封顶：难度不能超出学习者当前水平两档，
        # 否则零基础的人会被直接丢给五级难度的内容。
        base = min(base, learner_level(m) + config.ADAPT_WINDOW_HIGH)
        return max(config.DIFFICULTY_MIN, min(config.DIFFICULTY_MAX, base))

    def draft(self, kp: str, diag: Diagnosis) -> tuple[list[Claim], list[dict]]:
        """专家甲的视角：按知识点硬过滤后取 top-k。窄而准。"""
        name = self.kp_index[kp]["name"]
        hits = self.retriever.search(name, top_k=config.RETRIEVE_TOP_K, kp=kp)
        return self._draft_from(kp, diag, hits, view="narrow")

    def draft_wide(self, kp: str, diag: Diagnosis) -> tuple[list[Claim], list[dict]]:
        """专家乙的视角：全库检索后按知识点重排。宽而全。

        与专家甲拿到不同的上下文，这是交叉验证有信息增益的前提。
        两个 Agent 看同一份上下文等于同一个模型问两遍，配对全是一致，
        一致率再高也说明不了什么。

        检索用知识点名加上前置知识点名做查询扩展，因此可能捞到邻近知识点的
        切片；重排时本知识点的切片加权，但不做硬过滤，保留跨点的视野。
        """
        node = self.kp_index[kp]
        terms = [node["name"]] + [self.kp_index[p]["name"]
                                  for p in node.get("prereq", [])
                                  if p in self.kp_index]
        terms += node.get("tags", [])
        hits = self.retriever.search(" ".join(terms),
                                     top_k=config.RETRIEVE_TOP_K + 3)
        hits = [(c, s * (1.5 if c.kp == kp else 0.7)) for c, s in hits]
        hits.sort(key=lambda x: -x[1])
        return self._draft_from(kp, diag, hits[:config.RETRIEVE_TOP_K], view="wide")

    def _draft_from(self, kp: str, diag: Diagnosis, hits, view: str
                    ) -> tuple[list[Claim], list[dict]]:
        name = self.kp_index[kp]["name"]
        if not hits:  # 该知识点没有切片，宁可不生成也不让模型自由发挥
            return [], []
        chunks = [{"id": c.id, "title": c.title, "text": c.text, "source": c.source}
                  for c, _ in hits]
        raw = self.llm.run(
            task="draft_claims",
            system=_SYSTEM,
            user=json.dumps({
                "知识点": name,
                "目标难度": self.target_difficulty(kp, diag),
                "资料": chunks,
            }, ensure_ascii=False),
            context={"chunks": chunks, "n": config.CLAIMS_PER_KP,
                     "seed": f"{diag.profile_id}:{kp}:{view}"},
            json_mode=True,
        )
        data = parse_json(raw, {"claims": []})
        claims = [Claim(text=c.get("text", ""), source_id=c.get("source_id"))
                  for c in data.get("claims", []) if c.get("text")]
        return claims, chunks

    # ---- 三种资源形态，都从已过审的断言池组装 ----

    def _lecture(self, kp: str, claims: list[Claim], diff: int) -> Resource:
        name = self.kp_index[kp]["name"]
        lines = [f"# {name}", "", f"难度 {diff}/5，共 {len(claims)} 个要点。", ""]
        for i, c in enumerate(claims, 1):
            chunk = self.retriever.get(c.source_id)
            src = chunk.source if chunk else c.source_id
            lines.append(f"{i}. {c.text}")
            lines.append(f"   > 依据：{c.source_id}　{src}")
            lines.append("")
        return Resource(kind="lecture", kp=kp, title=f"{name}·要点讲义",
                        difficulty=diff, claims=list(claims), body="\n".join(lines))

    def _sop(self, kp: str, claims: list[Claim], diff: int) -> Resource:
        name = self.kp_index[kp]["name"]
        tags = self.kp_index[kp].get("tags", [])
        lines = [f"# {name}·实操指南", "", "## 前置条件",
                 "- 已完成开工前安全确认，围栏内无人",
                 "- 示教器在手，模式旋钮处于手动并锁定",
                 "", "## 操作步骤", ""]
        for i, c in enumerate(claims, 1):
            lines.append(f"**步骤 {i}**　{c.text}")
            lines.append(f"　　依据 {c.source_id}")
            lines.append("")
        lines += ["## 常见错误", ""]
        for c in claims[:2]:
            lines.append(f"- 忽略「{c.text[:24]}…」这一条，容易在验收环节返工")
        if "安全" in tags:
            lines += ["", "## 安全提示", "- 本知识点涉及安全规程，作业前须由带教人员确认"]
        return Resource(kind="sop", kp=kp, title=f"{name}·实操指南",
                        difficulty=diff, claims=list(claims), body="\n".join(lines))

    def _falsify(self, claim: Claim) -> str | None:
        """把一条已过审的真断言改成**可认证为假**的说法。

        判断题只有"正确"选项，等于没考。早期版本就是这样：每道判断题的
        答案都是 true，学习者一路点「正确」就是满分，这份测评一点信息都没有。
        这比选项位置偏斜还糟 —— 那个至少还有 25% 的下限。

        做法是把审核闸**反过来用**：改动断言里的数值，再送回 AuditAgent，
        只有当它被判为 contradicted 时才采纳。也就是说，这条假命题的
        "假"是知识库认证过的，不是我们自己认为它假。

        改不出可认证的假命题就返回 None，那道题保持为真命题 —— 宁可少一道
        反向题，不能放一条我们自己也说不清真假的说法进去。
        """
        if self.auditor is None:
            return None
        nums = sorted(numbers_in(claim.text))
        if not nums:
            return self._falsify_by_term(claim)
        target = max(nums, key=lambda x: float(x))
        for factor in (2, 3, 0.5, 4, 0.25):
            try:
                val = float(target) * factor
            except ValueError:
                continue
            new = (f"{val:.1f}".rstrip("0").rstrip(".")
                   if "." in target else str(int(val)))
            if new == target:
                continue
            cand = claim.text.replace(target, new, 1)
            probe = Claim(text=cand, source_id=claim.source_id)
            kept, dropped = self.auditor.review([probe])
            if dropped and dropped[0].verdict == VERDICT_CONTRADICTED:
                return cand
        return self._falsify_by_term(claim)

    def _falsify_by_term(self, claim: Claim) -> str | None:
        """不含数字的断言：把它的特征术语换成别的知识点的术语。

        大部分断言是没有数字的（「安全回路板负责处理急停信号」），
        只做数值扰动的话，一整套判断题里只有一两道能翻成反向题，
        答案分布仍然接近全真。

        换术语等于制造一条"张冠李戴"式的假命题，而审核闸本来就有
        术语归属和全库最佳匹配两条规则专门抓这个 —— 认证手段是现成的，
        不需要另立判据。同样是只有被判为 contradicted 才采纳。
        """
        if self.auditor is None or not claim.source_id:
            return None
        cited = self.retriever.get(claim.source_id)
        if cited is None:
            return None
        mine = self.retriever.distinctive_in(claim.text)
        if not mine:
            return None
        # 从别的知识点里取替换词，确保它确实不属于本切片
        pool: list[str] = []
        for c in self.retriever.chunks:
            if c.kp == cited.kp:
                continue
            for t in self.retriever.distinctive_in(f"{c.title} {c.text}"):
                if t not in mine:
                    pool.append(t)
        if not pool:
            return None
        for src_term in sorted(mine):
            for repl in pool[:40]:
                cand = claim.text.replace(src_term, repl, 1)
                if cand == claim.text:
                    continue
                probe = Claim(text=cand, source_id=claim.source_id)
                _, dropped = self.auditor.review([probe])
                if dropped and dropped[0].verdict == VERDICT_CONTRADICTED:
                    return cand
        return None

    def _quiz(self, kp: str, claims: list[Claim], diff: int) -> Resource:
        name = self.kp_index[kp]["name"]
        raw = self.llm.run(
            task="quiz",
            system="你是命题老师。只根据给定陈述出题，不要引入陈述之外的知识点。"
                   "输出 JSON：{\"items\":[{\"stem\":\"\",\"type\":\"judge\",\"answer\":true,"
                   "\"difficulty\":1,\"source_id\":\"\",\"explain\":\"\"}]}",
            user=json.dumps({"知识点": name, "陈述": [c.text for c in claims]},
                            ensure_ascii=False),
            context={"claims": [{"text": c.text, "source_id": c.source_id} for c in claims],
                     "kp_name": name, "difficulty": diff},
            json_mode=True,
        )
        items = parse_json(raw, {"items": []}).get("items", [])

        # 掺入反向题：拿一条真断言改出可认证的假命题，答案为「错误」。
        # 全是正命题的判断题等于没考，见 _falsify 的说明。
        for i, it in enumerate(items):
            if i % 2 == 0:                      # 隔一道翻一道，保持正反均衡
                continue
            src = next((c for c in claims if c.text == it.get("_from")), None)
            src = src or (claims[i] if i < len(claims) else None)
            if src is None:
                continue
            fake = self._falsify(src)
            if fake is None:
                continue
            it["stem"] = it["stem"].replace(src.text, fake)
            it["answer"] = False
            it["explain"] = (f"依据{src.source_id}，原文并非如此；"
                             "该说法已由审核环节判定与知识库冲突。")

        # 分阶：按难度排序，保证题目从易到难
        items.sort(key=lambda x: x.get("difficulty", diff))
        return Resource(kind="quiz", kp=kp, title=f"{name}·分阶测试题",
                        difficulty=diff, claims=list(claims), items=items)

    def assemble(self, kp: str, claims: list[Claim], diff: int) -> list[Resource]:
        return [self._lecture(kp, claims, diff),
                self._sop(kp, claims, diff),
                self._quiz(kp, claims, diff)]

    def simplify(self, resource: Resource) -> Resource:
        """降维解释：难度降一档，逐条改写成更直白的说法，引用保持不变。"""
        new_diff = max(config.DIFFICULTY_MIN, resource.difficulty - 1)
        claims = []
        for c in resource.claims:
            txt = self.llm.run(
                task="simplify",
                system="把下面这句话改写得更直白，面向零基础学员，不要增加新信息。",
                user=c.text, context={"text": c.text},
            ) or c.text
            claims.append(Claim(text=txt, source_id=c.source_id,
                                verdict=c.verdict, evidence_score=c.evidence_score))
        out = self._lecture(resource.kp, claims, new_diff)
        out.title = f"{out.title}（降维版）"
        out.variant = "remedial"
        return out

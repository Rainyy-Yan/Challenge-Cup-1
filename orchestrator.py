"""编排层：显式状态机。

没有用 LangGraph / AutoGen 这类框架，是有意的。骨架阶段用手写状态机有三个好处：
  1. 每条状态转移边都能直接写成一个单元测试，榜题明确要求测协同调度逻辑；
  2. 出问题时栈很浅，学生自己 debug 得动，不用先去读框架源码；
  3. 状态和事件都是自己定义的，前端可视化和评测中间数据可以直接复用同一份。
后期确实需要并行调度或者人在回路，再迁移到框架，接口留好了。

状态流：
    INIT -> DIAGNOSE -> PLAN -> [ GENERATE -> AUDIT -> (REVISE)* -> ASSEMBLE ]* -> READY
    READY -> FEEDBACK -> DECIDE -> GENERATE ...   （反馈迭代回到生成）
"""

from __future__ import annotations

import json
import time

import config
from agents.audit import AuditAgent
from agents.debate import DebateAgent
from agents.decide import ACTION_DOWN, ACTION_UP, DecideAgent
from agents.generate import GenerateAgent
from agents.diagnose import DiagnoseAgent
from core.llm import build_llm
from core.retrieval import Retriever
from core.schema import Event, Resource, Session

STATES = ["INIT", "DIAGNOSE", "PLAN", "GENERATE", "DEBATE", "AUDIT", "REVISE",
          "ASSEMBLE", "READY", "FEEDBACK", "DECIDE", "DONE"]

# 合法转移边。测试逐条覆盖，非法跳转直接抛错，防止后期加功能时把流程改乱。
TRANSITIONS = {
    "INIT": {"DIAGNOSE"},
    "DIAGNOSE": {"PLAN"},
    "PLAN": {"GENERATE", "READY"},
    "GENERATE": {"DEBATE", "AUDIT"},
    "DEBATE": {"AUDIT"},
    "AUDIT": {"REVISE", "ASSEMBLE"},
    "REVISE": {"AUDIT", "ASSEMBLE"},
    "ASSEMBLE": {"GENERATE", "READY"},
    "READY": {"FEEDBACK", "DONE"},
    "FEEDBACK": {"DECIDE"},
    "DECIDE": {"GENERATE", "READY"},
    "DONE": set(),
}


class IllegalTransition(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, llm=None, retriever: Retriever | None = None):
        self.llm = llm or build_llm()
        self.retriever = retriever or Retriever.from_jsonl(config.KB_PATH)
        kps = json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]
        self.kp_index = {k["id"]: k for k in kps}
        self.diagnoser = DiagnoseAgent(self.llm)
        self.auditor = AuditAgent(self.llm, self.retriever)
        self.generator = GenerateAgent(self.llm, self.retriever,
                                       self.kp_index, auditor=self.auditor)
        self.debater = DebateAgent(self.retriever, self.auditor)
        self.decider = DecideAgent()
        self.state = "INIT"
        self._seq = 0

    # ---- 状态机 ----

    def _goto(self, target: str, session: Session, agent: str, summary: str,
              detail: dict | None = None, t0: float | None = None) -> None:
        if target not in TRANSITIONS.get(self.state, set()):
            raise IllegalTransition(f"{self.state} -> {target} 不是合法转移")
        self.state = target
        self._seq += 1
        session.events.append(Event(
            seq=self._seq, state=target, agent=agent, summary=summary,
            detail=detail or {},
            ms=int((time.perf_counter() - t0) * 1000) if t0 else 0,
        ))

    # ---- 主流程 ----

    def run(self, profile: dict, max_kp: int = 3) -> Session:
        session = Session(profile_id=profile["id"])

        t0 = time.perf_counter()
        diag = self.diagnoser.run(profile)
        session.diagnosis = diag
        self._goto("DIAGNOSE", session, self.diagnoser.name,
                   f"完成前测评分，识别盲区 {len(diag.gaps)} 个",
                   {"overall": diag.overall, "entry_level": diag.entry_level,
                    "gaps": diag.gaps}, t0)

        session.path = diag.gaps[:max_kp]
        self._goto("PLAN", session, "编排层",
                   f"规划学习路径，本轮生成 {len(session.path)} 个知识点的资源",
                   {"path": session.path,
                    "path_names": [self.kp_index[k]["name"] for k in session.path]})

        for kp in session.path:
            self._produce(kp, session, diag)

        self._goto("READY", session, "编排层", "首轮资源就绪，等待学习交互反馈",
                   {"resource_count": len(session.resources)})
        session.metrics = self.summarize(session)
        return session

    def _produce(self, kp: str, session: Session, diag) -> None:
        name = self.kp_index[kp]["name"]
        diff = self.generator.target_difficulty(kp, diag)

        t0 = time.perf_counter()
        claims, chunks = self.generator.draft(kp, diag)
        if config.DEBATE_ENABLED:
            wide, wchunks = self.generator.draft_wide(kp, diag)
            self._goto("GENERATE", session, "领域专家Agent 甲/乙",
                       f"「{name}」双专家独立起草：甲 {len(claims)} 条（窄检索 "
                       f"{len(chunks)} 片），乙 {len(wide)} 条（宽检索 {len(wchunks)} 片）",
                       {"kp": kp, "chunks": [c["id"] for c in chunks],
                        "wide_chunks": [c["id"] for c in wchunks],
                        "claims": [c.text for c in claims],
                        "wide_claims": [c.text for c in wide]}, t0)

            t0 = time.perf_counter()
            claims, debate = self.debater.run(claims, wide)
            st = debate["stats"]
            session.debates.append({"kp": kp, **debate})
            self._goto("DEBATE", session, self.debater.name,
                       f"交叉验证：印证 {st['agreed_n']} 条，仲裁 {st['arbitrated_n']} 条，"
                       f"存疑弃用 {st['dropped_n']} 条，单方 {st['single_n']} 条",
                       {"kp": kp, **debate}, t0)
        else:
            self._goto("GENERATE", session, self.generator.name,
                       f"「{name}」检索 {len(chunks)} 个切片，草拟 {len(claims)} 条断言",
                       {"kp": kp, "chunks": [c["id"] for c in chunks],
                        "claims": [c.text for c in claims]}, t0)

        t0 = time.perf_counter()
        kept, dropped = self.auditor.review(claims)
        self._goto("AUDIT", session, self.auditor.name,
                   f"逐条核验：通过 {len(kept)} 条，拦截 {len(dropped)} 条",
                   {"kp": kp, "kept": len(kept),
                    "dropped": [{"text": d.text, "verdict": d.verdict,
                                 "note": d.audit_note} for d in dropped]}, t0)

        rounds = 0
        while dropped and rounds < config.MAX_REVISE_ROUNDS:
            rounds += 1
            t0 = time.perf_counter()
            retry, _ = self.generator.draft(kp, diag)
            fresh = [c for c in retry if c.text not in {k.text for k in kept}]
            self._goto("REVISE", session, self.generator.name,
                       f"第 {rounds} 轮重写，补充 {len(fresh)} 条候选断言",
                       {"kp": kp, "round": rounds}, t0)
            more_kept, dropped = self.auditor.review(fresh)
            kept.extend(more_kept)
            self._goto("AUDIT", session, self.auditor.name,
                       f"重写复核：新增通过 {len(more_kept)} 条，仍拦截 {len(dropped)} 条",
                       {"kp": kp, "round": rounds})
            if not more_kept:
                break

        t0 = time.perf_counter()
        resources = self.generator.assemble(kp, kept, diff)
        for r in resources:
            r.dropped = dropped
        session.resources.extend(resources)
        self._goto("ASSEMBLE", session, self.generator.name,
                   f"「{name}」产出 {len(resources)} 种形态资源，难度 {diff}/5",
                   {"kp": kp, "difficulty": diff,
                    "kinds": [r.kind for r in resources]}, t0)

    # ---- 反馈迭代 ----

    def feedback(self, session: Session, kp: str, answers: list[bool]) -> dict:
        if self.state != "READY":
            raise IllegalTransition(f"当前状态 {self.state}，不能接收反馈")
        self._goto("FEEDBACK", session, "编排层",
                   f"收到「{self.kp_index[kp]['name']}」作答 {len(answers)} 题",
                   {"kp": kp, "answers": answers})

        current = next((r.difficulty for r in session.resources if r.kp == kp), 2)
        decision = self.decider.run(kp, answers, current)
        session.decisions.append(decision)
        self._goto("DECIDE", session, self.decider.name,
                   decision["reason"], decision)

        if decision["action"] == ACTION_DOWN:
            base = next(r for r in session.resources
                        if r.kp == kp and r.kind == "lecture")
            t0 = time.perf_counter()
            simplified = self.generator.simplify(base)
            self._goto("GENERATE", session, self.generator.name,
                       f"生成降维版讲义，难度降至 {simplified.difficulty}/5",
                       {"kp": kp, "kind": "lecture_simplified"}, t0)
            # 改写过的内容照样要过审，不能因为是"简化"就免检
            kept, dropped = self.auditor.review(simplified.claims)
            simplified.claims, simplified.dropped = kept, dropped
            self._goto("AUDIT", session, self.auditor.name,
                       f"降维内容复核：通过 {len(kept)} 条，拦截 {len(dropped)} 条",
                       {"kp": kp})
            session.resources.append(simplified)
            self._goto("ASSEMBLE", session, self.generator.name,
                       "降维资源入库", {"kp": kp})
            self._goto("READY", session, "编排层", "等待下一轮反馈")
        elif decision["action"] == ACTION_UP:
            nxt = self._next_kp(session, kp)
            if nxt:
                diag = session.diagnosis
                self._goto("GENERATE", session, self.generator.name,
                           f"晋级到「{self.kp_index[nxt]['name']}」，生成进阶挑战任务",
                           {"kp": nxt})
                self._produce_from_generate(nxt, session, diag)
                session.path.append(nxt)
            self._goto("READY", session, "编排层", "进阶资源就绪")
        else:
            self._goto("READY", session, "编排层", "维持当前难度，等待继续作答")

        session.metrics = self.summarize(session)
        return decision

    def _produce_from_generate(self, kp, session, diag):
        """进阶分支复用生产流程。状态已在 GENERATE，这里补完后半段。"""
        claims, _ = self.generator.draft(kp, diag)
        kept, dropped = self.auditor.review(claims)
        self._goto("AUDIT", session, self.auditor.name,
                   f"进阶内容核验：通过 {len(kept)}，拦截 {len(dropped)}", {"kp": kp})
        diff = min(config.DIFFICULTY_MAX,
                   self.generator.target_difficulty(kp, diag) + 1)
        resources = self.generator.assemble(kp, kept, diff)
        for r in resources:
            r.variant = "advanced"
        session.resources.extend(resources)
        self._goto("ASSEMBLE", session, self.generator.name,
                   f"进阶资源产出 {len(resources)} 份，难度 {diff}/5", {"kp": kp})

    def _next_kp(self, session: Session, kp: str) -> str | None:
        gaps = session.diagnosis.gaps if session.diagnosis else []
        done = {r.kp for r in session.resources}
        for g in gaps:
            if g not in done:
                return g
        return None

    # ---- 会话级指标 ----

    def summarize(self, session: Session) -> dict:
        all_claims = [c for r in session.resources for c in r.claims]
        all_dropped = [c for r in session.resources for c in r.dropped]
        total = len(all_claims) + len({d.text for d in all_dropped})
        gaps = session.diagnosis.gaps if session.diagnosis else []
        covered = {r.kp for r in session.resources}
        return {
            "claims_kept": len(all_claims),
            "claims_dropped": len({d.text for d in all_dropped}),
            "intercept_rate": round(len({d.text for d in all_dropped}) / total, 4) if total else 0.0,
            "gap_total": len(gaps),
            "gap_covered": len([g for g in gaps if g in covered]),
            "resource_count": len(session.resources),
            "kinds": sorted({r.kind for r in session.resources}),
        }


def load_profile(pid: str) -> dict:
    return json.loads((config.PROFILE_DIR / f"{pid}.json").read_text(encoding="utf-8"))

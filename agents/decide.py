"""决策调度 Agent。

这一层坚决用规则，不用大模型。

原因不是偷懒。教学决策要可复现、可解释、可在答辩现场当场推演：正确率 0.4
就必须走降维，跑一百遍都是降维。如果交给模型，同一个输入两次给出不同决策，
"动态迭代机制"就变成了看运气，评委随手一试就露馅。

大模型该出力的地方是内容生成，不是控制流。这一条建议整个项目都遵守。
"""

from __future__ import annotations

import config

ACTION_DOWN = "explain_down"      # 降维解释
ACTION_HOLD = "consolidate"       # 原难度巩固
ACTION_UP = "advance"             # 进阶挑战


class DecideAgent:
    name = "决策调度Agent"

    def run(self, kp: str, answers: list[bool], current_difficulty: int) -> dict:
        n = len(answers)
        if n < config.DECIDE_MIN_ITEMS:
            return {
                "kp": kp, "action": ACTION_HOLD, "accuracy": None, "n": n,
                "reason": f"作答样本 {n} 条，少于 {config.DECIDE_MIN_ITEMS} 条，不调整难度",
                "next_difficulty": current_difficulty,
            }
        acc = sum(1 for a in answers if a) / n
        if acc < config.DECIDE_DOWN:
            action, nxt = ACTION_DOWN, max(config.DIFFICULTY_MIN, current_difficulty - 1)
            reason = f"正确率 {acc:.0%} 低于 {config.DECIDE_DOWN:.0%}，触发降维解释"
        elif acc >= config.DECIDE_ADVANCE:
            action, nxt = ACTION_UP, min(config.DIFFICULTY_MAX, current_difficulty + 1)
            reason = f"正确率 {acc:.0%} 达到 {config.DECIDE_ADVANCE:.0%}，生成进阶挑战任务"
        else:
            action, nxt = ACTION_HOLD, current_difficulty
            reason = f"正确率 {acc:.0%} 处于中间区间，保持当前难度巩固"
        return {"kp": kp, "action": action, "accuracy": round(acc, 3), "n": n,
                "reason": reason, "next_difficulty": nxt}

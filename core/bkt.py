"""贝叶斯知识追踪（BKT）。

替换掉原来的"答对率"打分。答对率有两个硬伤，评委一问就露：

  1. 猜对算掌握。四选一蒙对的概率 25%，答对率把这 25% 当成真掌握。
  2. 失误算不会。真懂的人手滑点错，答对率直接把他打成盲区。

BKT 用四个参数把这两件事显式建模：

  p_L0  初始掌握概率，可由学历背景和实操学时给先验
  p_T   一次学习机会带来的掌握跃迁概率
  p_S   已掌握却答错的概率（slip，失误）
  p_G   未掌握却答对的概率（guess，蒙对）

对四选一客观题，p_G 的理论下界就是 0.25，这不是拍脑袋来的。

更新公式（标准 Corbett-Anderson 形式）：

  答对：P(L|obs) = P(L)(1-p_S) / [ P(L)(1-p_S) + (1-P(L))p_G ]
  答错：P(L|obs) = P(L)p_S     / [ P(L)p_S     + (1-P(L))(1-p_G) ]
  学习：P(L') = P(L|obs) + (1-P(L|obs))p_T

输出是掌握概率，取值 0 到 1，可以直接接到原来的分档阈值上，
所以这次替换对下游是无感的，接口不变。

参数标定说明见 README：现在用的是文献常用初值，队伍拿到真实作答数据后
应当用 EM 或网格搜索重新拟合，拟合脚本见 evalkit/fit_bkt.py。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 四选一客观题的理论蒙对率。判断题应改成 0.5。
GUESS_4CHOICE = 0.25
GUESS_TRUEFALSE = 0.5


@dataclass
class BKTParams:
    p_L0: float = 0.20    # 无先验时的初始掌握概率
    p_T: float = 0.15     # 学习跃迁率（前测阶段没有教学干预，影响很小）
    p_S: float = 0.10     # 失误率
    p_G: float = GUESS_4CHOICE

    def validate(self) -> None:
        """p_S + p_G >= 1 时模型退化，答对答错给出的信息方向会翻转。

        这是 BKT 最常见的失效模式，文献里叫 model degeneracy。
        标定参数时必须卡这一条，否则会得到"答错反而涨掌握度"的荒唐结果。
        """
        for name in ("p_L0", "p_T", "p_S", "p_G"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name}={v} 超出 [0,1]")
        if self.p_S + self.p_G >= 1.0:
            raise ValueError(
                f"参数退化：p_S({self.p_S}) + p_G({self.p_G}) >= 1，"
                "此时答错会被解释为掌握度上升")


def prior_from_background(background: dict, base: float = 0.20) -> float:
    """用学历和实操学时给 p_L0 一个先验。

    这一步就是榜题里说的"整合学习者的先验知识画像"，落到算法上是有实际
    作用的：同样答对一道题，有 120 小时实操的人比零基础的人更可能是真会。

    先验只影响起点，随着作答证据累积会被数据冲淡，不会喧宾夺主。
    """
    edu = str(background.get("education", ""))
    hours = float(background.get("hands_on_hours", 0) or 0)

    prior = base
    if "博士" in edu or "硕士" in edu:
        prior += 0.15
    elif "本科" in edu:
        prior += 0.10
    elif "高职" in edu or "专科" in edu:
        prior += 0.05

    # 实操学时：按对数刻度加成，120 小时约 +0.14，边际递减
    if hours > 0:
        prior += min(0.20, 0.065 * (hours ** 0.5) / 2.0)

    return max(0.05, min(0.85, prior))


def update(p_L: float, correct: bool, params: BKTParams) -> float:
    """吸收一次作答，返回更新后的掌握概率。"""
    if correct:
        num = p_L * (1 - params.p_S)
        den = num + (1 - p_L) * params.p_G
    else:
        num = p_L * params.p_S
        den = num + (1 - p_L) * (1 - params.p_G)
    posterior = num / den if den > 0 else p_L
    return posterior + (1 - posterior) * params.p_T


def trace(responses: list[bool], params: BKTParams, p_L0: float | None = None) -> float:
    """按顺序吸收一串作答，返回最终掌握概率。"""
    params.validate()
    p = params.p_L0 if p_L0 is None else p_L0
    for correct in responses:
        p = update(p, correct, params)
    return p


def trace_curve(responses: list[bool], params: BKTParams,
                p_L0: float | None = None) -> list[float]:
    """返回每一步之后的掌握概率，用于前端画掌握度演进曲线。"""
    params.validate()
    p = params.p_L0 if p_L0 is None else p_L0
    curve = [p]
    for correct in responses:
        p = update(p, correct, params)
        curve.append(p)
    return curve


def confidence(n_obs: int) -> float:
    """估计的可信度，仅由观测条数决定。

    一道题就判人家是盲区，站不住。这个数在前端展示，也用来决定
    要不要在诊断结论里给出"证据不足"的提示。
    """
    if n_obs <= 0:
        return 0.0
    return round(1 - 0.6 ** n_obs, 3)

# ============================================================
# 证据强度：点估计之外还必须给出区间与蒙对概率
# ============================================================
#
# 上面的 trace() 给的是**点估计**。只看点估计会得出荒唐的结论：
# 四选一题答对 2 道，BKT 掌握概率 0.896，判为"掌握牢固"——
# 可纯靠蒙达到 2/2 的概率有 6.2%，二十个人里就有一个能蒙出来。
# 答对 3/4 判"掌握牢固"，蒙对概率 5.1%，同样站不住。
#
# 少量作答下的点估计本身就是高方差的。拿它当结论，
# 等于把"目前的最佳猜测"说成"已经确认"。
# 所以每个知识点除点估计外，必须同时给出两个数：
#
#   蒙对概率  P(得分 ≥ 观察值 | 完全不会)。这是最直观的一个数，
#             直接回答"我瞎蒙也能考成这样吗"。
#   可信区间  掌握概率的区间估计。区间下界才是能对外声称的下限。
#
# 判定规则见 evidence_state()：点估计、区间下界、蒙对概率三者同时达标
# 才能声称"已确认"，否则一律降级为"疑似"并标注证据不足。


def luck_probability(correct: int, asked: int, p_g: float = GUESS_4CHOICE) -> float:
    """完全不会的人靠蒙达到该成绩（或更好）的概率。二项分布右尾。

    这是给学习者和评委看的最直观的一个数。
    "你答对 2/2，但瞎蒙也有 6.2% 的机会考成这样" ——
    比任何掌握概率都更容易理解，也更诚实。
    """
    if asked <= 0:
        return 1.0
    correct = max(0, min(correct, asked))
    total = 0.0
    for i in range(correct, asked + 1):
        total += math.comb(asked, i) * (p_g ** i) * ((1 - p_g) ** (asked - i))
    return min(1.0, total)


def _beta_grid(a: float, b: float, steps: int = 2000):
    """Beta 分布的网格 CDF。

    不引入 scipy —— 全项目零第三方依赖是硬约束。
    n 很小（通常不超过六），网格法足够精确且**便于核对**：
    任何人都能把这段代码逐行读懂并验算，而连分数展开的
    不完全 Beta 函数做不到这一点。诊断结论的可复核性比几位小数重要。
    """
    xs = [(i + 0.5) / steps for i in range(steps)]
    w = []
    for x in xs:
        # 用对数避免 a、b 较大时下溢
        lp = (a - 1) * math.log(x) + (b - 1) * math.log(1 - x)
        w.append(lp)
    m = max(w)
    ws = [math.exp(v - m) for v in w]
    tot = sum(ws)
    ws = [v / tot for v in ws]
    cdf, acc = [], 0.0
    for v in ws:
        acc += v
        cdf.append(acc)
    return xs, cdf


def _beta_quantile(a: float, b: float, q: float) -> float:
    xs, cdf = _beta_grid(a, b)
    for x, c in zip(xs, cdf):
        if c >= q:
            return x
    return xs[-1]


def mastery_interval(correct: int, asked: int, params: BKTParams,
                     level: float = 0.80) -> tuple[float, float]:
    """掌握概率的可信区间。

    做法：作答正确率 θ 的后验取 Beta(1+k, 1+n-k)（均匀先验），
    再按 BKT 的观测模型把 θ 反解回掌握概率：

        θ = p_L(1-p_S) + (1-p_L)p_G   ⟹   p_L = (θ - p_G) / (1 - p_S - p_G)

    这一步反解正是"扣掉蒙对成分"。分母 1-p_S-p_G 必须为正，
    也就是模块开头强调的 p_S + p_G < 1，否则反解方向会翻转。

    区间水平默认取 0.80 而非常用的 0.95：本项目的作答数极少，
    0.95 区间在 n=2 时几乎覆盖 [0,1]，虽然"正确"但没有任何决策价值。
    0.80 在保守与可用之间，且报告中会明确写出所用水平。
    """
    if asked <= 0:
        return (0.0, 1.0)
    denom = 1.0 - params.p_S - params.p_G
    if denom <= 0:
        raise ValueError("p_S + p_G >= 1，无法从作答正确率反解掌握概率")
    lo_q, hi_q = (1 - level) / 2, 1 - (1 - level) / 2
    a, b = 1 + correct, 1 + (asked - correct)
    out = []
    for q in (lo_q, hi_q):
        theta = _beta_quantile(a, b, q)
        p_l = (theta - params.p_G) / denom
        out.append(max(0.0, min(1.0, p_l)))
    return (round(out[0], 3), round(out[1], 3))


# 证据判定的三道阈值
LUCK_MAX = 0.05        # 蒙对概率上限。高于此值不得声称"已确认掌握"
INTERVAL_LEVEL = 0.80  # 可信区间水平


def evidence_state(score: float, lower: float, luck: float, asked: int,
                   ok_th: float, blind_th: float) -> tuple[str, str]:
    """综合点估计、区间下界与蒙对概率，给出判定与理由。

    返回（判定, 理由）。判定取值：
      confirmed_strong  已确认掌握
      likely_strong     疑似掌握，证据不足
      confirmed_blind   已确认盲区
      likely_blind      疑似盲区，证据不足
      inconclusive      证据不足以判定
      untested          未测

    声称"已确认掌握"要同时满足三条：点估计过线、**区间下界也过线**、
    且蒙对概率足够低。三条里最容易被忽略的是第二条 ——
    点估计 0.896 而下界只有 0.31 时，说"已掌握"是没有依据的。
    """
    if asked <= 0:
        return "untested", "未作答"
    if score >= ok_th:
        if lower >= ok_th and luck <= LUCK_MAX:
            return "confirmed_strong", f"下界 {lower:.2f} 过线，蒙对概率 {luck:.1%}"
        why = []
        if lower < ok_th:
            why.append(f"区间下界仅 {lower:.2f}")
        if luck > LUCK_MAX:
            why.append(f"纯蒙也有 {luck:.0%} 概率考成这样")
        return "likely_strong", "；".join(why)
    if score < blind_th:
        # 判盲区不需要卡蒙对概率 —— 蒙对只会把成绩往上抬，
        # 成绩低说明连蒙都没蒙上，结论方向是安全的。
        # 但样本太少时仍不宜下定论。
        if asked >= 2:
            return "confirmed_blind", f"{asked} 题作答，掌握概率 {score:.2f}"
        return "likely_blind", "仅 1 题作答，样本不足"
    return "inconclusive", f"掌握概率 {score:.2f} 处于中间区间"


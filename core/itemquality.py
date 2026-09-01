"""题目质量。

命题闸门（`agents/examiner.vet`）回答的是"这道题**对不对**"。
这个模块回答的是另一个问题：**这道题好不好用**。

两者不能互相替代。一道题可以完全正确、出处清楚、干扰项都不成立，
但仍然测不出任何东西：

  · 太简单，人人都对 —— 答对了不代表会，信息量为零
  · 太难，人人都错 —— 同上
  · 干扰项一眼假 —— 不会的人也能排除，等于三选一甚至二选一
  · 两个干扰项几乎一样 —— 名义四选一，实际三选一
  · 正确答案明显更长 —— 应试技巧就能蒙对

这些都不是"错题"，闸门放它们过去是对的。但拿它们去估计掌握度，
BKT 的 p_G（蒙对率）就不再是 0.25，估出来的掌握概率整体偏高，
而且偏多少不知道。**题不好，尺子就不准。**

分两层：

  一、结构质量（无需作答数据，出题当场就能算）
      查的是命题技术上的瑕疵。全部是可解释的规则，每条都指得出问题在哪。

  二、实测标定（需要作答数据，事后算）
      难度 p 值、区分度点二列相关。这是心理测量学的标准做法，
      **也是发现答案标错的唯一可靠手段** —— 见下面 point_biserial 的说明。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from core.retrieval import jaccard_like, numbers_in, overlap_ratio

# ---- 结构瑕疵等级 ----
SEV_BLOCK = "block"    # 严重到不该用
SEV_WARN = "warn"      # 可用，但要记一笔
SEV_INFO = "info"      # 提示

# 绝对化措辞。出现在干扰项里，应试者一眼就能排除；
# 出现在正确答案里，则往往是命题人把"一般要求"写成了"必须"。
_ABSOLUTE = re.compile(r"总是|永远|绝不|绝对|一定|所有|任何|完全不|从不|必然")

# 两个干扰项的相似度超过这个值就算重复。
#
# 定在 0.60 而不是直觉上的 0.8：中文短选项用二元组 Jaccard 时，
# 改一个字就会掉掉两个二元组。「由三个直线轴构成」和「由三个直线轴组成」
# 只差一个字，实际是同一个选项，相似度却只有 0.667；
# 而真正不同的「由两个回转轴组成」和「由四个平动轴组成」是 0.304。
# 两档之间有足够间隔，0.60 落在中间，不是拍脑袋凑出来的。
_DUP_SIM = 0.60
_CATCHALL = re.compile(r"以上都|上述都|全部正确|全部错误|均正确|均错误|以上均")


@dataclass
class Flaw:
    code: str
    severity: str
    detail: str


@dataclass
class QualityReport:
    item_id: str
    flaws: list[Flaw] = field(default_factory=list)
    score: int = 100
    # 实测部分，没有作答数据时为 None
    p_value: float | None = None
    discrimination: float | None = None
    dead_options: list[int] = field(default_factory=list)
    n_responses: int = 0

    @property
    def usable(self) -> bool:
        return not any(f.severity == SEV_BLOCK for f in self.flaws)

    @property
    def grade(self) -> str:
        if not self.usable:
            return "不可用"
        if self.score >= 85:
            return "良"
        if self.score >= 65:
            return "可用"
        return "待改"


# ============================================================
# 一、结构质量
# ============================================================

_PENALTY = {SEV_BLOCK: 40, SEV_WARN: 15, SEV_INFO: 5}


def structural(item: dict, chunk_body: str = "") -> QualityReport:
    """出题当场就能算的质量检查。不需要任何作答数据。

    chunk_body 给了的话会多做两项与知识库相关的检查（干扰项是否离题、
    正确答案证据是否单薄）。不给也能跑，只是少两项。
    """
    rep = QualityReport(item_id=item.get("id", "?"))
    opts = [str(o).strip() for o in (item.get("options") or [])]
    ans = item.get("answer")
    stem = str(item.get("stem", "")).strip()

    if len(opts) < 3 or not isinstance(ans, int) or not 0 <= ans < len(opts):
        rep.flaws.append(Flaw("BADSHAPE", SEV_BLOCK, "选项数或答案下标不合法"))
        rep.score = 0
        return rep

    right = opts[ans]
    wrong = [o for i, o in enumerate(opts) if i != ans]
    numeric = _all_numeric(opts)

    # F1 长度线索。纯数值选项不适用 —— 长度反映的是量级不是正确性。
    if not numeric:
        lens = [len(o) for o in opts]
        if lens[ans] == max(lens) and min(lens) > 0 and max(lens) >= min(lens) * 2:
            rep.flaws.append(Flaw(
                "LENCLUE", SEV_WARN,
                f"正确答案 {lens[ans]} 字，最短干扰项 {min(lens)} 字，"
                "应试者不用会也能蒙对"))

    # F2 兜底选项。「以上都对」这类会把四选一变成逻辑推理题，
    # 测的不再是本知识点。
    for i, o in enumerate(opts):
        if _CATCHALL.search(o):
            rep.flaws.append(Flaw(
                "CATCHALL", SEV_WARN,
                f"选项{chr(65+i)}「{o}」属兜底表述，会把题目变成逻辑推理"))

    # F3 绝对化措辞
    abs_wrong = [o for o in wrong if _ABSOLUTE.search(o)]
    if len(abs_wrong) >= 2:
        rep.flaws.append(Flaw(
            "ABSOLUTE", SEV_WARN,
            f"{len(abs_wrong)} 个干扰项带绝对化措辞，应试者会直接排除"))
    if _ABSOLUTE.search(right):
        rep.flaws.append(Flaw(
            "ABSANSWER", SEV_INFO,
            "正确答案含绝对化措辞，注意是否把一般要求写成了强制要求"))

    # F4 干扰项冗余。两个几乎一样的干扰项等于少一个选项，
    # 名义蒙对率 25% 实际接近 33%，BKT 的 p_G 就不准了。
    for i in range(len(wrong)):
        for j in range(i + 1, len(wrong)):
            sim = jaccard_like(wrong[i], wrong[j])
            if sim >= _DUP_SIM:
                rep.flaws.append(Flaw(
                    "DUPDISTRACT", SEV_WARN,
                    f"干扰项「{wrong[i][:14]}」与「{wrong[j][:14]}」相似度 "
                    f"{sim:.2f}，实际有效选项少于名义值"))
                break

    # F5 题干过短，没有语境
    if len(stem) < 10:
        rep.flaws.append(Flaw("SHORTSTEM", SEV_WARN,
                              f"题干仅 {len(stem)} 字，缺少作答语境"))

    if chunk_body:
        # F6 干扰项离题。跟知识库几乎无关的干扰项一眼就能排除，
        # 白占一个选项位。
        far = [o for o in wrong
               if not _is_numeric(o) and overlap_ratio(o, chunk_body) < 0.12]
        if len(far) >= 2:
            rep.flaws.append(Flaw(
                "FARDISTRACT", SEV_WARN,
                f"{len(far)} 个干扰项与所考内容几乎无关，缺乏干扰力"))

        # F7 正确答案证据单薄。够格过闸，但离下限很近，值得复核。
        if not _is_numeric(right):
            r = overlap_ratio(right, chunk_body)
            if r < 0.62:
                rep.flaws.append(Flaw(
                    "THINEVIDENCE", SEV_INFO,
                    f"正确答案与出处的证据覆盖率 {r:.2f}，偏低，建议人工复核"))

    rep.score = max(0, 100 - sum(_PENALTY[f.severity] for f in rep.flaws))
    return rep


def _is_numeric(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(re.fullmatch(r"[\d\.零〇一二两三四五六七八九十百千万亿]+\s*\S{0,6}", t)) \
        and bool(numbers_in(t))


def _all_numeric(opts: list[str]) -> bool:
    return all(_is_numeric(o) for o in opts)


def position_bias(items: list[dict]) -> dict:
    """整套题的答案位置分布。单题看不出来，成套才看得出。

    正确答案总落在 B 或 C，是最常见的成套命题瑕疵。应试者摸出规律以后，
    整套题的测量效力都会打折。
    """
    counts: dict[int, int] = {}
    for it in items:
        a = it.get("answer")
        if isinstance(a, int):
            counts[a] = counts.get(a, 0) + 1
    n = sum(counts.values())
    if not n:
        return {"n": 0, "counts": {}, "skewed": False, "detail": ""}
    k = max(len(it.get("options") or []) for it in items) or 4
    expect = n / k
    worst = max(counts.values())
    skewed = worst > expect * 1.6 and n >= 12
    return {
        "n": n,
        "counts": {chr(65 + i): counts.get(i, 0) for i in range(k)},
        "skewed": skewed,
        "detail": (f"正确答案分布不均，最多的一个位置占 {worst}/{n}，"
                   f"均匀应为 {expect:.1f}" if skewed else "位置分布大致均匀"),
    }


# ============================================================
# 二、实测标定
# ============================================================

def key_balance(items: list[dict]) -> dict:
    """整套题的答案是不是全都一样。

    判断题最容易犯这个 —— 题干由已过审的断言直接生成，那答案自然全是"正确"，
    学习者一路点「正确」就是满分，整份测评一点信息都没有。
    这比选项位置偏斜更糟：位置偏斜至少还有 25% 的蒙对下限，
    答案全同则是 100%。

    单题看不出来，必须成套看。
    """
    keys = [it.get("answer") for it in items if it.get("answer") is not None]
    if len(keys) < 3:
        return {"n": len(keys), "skewed": False, "detail": "题量不足，不做判定"}
    uniq = set(map(str, keys))
    counts = Counter(map(str, keys))
    top, n_top = counts.most_common(1)[0]
    skewed = len(uniq) == 1 or n_top / len(keys) >= 0.85
    return {
        "n": len(keys), "counts": dict(counts), "skewed": skewed,
        "detail": (f"{n_top}/{len(keys)} 道答案同为「{top}」，"
                   "学习者只需固定作答即可高分" if skewed else "答案分布可用"),
    }


def p_value(responses: list[bool]) -> float:
    """难度：答对比例。0 最难，1 最易。

    注意方向 —— 心理测量学里 p 值越大题越**简单**，这跟直觉相反，
    报告里要写清楚，别让人读反。
    """
    return sum(responses) / len(responses) if responses else 0.0


def point_biserial(item_correct: list[bool], total_scores: list[float]) -> float | None:
    """区分度：点二列相关系数。

    含义：答对这道题的人，在整份测评上是不是也考得更好。

    这是**发现答案标错的唯一可靠手段**，对本项目尤其要紧 ——
    生成题的答案是模型断言的，标错了不会有任何环节报错。
    一旦答案标错，水平高的人反而更容易选到真正正确的那个选项、
    被判为错，于是相关系数**变成负数**。

    所以：
        r > 0.30   区分度良好
        0.15~0.30  可用
        0~0.15     区分度弱，这道题几乎不提供信息
        r < 0      **危险信号，优先怀疑答案标错**，而不是题目难

    需要至少 8 个作答样本才有意义，少于此返回 None，不给一个假装精确的数。
    """
    n = len(item_correct)
    if n < 8 or n != len(total_scores):
        return None
    xs = [1.0 if c else 0.0 for c in item_correct]
    mx = sum(xs) / n
    my = sum(total_scores) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in total_scores) / n)
    if sx == 0 or sy == 0:
        return None                       # 全对或全错，算不出相关
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, total_scores)) / n
    return round(cov / (sx * sy), 3)


def dead_options(choices: list[int], n_options: int, answer: int) -> list[int]:
    """从没被选过的干扰项。它白占一个位置，把实际蒙对率抬高了。"""
    if len(choices) < 10:
        return []
    used = set(choices)
    return [i for i in range(n_options) if i != answer and i not in used]


def calibrate(item: dict, records: list[dict]) -> QualityReport:
    """用作答数据给一道题做实测标定。

    records 每条形如 {"choice": int, "total": float}
    total 是该学习者在整份测评上的得分，用来算区分度。
    """
    rep = structural(item)
    rep.n_responses = len(records)
    if not records:
        return rep

    ans = item["answer"]
    correct = [r["choice"] == ans for r in records]
    rep.p_value = round(p_value(correct), 3)
    rep.discrimination = point_biserial(correct, [r["total"] for r in records])
    rep.dead_options = dead_options([r["choice"] for r in records],
                                    len(item.get("options") or []), ans)

    if rep.n_responses >= 8:
        if rep.p_value >= 0.95:
            rep.flaws.append(Flaw("TOOEASY", SEV_WARN,
                                  f"答对率 {rep.p_value:.0%}，几乎人人都对，信息量近零"))
        elif rep.p_value <= 0.15:
            rep.flaws.append(Flaw(
                "TOOHARD", SEV_WARN,
                f"答对率仅 {rep.p_value:.0%}；先排查答案是否标错，再考虑是题难"))
        if rep.discrimination is not None:
            if rep.discrimination < 0:
                rep.flaws.append(Flaw(
                    "NEGDISC", SEV_BLOCK,
                    f"区分度 {rep.discrimination}，为负 —— 水平高的人反而更容易做错，"
                    "最可能的原因是正确答案标错了，应立即停用并人工复核"))
            elif rep.discrimination < 0.15:
                rep.flaws.append(Flaw(
                    "LOWDISC", SEV_WARN,
                    f"区分度 {rep.discrimination}，偏低，这道题几乎不提供信息"))
    if rep.dead_options:
        letters = "、".join(chr(65 + i) for i in rep.dead_options)
        rep.flaws.append(Flaw("DEADOPT", SEV_INFO,
                              f"干扰项 {letters} 从未被选中，实际蒙对率高于名义值"))

    rep.score = max(0, 100 - sum(_PENALTY[f.severity] for f in rep.flaws))
    return rep


def summarize(reports: list[QualityReport]) -> dict:
    """整套题的质量概览。"""
    if not reports:
        return {"n": 0}
    blocked = [r for r in reports if not r.usable]
    by_code: dict[str, int] = {}
    for r in reports:
        for f in r.flaws:
            by_code[f.code] = by_code.get(f.code, 0) + 1
    calibrated = [r for r in reports if r.discrimination is not None]
    return {
        "n": len(reports),
        "mean_score": round(sum(r.score for r in reports) / len(reports), 1),
        "unusable": len(blocked),
        "unusable_ids": [r.item_id for r in blocked],
        "flaw_counts": dict(sorted(by_code.items(), key=lambda x: -x[1])),
        "calibrated": len(calibrated),
        "mean_discrimination": (
            round(sum(r.discrimination for r in calibrated) / len(calibrated), 3)
            if calibrated else None),
        "negative_discrimination": [r.item_id for r in calibrated
                                    if r.discrimination < 0],
    }

"""人工评分工具：抽样、盲评、算一致性。

    python3 -m tools.rating sample --n 120 --raters 2   # 出评分表
    python3 -m tools.rating score  ratings/            # 汇总并算 Kappa

为什么需要这个工具：

系统里三项指标当中，"适配"那一项是\
自证的 —— 按规则算难度，又用同一条规则去校验，必然接近 100%。
幻觉率也只能证明"生成内容与知识库一致"。
**任何自动化检验都绕不开这层循环，因为判据和被判对象同源。**

打破循环只有一个办法：引入一个与系统无关的判断源，也就是人。
这个工具不产生判断，它只做三件事 —— 把样本抽好、把干扰去掉、把一致性算出来。
判断本身必须由人给。

这和核实 Agent 是同一个思路：机器造仪器，人出判断。

────────────────────────────────────────────────────────────────────
盲评的三条设计
────────────────────────────────────────────────────────────────────

**一、隐藏系统结论。** 评分表里不出现系统给的难度值、掌握概率、
共识等级。看到系统答案再打分，打出来的是对系统的认同度，不是独立判断。

**二、打散顺序。** 同一知识点的资源不相邻，避免评分者形成惯性。
每位评分者拿到的顺序不同，且由固定种子决定，可复现。

**三、评分者互不可见。** 每人一个文件，标注前不许对答案。
Cohen's Kappa 低于 0.6 说明标注标准没对齐，此时**不是取平均，
而是重新对齐标准再标一遍** —— 对不齐的两组数取平均只会得到一个
看着精确的假数。
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

import config
from orchestrator import Orchestrator, load_profile

# 两类评分任务
TASK_CLAIM = "claim"      # 断言是否被知识库支撑
TASK_FIT = "fit"          # 资源难度对该学习者是否合适

CLAIM_OPTIONS = ["支持", "不支持", "无法判断"]
FIT_OPTIONS = ["偏易", "合适", "偏难"]


def _shuffled(items: list, seed: int) -> list:
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


def build_sample(n_claims: int, n_fit: int, seed: int = 20260905) -> dict:
    """跑一遍系统，抽出待评样本。

    抽样本身用固定种子，保证同一批样本可以被不同评分者、不同时间重复抽出，
    也便于事后复查"这个数是从哪批样本算出来的"。
    """
    rng = random.Random(seed)
    claims, fits = [], []

    for pid in ("P-A", "P-B", "P-C"):
        orch = Orchestrator()
        session = orch.run(load_profile(pid), max_kp=5)
        R = orch.retriever
        diag = session.diagnosis

        for res in session.resources:
            m = diag.by_kp(res.kp)
            for c in res.claims:
                chunk = R.get(c.source_id) if c.source_id else None
                claims.append({
                    "task": TASK_CLAIM,
                    "claim": c.text,
                    "source_id": c.source_id,
                    # 给评分者看的是知识库原文，让他判断"这句话是否被这段话支撑"
                    "source_text": chunk.text if chunk else "（引用的切片不存在）",
                    "source_verified": bool(chunk and chunk.verified),
                    # 以下字段仅用于事后比对，评分表里不输出
                    "_system_verdict": c.verdict,
                    "_consensus": c.consensus,
                })
            if res.variant == "primary" and res.kind == "lecture":
                fits.append({
                    "task": TASK_FIT,
                    "learner": {
                        "profile": pid,
                        "背景": load_profile(pid)["background"].get("self_report", ""),
                        "该知识点前测": f"{m.correct}/{m.asked} 题" if m and m.asked else "未测",
                    },
                    "kp_name": orch.kp_index[res.kp]["name"],
                    "resource_title": res.title,
                    "resource_body": res.body[:900],
                    "_system_difficulty": res.difficulty,
                    "_learner_mastery": m.score if m else None,
                })

    # 去重后抽样
    seen, uniq = set(), []
    for c in claims:
        if c["claim"] not in seen:
            seen.add(c["claim"])
            uniq.append(c)
    return {
        "seed": seed,
        "claims": rng.sample(uniq, min(n_claims, len(uniq))),
        "fits": rng.sample(fits, min(n_fit, len(fits))),
    }


def write_sheets(sample: dict, raters: int, outdir: Path) -> list[Path]:
    """给每位评分者写一份顺序不同的评分表。系统结论一律不输出。"""
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []

    # 编号固定，与评分者顺序无关，汇总时按编号对齐
    for i, c in enumerate(sample["claims"], 1):
        c["item_id"] = f"C{i:03d}"
    for i, f in enumerate(sample["fits"], 1):
        f["item_id"] = f"F{i:03d}"

    for r in range(1, raters + 1):
        rows = (_shuffled(sample["claims"], sample["seed"] + r)
                + _shuffled(sample["fits"], sample["seed"] + 100 + r))
        lines = [
            f"# 评分表 · 评分者 {r}",
            "",
            "**标注前请勿与其他评分者讨论。** 一致性系数低于 0.6 时会要求重标，",
            "此时需要的是重新对齐标准，而不是把两份不一致的结果取平均。",
            "",
            "在每题的「答」后面填写选项原文即可。",
            "",
        ]
        for row in rows:
            lines.append(f"## {row['item_id']}")
            if row["task"] == TASK_CLAIM:
                lines += [
                    "",
                    "**知识库原文**",
                    "",
                    "> " + row["source_text"].replace("\n", " "),
                    "",
                    "**待判断的说法**",
                    "",
                    "> " + row["claim"],
                    "",
                    f"问：上面这段原文是否支撑这个说法？（{' / '.join(CLAIM_OPTIONS)}）",
                    "",
                    "答：",
                    "",
                ]
            else:
                lines += [
                    "",
                    f"**学习者**：{row['learner']['背景']}",
                    f"**该知识点前测**：{row['learner']['该知识点前测']}",
                    "",
                    f"**资源**：{row['resource_title']}",
                    "",
                    "```",
                    row["resource_body"],
                    "```",
                    "",
                    f"问：这份资源的难度对这位学习者是否合适？（{' / '.join(FIT_OPTIONS)}）",
                    "",
                    "答：",
                    "",
                ]
        p = outdir / f"rater{r}.md"
        p.write_text("\n".join(lines), encoding="utf-8")
        paths.append(p)

    (outdir / "_key.json").write_text(
        json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


_ANS = re.compile(r"^##\s+([CF]\d{3})\s*$")


def read_sheet(path: Path) -> dict[str, str]:
    """从评分表里读出作答。缺答的条目不计入，不猜。"""
    out, cur = {}, None
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _ANS.match(ln)
        if m:
            cur = m.group(1)
            continue
        if cur and ln.startswith("答："):
            a = ln[2:].strip()
            if a:
                out[cur] = a
            cur = None
    return out


def cohen_kappa(a: dict[str, str], b: dict[str, str], labels: list[str]) -> dict:
    """Cohen's Kappa。只在两人都作答的条目上计算。"""
    common = [k for k in a if k in b and a[k] in labels and b[k] in labels]
    n = len(common)
    if n < 2:
        return {"n": n, "kappa": None, "agreement": None,
                "note": "共同作答条目不足，无法计算"}
    agree = sum(1 for k in common if a[k] == b[k])
    po = agree / n
    pe = 0.0
    for L in labels:
        pa = sum(1 for k in common if a[k] == L) / n
        pb = sum(1 for k in common if b[k] == L) / n
        pe += pa * pb
    kappa = (po - pe) / (1 - pe) if pe < 1 else None
    return {"n": n, "agreement": round(po, 3),
            "kappa": round(kappa, 3) if kappa is not None else None,
            "disagreed": [k for k in common if a[k] != b[k]]}


def interpret(k: float | None) -> str:
    if k is None:
        return "无法判定"
    if k < 0.4:
        return "一致性差，标注标准明显没对齐"
    if k < 0.6:
        return "一致性不足，需重新对齐标准后重标"
    if k < 0.8:
        return "一致性可接受"
    return "一致性良好"


def score(outdir: Path) -> int:
    key_path = outdir / "_key.json"
    if not key_path.exists():
        print(f"找不到 {key_path}，先跑 sample 生成评分表")
        return 2
    sample = json.loads(key_path.read_text(encoding="utf-8"))
    sheets = sorted(outdir.glob("rater*.md"))
    if len(sheets) < 2:
        print("至少需要两位评分者的表才能算一致性")
        return 2

    answers = {p.stem: read_sheet(p) for p in sheets}
    for name, a in answers.items():
        print(f"{name}：作答 {len(a)} 条")

    names = sorted(answers)
    claim_ids = {c["item_id"] for c in sample["claims"]}
    fit_ids = {f["item_id"] for f in sample["fits"]}

    print()
    for task, ids, labels in ((TASK_CLAIM, claim_ids, CLAIM_OPTIONS),
                              (TASK_FIT, fit_ids, FIT_OPTIONS)):
        a = {k: v for k, v in answers[names[0]].items() if k in ids}
        b = {k: v for k, v in answers[names[1]].items() if k in ids}
        r = cohen_kappa(a, b, labels)
        label = "断言是否被支撑" if task == TASK_CLAIM else "资源难度是否合适"
        print(f"── {label} ──")
        if r["kappa"] is None:
            print(f"   {r.get('note', '无法计算')}")
            continue
        print(f"   共同作答 {r['n']} 条，一致 {r['agreement']:.0%}，"
              f"Kappa {r['kappa']}　{interpret(r['kappa'])}")
        if r["kappa"] < 0.6:
            print("   **不要取平均。** 请两位评分者一起过一遍分歧条目，"
                  "对齐标准后重标。")
        if r["disagreed"]:
            print(f"   分歧条目：{'、'.join(r['disagreed'][:12])}"
                  f"{' 等' if len(r['disagreed']) > 12 else ''}")
        print()

    print("提示：分歧条目应由第三人仲裁。报告里要同时给出自动复核结果、")
    print("      人工标注结果与 Kappa 值，三者缺一不可。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="抽样并生成盲评表")
    s.add_argument("--n", type=int, default=120, help="断言样本数")
    s.add_argument("--fit", type=int, default=60, help="难度适配样本数")
    s.add_argument("--raters", type=int, default=2)
    s.add_argument("--out", default="ratings")

    c = sub.add_parser("score", help="汇总评分并计算一致性")
    c.add_argument("--dir", default="ratings")

    args = ap.parse_args()
    if args.cmd == "sample":
        sample = build_sample(args.n, args.fit)
        paths = write_sheets(sample, args.raters, Path(args.out))
        print(f"断言样本 {len(sample['claims'])} 条，"
              f"难度适配样本 {len(sample['fits'])} 条")
        for p in paths:
            print(f"  {p}")
        print("\n评分表中不含系统结论 —— 看到系统答案再打分，")
        print("打出来的是对系统的认同度，不是独立判断。")
    else:
        raise SystemExit(score(Path(args.dir)))


if __name__ == "__main__":
    main()

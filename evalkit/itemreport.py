"""题库质量报告。

    python3 -m evalkit.itemreport

回答的问题是：**我们这套题，测得准吗？**

跟命题闸门分工明确 —— 闸门管"题对不对"，这里管"题好不好用"。
一道完全正确的题也可能一点信息都提供不了。

报告分两部分：
  结构检查   出题当场就能算，不需要作答数据
  位置分布   单题看不出来，成套才看得出
实测标定（难度 p 值、区分度）需要真实作答数据，脚本预留了接口，
拿到数据后传进来即可，见 calibrate_from()。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import config
from core.demo_items import formal_demo_items
from core.itemquality import calibrate, position_bias, structural, summarize
from core.retrieval import Retriever

FLAW_CN = {
    "LENCLUE": "长度线索：正确答案明显更长",
    "CATCHALL": "兜底选项：以上都对之类",
    "ABSOLUTE": "干扰项绝对化措辞，易被排除",
    "ABSANSWER": "正确答案含绝对化措辞",
    "DUPDISTRACT": "干扰项重复，有效选项少于名义值",
    "SHORTSTEM": "题干过短，缺少语境",
    "FARDISTRACT": "干扰项离题，缺乏干扰力",
    "THINEVIDENCE": "正确答案证据偏薄",
    "BADSHAPE": "题目结构不合法",
    "TOOEASY": "太简单，信息量近零",
    "TOOHARD": "太难，先查答案是否标错",
    "NEGDISC": "区分度为负，答案很可能标错",
    "LOWDISC": "区分度偏低",
    "DEADOPT": "存在从未被选中的干扰项",
}


def build(records: dict | None = None, *, formal_demo: bool = False) -> dict:
    items = json.loads(config.PRETEST_PATH.read_text(encoding="utf-8"))["items"]
    if formal_demo:
        items = formal_demo_items(items)
    R = Retriever.from_jsonl(config.KB_PATH)
    bodies = {}
    for c in R.chunks:
        bodies.setdefault(c.kp, f"{c.title} {c.text}")

    reports = []
    for it in items:
        body = bodies.get(it["kp"], "")
        if records and it["id"] in records:
            rep = calibrate(it, records[it["id"]])
        else:
            rep = structural(it, body)
        reports.append(rep)

    per_item = [{
        "id": r.item_id, "score": r.score, "grade": r.grade,
        "usable": r.usable,
        "p_value": r.p_value, "discrimination": r.discrimination,
        "flaws": [{"code": f.code, "severity": f.severity, "detail": f.detail}
                  for f in r.flaws],
    } for r in reports]

    return {
        "summary": summarize(reports),
        "position": position_bias(items),
        "items": per_item,
        "worklist": _worklist(reports),
    }


def _worklist(reports) -> list[dict]:
    """按需要修的紧迫程度排出待办。给学生当任务清单用。"""
    out = []
    for r in reports:
        if r.score >= 85:
            continue
        out.append({
            "id": r.item_id, "score": r.score, "grade": r.grade,
            "todo": [FLAW_CN.get(f.code, f.code) for f in r.flaws
                     if f.severity != "info"],
        })
    out.sort(key=lambda x: x["score"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evalkit/report_items")
    ap.add_argument("--formal-demo", action="store_true",
                    help="只检查允许在正式 Demo 中出现的固定题")
    args = ap.parse_args()

    res = build(formal_demo=args.formal_demo)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "items.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    s = res["summary"]
    print(f"题库共 {s['n']} 道，平均质量分 {s['mean_score']}，"
          f"判为不可用 {s['unusable']} 道")
    print(f"答案位置分布：{res['position']['counts']}　{res['position']['detail']}")
    print()
    print("结构瑕疵分布：")
    for code, n in s["flaw_counts"].items():
        print(f"  {n:>3} 道  {FLAW_CN.get(code, code)}")
    print()
    wl = res["worklist"]
    print(f"待改题目 {len(wl)} 道（质量分低于 85），最需要先改的前 8 道：")
    for w in wl[:8]:
        print(f"  {w['id']}  {w['score']:>3} 分  {w['grade']}　{'；'.join(w['todo'])}")
    print()
    print("说明：结构检查不需要作答数据；难度与区分度需要真实作答，")
    print("      拿到数据后调用 build(records=...) 即可算出，接口已留好。")
    print(f"明细已写入 {out}/items.json")


if __name__ == "__main__":
    main()

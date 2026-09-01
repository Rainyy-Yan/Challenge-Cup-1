"""生成演示快照。

    python3 -m evalkit.snapshot

把三位学习者的完整会话（含辩论明细、审核拦截、指标）跑一遍，导出成一份
JSON，嵌进 web/showcase.html。

为什么要这个：演示服务万一在评委机器上起不来（端口占用、防火墙、Python
版本），双击 showcase.html 照样能完整展示全流程。录视频也省事，不用一边
录一边盯着后台进程。

快照默认开启注入与数值漂移，目的是让审核拦截和辩论仲裁这两条分支在演示里
真的被走到 —— 一个从不出错的演示，说明不了纠错机制存在。
生成的每份快照都带 `injection` 字段标明开关状态，界面上也会明示，
不能让人误以为这是无注入的真实跑分。
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

import config
from orchestrator import Orchestrator, load_profile

PROFILES = ["P-A", "P-B", "P-C"]


def build(inject: float, drift: float) -> dict:
    os.environ["AGENTEDU_INJECT"] = str(inject)
    os.environ["AGENTEDU_DRIFT"] = str(drift)

    sessions = {}
    for pid in PROFILES:
        orch = Orchestrator()
        profile = load_profile(pid)
        session = orch.run(profile, max_kp=4)

        # 走一轮低分反馈，让降维分支出现在时间线里
        kp = session.path[0]
        orch.feedback(session, kp, [False, False, True, False])
        # 再走一轮高分反馈，让进阶分支也出现
        if orch.state == "READY":
            orch.feedback(session, kp, [True, True, True, True])

        data = session.to_dict()
        bg = profile.get("background", {})
        data["profile"] = {
            "id": pid, "name": profile.get("name", pid),
            "label": f"{bg.get('education', '')}·{bg.get('major', '') or '无专业背景'}",
            "grade": bg.get("grade", ""), "hours": bg.get("hands_on_hours", 0),
            "self_report": bg.get("self_report", ""), "goal": profile.get("goal", ""),
            "note": profile.get("note", ""),
        }
        data["kp_index"] = orch.kp_index
        data["path_names"] = [orch.kp_index[k]["name"] for k in session.path]
        sessions[pid] = data

    kb = {}
    orch = Orchestrator()
    for c in orch.retriever.chunks:
        kb[c.id] = {"title": c.title, "source": c.source, "kp": c.kp,
                    "text": c.text}

    items = json.loads(config.PRETEST_PATH.read_text(encoding="utf-8"))["items"]
    kps = json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]

    return {
        "generated_for": "XH-202630 演示快照",
        "items": items,
        "kps": kps,
        "dims": json.loads((config.DATA / "dimensions.json").read_text(encoding="utf-8"))["dimensions"],
        "injection": {"hallucination_rate": inject, "numeric_drift": drift,
                      "note": "演示快照开启了幻觉注入与数值漂移，用于展示纠错分支，非真实跑分"},
        "thresholds": {
            "hallucination": config.TARGET_HALLUCINATION,
            "adapt": config.TARGET_ADAPT,
            "coverage": config.TARGET_COVERAGE,
            "decide_down": config.DECIDE_DOWN,
            "decide_advance": config.DECIDE_ADVANCE,
            "mastery_blind": config.MASTERY_BLIND,
            "mastery_weak": config.MASTERY_WEAK,
            "mastery_ok": config.MASTERY_OK,
        },
        "kb": kb,
        "sessions": sessions,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", type=float, default=0.35)
    ap.add_argument("--drift", type=float, default=0.7)
    ap.add_argument("--out", default="web/snapshot.json")
    args = ap.parse_args()

    data = build(args.inject, args.drift)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")

    total_ev = sum(len(s["events"]) for s in data["sessions"].values())
    total_res = sum(len(s["resources"]) for s in data["sessions"].values())
    print(f"快照已写入 {out}  ({out.stat().st_size / 1024:.0f} KB)")
    print(f"会话 {len(data['sessions'])} 个，调度步骤 {total_ev} 步，资源 {total_res} 份")


if __name__ == "__main__":
    main()

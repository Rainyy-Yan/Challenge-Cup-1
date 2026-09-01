"""命令行入口。

    python3 cli.py P-A

跑一遍完整闭环并打印调度时间线，用来快速确认改动没把流程改坏。
录演示视频时也可以先跑这个把日志摊开讲一遍，再切到网页端。
"""

import json
import sys

from orchestrator import Orchestrator, load_profile


def main() -> None:
    pid = sys.argv[1] if len(sys.argv) > 1 else "P-A"
    orch = Orchestrator()
    session = orch.run(load_profile(pid))

    d = session.diagnosis
    print(f"\n=== 学情诊断 {pid} ===")
    print(f"整体掌握度 {d.overall}    建议起始难度 {d.entry_level}/5")
    print(d.narrative)
    gap_names = [m.name for m in d.mastery if m.kp in d.gaps]
    print(f"盲区 {len(d.gaps)} 个：{'、'.join(gap_names)}")

    print("\n=== 协同调度时间线 ===")
    for e in session.events:
        print(f"[{e.seq:>2}] {e.state:<9} {e.agent:<14} {e.summary}  ({e.ms}ms)")

    print("\n=== 产出资源 ===")
    for r in session.resources:
        print(f"- {r.kind:<8} 难度{r.difficulty}/5  {r.title}  "
              f"断言{len(r.claims)}条  引用{r.sources()}")

    print("\n=== 反馈迭代 ===")
    kp = session.path[0]
    print("模拟作答 4 题错 3 题：")
    print("   ->", orch.feedback(session, kp, [False, False, True, False])["reason"])
    print("模拟作答 4 题全对：")
    print("   ->", orch.feedback(session, kp, [True, True, True, True])["reason"])

    print("\n=== 会话指标 ===")
    print(json.dumps(session.metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

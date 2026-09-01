"""跑一遍知识库核实，产出人工复核队列。

    python3 -m evalkit.factcheck_run --dry        # 不接检索源，看流程
    python3 -m evalkit.factcheck_run --apply      # 结果写回知识库

**这个工具不会把任何条目标成 verified。** 它只做三件事：
生成检索词、拉取外部资料、按证据定出机器状态，然后把证据摆到人面前。
最终的 verified 标签只能由人在看过证据之后手动置位。

理由见 agents/factcheck.py 顶部：让大模型判断大模型写的知识库，
等于让同一个来源自己给自己背书，标签变了可信度没变，
错误反而戴上一顶合规的帽子。

接检索源的方法：实现 SearchBackend 接口，在下面 build_search() 里返回它。
容器内没有网络，默认返回 NullSearch，此时全部条目判为存疑 ——
这是正确行为，不是故障。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import config
from agents.factcheck import (ST_DISPUTED, ST_MACHINE, ST_REFUTED,
                              FactCheckAgent, NullSearch, SearchBackend,
                              apply_results)
from core.llm import build_llm
from core.retrieval import Retriever


def build_search() -> SearchBackend:
    """在这里接入你的检索源。

    可选做法：
      · 模型自带的联网工具（多数厂商的 API 支持 web_search 工具）
      · 独立搜索 API
      · 企业内网的文档检索

    实现 search(query, top_k) -> [{"url","title","text"}] 即可。
    没接的话返回 NullSearch，全部条目判存疑，不会误标。
    """
    if os.environ.get("AGENTEDU_SEARCH_URL"):
        raise NotImplementedError(
            "检测到 AGENTEDU_SEARCH_URL，但适配器尚未实现。"
            "请在 build_search() 中按你的检索源补上，接口见 SearchBackend")
    return NullSearch()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="把结果写回知识库")
    ap.add_argument("--out", default="evalkit/report_factcheck")
    ap.add_argument("--limit", type=int, default=0, help="只核实前 N 条，调试用")
    args = ap.parse_args()

    R = Retriever.from_jsonl(config.KB_PATH)
    pending = [c for c in R.chunks if not c.verified]
    done = len(R.chunks) - len(pending)
    chunks = pending[:args.limit] if args.limit else pending

    agent = FactCheckAgent(build_llm(), build_search())
    # 三个数分开报：--limit 截断之后，"跳过"的口径很容易算错，
    # 一旦算错就会给人"核实进度比实际好"的错觉。
    print(f"知识库 {len(R.chunks)} 条：已人工核实 {done} 条，"
          f"待核实 {len(pending)} 条")
    if args.limit and len(chunks) < len(pending):
        print(f"本次只处理前 {len(chunks)} 条（--limit）")
    results = agent.check_all(chunks, only_unverified=False)

    buckets = {ST_MACHINE: [], ST_DISPUTED: [], ST_REFUTED: []}
    for r in results:
        buckets.setdefault(r.status, []).append(r)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8")

    print()
    print(f"  机器核实通过  {len(buckets[ST_MACHINE]):>3} 条   多来源一致，待人工确认")
    print(f"  存疑          {len(buckets[ST_DISPUTED]):>3} 条   证据不足或来源单一")
    print(f"  被推翻        {len(buckets[ST_REFUTED]):>3} 条   有外部资料明确矛盾")
    print(f"  模型调用失败  {agent.stats['llm_errors']:>3} 次")

    if buckets[ST_REFUTED]:
        print("\n── 优先处理：被外部资料推翻 ──")
        for r in buckets[ST_REFUTED][:8]:
            print(f"  {r.chunk_id}  矛盾来源 {r.refute_domains}")
            for e in r.evidence:
                if e.verdict == "refute":
                    print(f"      {e.domain}：{e.quote}")

    if agent.stats["no_evidence"] == len(results) and results:
        print("\n注意：所有条目都没检索到资料，说明检索源没接上。")
        print("      全判存疑是正确行为 —— 系统不会用模型记忆冒充证据。")
        print("      接入方法见 evalkit/factcheck_run.py 的 build_search()。")

    if args.apply:
        st = apply_results(config.KB_PATH, results)
        print(f"\n已写回知识库：更新 {st['updated']} / {st['total']} 条")
        print("注意：写入的是 check_status 字段，**verified 未被改动**。")
        print("      人工看过证据后，再手动把确认无误的条目置 verified=true。")
    else:
        print(f"\n明细已写入 {out}/results.json（未写回知识库，加 --apply 才写）")


if __name__ == "__main__":
    main()

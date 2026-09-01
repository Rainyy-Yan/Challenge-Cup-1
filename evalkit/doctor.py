"""接模型体检。配好 key 之后的第一件事。

    python3 -m evalkit.doctor

逐项探明这个端点到底支持什么、贵不贵、能不能干活。不通过就别急着跑全流程 ——
接模型最常见的故障是 JSON 模式不支持、模型名写错、限流，这三样在全流程里
表现出来都是"生成结果为空"，很难定位。这里一次性问清楚。

六项检查：
  1. 连通与鉴权     key 对不对、端点通不通
  2. 模型名有效     写错模型名的报错常常被吞掉
  3. JSON 模式      各家支持不一，不支持会自动降级，这里明确告诉你走的哪条路
  4. 中文与约束遵循  能不能按要求只输出 JSON、不加解释
  5. 命题实测       出三道题走一遍四关审核，看通过率
  6. 用量与成本     按配置单价折算这次体检花了多少
"""

from __future__ import annotations

import json
import os
import time

import config
from agents.audit import AuditAgent
from agents.examiner import ExaminerAgent
from core.llm import LLMError, MockLLM, RealLLM, build_llm, parse_json
from core.retrieval import Retriever

OK, BAD, WARN = "通过", "失败", "注意"


def _line(name: str, status: str, detail: str = "") -> None:
    mark = {"通过": "✓", "失败": "✗", "注意": "!"}[status]
    print(f"  {mark} {name:<16}{status}　{detail}")


def main() -> None:
    llm = build_llm()
    if isinstance(llm, MockLLM):
        print("当前是离线桩（未设置 AGENTEDU_API_KEY）。")
        print("系统可以完整运行，但自述解析、命题、综合诊断都走规则版。")
        print("要接真模型，先设置：")
        print("  export AGENTEDU_API_KEY=sk-xxx")
        print("  export AGENTEDU_BASE_URL=<OpenAI 兼容端点>")
        print("  export AGENTEDU_MODEL=<模型名>")
        return

    print(f"端点   {llm.base_url}")
    print(f"模型   默认 {llm.model}"
          + (f"　分档 {json.dumps({k: v for k, v in llm.models.items() if v}, ensure_ascii=False)}"
             if any(llm.models.values()) else "　（未分档，全部任务用同一个）"))
    print()

    # 1 连通与鉴权
    t0 = time.perf_counter()
    try:
        out = llm.run(task="simplify", system="你是一个测试助手。",
                      user="回复两个字：正常", json_mode=False)
        ms = int((time.perf_counter() - t0) * 1000)
        _line("连通与鉴权", OK, f"往返 {ms} ms，返回 {out.strip()[:20]!r}")
    except LLMError as exc:
        _line("连通与鉴权", BAD, str(exc)[:160])
        print("\n先解决连通问题：检查 key、端点地址、网络出口和账户余额。")
        return

    # 2 模型名（分档模型逐个探）
    tiers = {k: v for k, v in llm.models.items() if v}
    if tiers:
        for tier, name in tiers.items():
            probe = RealLLM(llm.base_url, llm.api_key, name,
                            timeout=llm.timeout, retries=1)
            try:
                probe.run(task="simplify", system="测试", user="回复：ok")
                _line(f"模型 {tier}", OK, name)
            except LLMError as exc:
                _line(f"模型 {tier}", BAD, f"{name} 不可用：{str(exc)[:100]}")
    else:
        _line("模型分档", WARN, "未配置 STRONG/MID/LIGHT，命题会用默认模型，成本偏高")

    # 3 JSON 模式
    try:
        raw = llm.run(task="verify", system='只输出 JSON：{"ok":true}',
                      user="确认", json_mode=True)
        data = parse_json(raw)
        if llm._json_mode_ok:
            _line("JSON 模式", OK if data else WARN,
                  "原生支持" if data else "原生支持但返回无法解析")
        else:
            _line("JSON 模式", WARN,
                  "端点不支持 response_format，已自动降级为提示词约束"
                  + ("，解析正常" if data else "，且解析失败"))
    except LLMError as exc:
        _line("JSON 模式", BAD, str(exc)[:120])

    # 4 中文与约束遵循
    try:
        raw = llm.run(
            task="analyze_intake",
            system='你从自述里抽取信息。只输出 JSON：{"education":"","hands_on_hours":0}',
            user="我是机械专业大三的，实操大概40小时", json_mode=True)
        d = parse_json(raw)
        hit = d.get("education") and isinstance(d.get("hands_on_hours"), (int, float))
        _line("中文与约束", OK if hit else WARN,
              f"抽取结果 {json.dumps(d, ensure_ascii=False)[:80]}"
              if d else "未返回可解析结构，命题环节大概率也会不稳")
    except LLMError as exc:
        _line("中文与约束", BAD, str(exc)[:120])

    # 5 命题实测：这项才是真正决定能不能用
    R = Retriever.from_jsonl(config.KB_PATH)
    kps = json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]
    kpi = {k["id"]: k for k in kps}
    ex = ExaminerAgent(llm, R, kpi)
    made, sample = 0, None
    for kp in list(kpi)[:3]:
        try:
            it = ex.make_item(kp, 3)
        except LLMError:
            it = None
        if it:
            made += 1
            sample = sample or it
    status = OK if made >= 2 else (WARN if made == 1 else BAD)
    _line("命题实测", status,
          f"3 次请求产出 {made} 道；审核拒收 {len(ex.rejects)} 次，"
          f"模型无产出 {ex.no_output} 次")
    if ex.rejects:
        print(f"      拒收样例：{ex.rejects[0]['why'][:70]}")
    if sample:
        print(f"      样题：{sample['stem'][:54]}")

    # 6 用量与成本
    st = llm.stats()
    print()
    print(f"本次体检共 {st['calls']} 次调用，失败 {st['failures']} 次，"
          f"输入 {st['tokens_in']} / 输出 {st['tokens_out']} token")
    if llm.price_in or llm.price_out:
        print(f"折合成本 ¥{st['cost_cny']}")
    else:
        print("未配置单价（AGENTEDU_PRICE_IN / _OUT），无法折算成本")

    print()
    if made >= 2:
        print("可以接全流程了：先跑 python3 server.py，再打开 http://127.0.0.1:8000")
    else:
        print("命题通过率偏低。优先排查顺序：模型是否够强 → 提示词是否被截断 → "
              "知识库切片是否太短。审核阈值放在最后调，那是最后一道防线。")


if __name__ == "__main__":
    main()

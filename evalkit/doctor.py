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


def format_model_status(status: dict) -> list[str]:
    """Return a secret-free, operator-readable model-router summary."""
    lines = ["", f"路由策略 {status['strategy']}"]
    for model in status["models"]:
        rate = model["success_rate"]
        rate_text = "暂无样本" if rate is None else f"{rate:.0%}"
        lines.append(
            f"  {model['id']} [{model['role']}] "
            f"{model['health']}，尝试 {model['attempts']}，"
            f"成功率 {rate_text}，平均 {model['avg_latency_ms']} ms"
        )
    lines.append(
        f"自动降级 {status['router']['fallbacks']} 次，"
        f"全部模型失败 {status['router']['all_models_failed']} 次"
    )
    return lines


def _safe_error_detail(exc: LLMError) -> str:
    """Describe an error without exposing a provider response body."""
    text = str(exc)
    for code, label in ((401, "鉴权失败"), (403, "访问被拒绝"),
                        (404, "端点或模型不可用"), (429, "触发限流")):
        if f"HTTP {code}" in text:
            return f"{label}（{code}）"
    return "调用失败；请检查本地配置、网络和账户状态"


def _line(name: str, status: str, detail: str = "") -> None:
    mark = {"通过": "✓", "失败": "✗", "注意": "!"}[status]
    print(f"  {mark} {name:<16}{status}　{detail}")


def main() -> None:
    llm = build_llm()
    if isinstance(llm, MockLLM):
        print("当前是离线桩（未设置 AGENTEDU_API_KEY）。")
        print("系统可以完整运行，但自述解析、命题、综合诊断都走规则版。")
        print("要接真模型，请编辑仓库根目录的 .env：")
        print("  AGENTEDU_API_KEY=<你的 key>")
        print("  AGENTEDU_BASE_URL=<OpenAI 兼容端点>")
        print("  AGENTEDU_MODEL=<模型名>")
        print("修改后重启 server.py，再运行 python3 -m evalkit.doctor。")
        return

    model_ids = [llm.model, llm.models["strong"]]
    print(f"模型   默认 {model_ids[0]}　强模型 {model_ids[1]}")
    print()

    # 1 连通与鉴权
    t0 = time.perf_counter()
    try:
        llm.run(task="simplify", system="你是一个测试助手。",
                user="回复两个字：正常", json_mode=False)
        ms = int((time.perf_counter() - t0) * 1000)
        _line("连通与鉴权", OK, f"往返 {ms} ms")
    except LLMError as exc:
        _line("连通与鉴权", BAD, _safe_error_detail(exc))
        print("\n先解决连通问题：检查 key、端点地址、网络出口和账户余额。")
        return

    # 2 模型名：每次探针仍保持双模型注册，避免构造非法单模型客户端。
    for name in model_ids:
        other = next(item for item in model_ids if item != name)
        probe = RealLLM(
            llm.base_url,
            llm.api_key,
            name,
            timeout=llm.timeout,
            models={"strong": other},
            retries=1,
            cache=False,
        )
        try:
            probe.run(task="simplify", system="测试", user="回复：ok")
            target = next(
                item for item in probe.model_status()["models"]
                if item["id"] == name
            )
            if target["successes"] == 1:
                _line(f"模型 {name}", OK, "可调用")
            else:
                _line(f"模型 {name}", BAD, "目标调用失败，结果来自自动降级")
        except LLMError as exc:
            _line(f"模型 {name}", BAD, _safe_error_detail(exc))

    # 3 JSON 模式
    try:
        raw = llm.run(task="verify", system='只输出 JSON：{"ok":true}',
                      user="确认", json_mode=True)
        data = parse_json(raw)
        if all(llm._json_mode_ok.values()):
            _line("JSON 模式", OK if data else WARN,
                  "原生支持" if data else "原生支持但返回无法解析")
        else:
            _line("JSON 模式", WARN,
                  "当前模型按配置使用提示词 JSON 约束"
                  + ("，解析正常" if data else "，且解析失败"))
    except LLMError as exc:
        _line("JSON 模式", BAD, _safe_error_detail(exc))

    # 4 中文与约束遵循
    try:
        raw = llm.run(
            task="analyze_intake",
            system='你从自述里抽取信息。只输出 JSON：{"education":"","hands_on_hours":0}',
            user="我是机械专业大三的，实操大概40小时", json_mode=True)
        d = parse_json(raw)
        hit = d.get("education") and isinstance(d.get("hands_on_hours"), (int, float))
        _line("中文与约束", OK if hit else WARN,
              "返回可解析结构" if d
              else "未返回可解析结构，命题环节大概率也会不稳")
    except LLMError as exc:
        _line("中文与约束", BAD, _safe_error_detail(exc))

    # 5 命题实测：这项才是真正决定能不能用
    R = Retriever.from_jsonl(config.KB_PATH)
    kps = json.loads(config.KP_PATH.read_text(encoding="utf-8"))["points"]
    kpi = {k["id"]: k for k in kps}
    ex = ExaminerAgent(llm, R, kpi)
    made = 0
    for kp in list(kpi)[:3]:
        try:
            it = ex.make_item(kp, 3)
        except LLMError:
            it = None
        if it:
            made += 1
    status = OK if made >= 2 else (WARN if made == 1 else BAD)
    _line("命题实测", status,
          f"3 次请求产出 {made} 道；审核拒收 {len(ex.rejects)} 次，"
          f"模型无产出 {ex.no_output} 次")

    # 6 用量与成本
    st = llm.stats()
    print()
    print(f"本次体检共 {st['calls']} 次调用，失败 {st['failures']} 次，"
          f"输入 {st['tokens_in']} / 输出 {st['tokens_out']} token")
    if st["by_model"]:
        model_calls = {name: rec["calls"] for name, rec in st["by_model"].items()}
        print(f"模型调用 {json.dumps(model_calls, ensure_ascii=False)}，"
              f"自动降级 {st['fallbacks']} 次")
    if llm.price_in or llm.price_out:
        print(f"折合成本 ¥{st['cost_cny']}")
    else:
        print("未配置单价（AGENTEDU_PRICE_IN / _OUT），无法折算成本")

    for line in format_model_status(llm.model_status()):
        print(line)

    print()
    if made >= 2:
        print("可以接全流程了：先跑 python3 server.py，再打开 http://127.0.0.1:8000")
    else:
        print("命题通过率偏低。优先排查顺序：模型是否够强 → 提示词是否被截断 → "
              "知识库切片是否太短。审核阈值放在最后调，那是最后一道防线。")


if __name__ == "__main__":
    main()

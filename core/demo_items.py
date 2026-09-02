"""正式 Demo 的固定测评题候选集。"""

from __future__ import annotations


def formal_demo_items(items: list[dict]) -> list[dict]:
    """只返回可用于正式演示的固定题。

    被排除的题仍保留在原题库中，便于在补齐一手证据并完成复核后改写、恢复；
    它们不能因为数据文件还在就重新进入线上测评或离线展示。
    """
    return [item for item in items if item.get("demo_eligible", True)]

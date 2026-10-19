"""工具注册表 —— 按名字查找、导出 schema、执行。

参考 MewCode 的 ``ToolRegistry``。额外做了一件 MewCode 里没有的事：
**执行失败不抛异常，转成 is_error 的 ToolResult 交还给模型**。
理由：工具报错（比如日期格式不对、活动不存在）本质上是给模型的一次反馈，
让它自己换个参数重试，比直接 500 给用户看要好得多。Agent 只在工具抛
未捕获异常时才中断。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from .base import Tool, ToolResult

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具名重复: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """导出给 LLM 的工具清单。"""
        return [tool.schema() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                content=f"没有名为 {name} 的工具。可用工具：{', '.join(self.names())}",
                is_error=True,
            )

        try:
            params = tool.params_model(**arguments)
        except ValidationError as e:
            # 把校验错误原样回给模型，它通常能自己改对参数
            return ToolResult(
                content=f"参数校验失败：{e}",
                is_error=True,
            )

        try:
            return await tool.execute(params)
        except Exception as e:  # noqa: BLE001 - 工具内部错误不该掀翻整个对话
            log.exception("工具 %s 执行失败", name)
            return ToolResult(
                content=f"工具 {name} 执行出错：{e.__class__.__name__}: {e}",
                is_error=True,
            )

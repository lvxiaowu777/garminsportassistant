"""工具基类 —— 模型能调用的一个能力。

三个要素缺一不可：
- ``name``：模型在 function calling 里看到的名字
- ``description``：直接给模型读的说明书，写清楚「什么时候该调」比「参数是什么」更重要
- ``params_model``：pydantic 模型，既用于运行时校验，也用于导出 JSON Schema

参照 MewCode 的 ``mewcode/tools/base.py``。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    """工具执行结果。

    只回文本 —— 模型的输入就是文本，把结构化数据先格式化成它读得懂的
    紧凑文本，比塞一大坨 JSON 更省 token 也更好理解。
    """

    content: str
    is_error: bool = False


class Tool(ABC):
    """所有工具的基类。"""

    name: ClassVar[str]
    description: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]

    def schema(self) -> dict[str, Any]:
        """导出成 OpenAI function calling 需要的 JSON Schema。"""
        raw = self.params_model.model_json_schema()
        # pydantic 会塞 title/$defs 等，OpenAI 侧不需要顶层 title
        raw.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": raw,
            },
        }

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult:
        """执行工具。params 已经是按 params_model 校验过的对象。"""
        raise NotImplementedError

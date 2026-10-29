"""长期记忆的修改工具：update_memory / forget_memory。

为什么需要：夜间提炼是批量的，用户在对话里说"马拉松没报上"，
不应该等到第二天才改；Agent 当场就能改。
"""
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from garmin_coach.agent.memory.store import KINDS, delete_memory, upsert_memory

from .base import Tool, ToolResult

MemoryKind = Literal["injury", "goal", "preference", "constraint", "plan_decision"]


class UpdateMemoryParams(BaseModel):
    kind: MemoryKind = Field(..., description="记忆类别")
    key: str = Field(..., min_length=1, max_length=64, description="短键，如 races / knee_pain")
    value: str = Field(..., min_length=1, max_length=500, description="要记住的内容")
    confidence: float = Field(0.9, ge=0.0, le=1.0, description="置信度")


class ForgetMemoryParams(BaseModel):
    kind: MemoryKind = Field(..., description="记忆类别")
    key: str = Field(..., min_length=1, max_length=64, description="要删除的键")
    reason: str = Field("", max_length=200, description="为什么删（可选）")


class UpdateMemoryTool(Tool):
    name = "update_memory"
    description = (
        "更新/新增一条长期记忆。\n"
        "**用户说「记住 / 以后 / 别让我 / 我更喜欢」时必须调用**；\n"
        "用户纠正旧事实（「没报上」「改了」）时也调用。\n"
        "不要把猜测写进记忆；没调用就不要说「记住了」。\n"
        "kind 只能是：injury / goal / preference / constraint / plan_decision。"
    )
    params_model = UpdateMemoryParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: UpdateMemoryParams = params  # type: ignore[assignment]
        ok = await asyncio.to_thread(
            upsert_memory,
            self._user_id,
            p.kind,
            p.key.strip(),
            p.value.strip(),
            confidence=p.confidence,
        )
        if not ok:
            return ToolResult("记忆更新失败（内容为空或写入异常）", is_error=True)
        return ToolResult(f"已记住 {p.kind}/{p.key}：{p.value.strip()}")


class ForgetMemoryTool(Tool):
    name = "forget_memory"
    description = (
        "删除一条已经过时/错误的长期记忆（用户说'那个已经过去了''没报上''改了'时用）。\n"
        "删除后不会再注入给模型。"
    )
    params_model = ForgetMemoryParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: ForgetMemoryParams = params  # type: ignore[assignment]
        deleted = await asyncio.to_thread(
            delete_memory, self._user_id, p.kind, p.key.strip()
        )
        if not deleted:
            return ToolResult(f"没有找到 {p.kind}/{p.key} 这条记忆", is_error=True)
        suffix = f"（原因：{p.reason.strip()}）" if p.reason.strip() else ""
        return ToolResult(f"已删除记忆 {p.kind}/{p.key}{suffix}")

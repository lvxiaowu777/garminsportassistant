"""对话：历史 / 语音 / 文字 / 流式 / WebSocket。

**所有对话都走同一条 Agent 链路**（`garmin_coach.agent`）：
模型自己决定查什么数据、要不要记录、要不要存计划。

| 端点 | 形态 |
|---|---|
| `POST /api/chat` | 非流式，返回 `{reply, recorded}` |
| `POST /api/chat/stream` | SSE，转发 Agent 层事件 |
| `POST /api/chat/voice` | 语音：ASR → 同一个 Agent → 落语音消息 |
| `WS /ws/chat` | 流式，帧格式兼容旧客户端（token / done / error） |
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse

from ..deps import AUDIO_DIR, log, require_user
from ..schemas import ChatRequest

from garmin_coach.agent.loop.events import DoneEvent, TextEvent, to_sse
from garmin_coach.agent.session import session_id_for
from garmin_coach.db.database import (
    add_chat,
    clear_chat_history,
    get_chat_dates,
    get_chat_history,
)
from garmin_coach.services.asr import asr_transcribe

router = APIRouter(tags=["chat"])

# 语音消息允许的容器（Web 录 wav，Android 录 m4a）
AUDIO_SUFFIXES = (".wav", ".m4a", ".mp3", ".webm", ".ogg")


# ── 历史 ──

@router.get("/api/chat/history")
async def api_chat_history(date: str = "", user_id: int = Depends(require_user)):
    """聊天历史，date 可选（YYYY-MM-DD），默认返回最近 200 条。"""
    return get_chat_history(200, user_id, date)


@router.get("/api/chat/dates")
async def api_chat_dates(user_id: int = Depends(require_user)):
    """近 7 天有聊天记录的日期列表（供日期下拉框）。"""
    return get_chat_dates(user_id)


@router.delete("/api/chat/history")
async def api_clear_chat(user_id: int = Depends(require_user)):
    """清空聊天历史（只删记录，不删长期记忆）。

    **同时丢掉内存里的会话**：只删库不清内存的话，模型下一句还记得刚删掉的内容。
    """
    clear_chat_history(user_id)
    from garmin_coach.agent import get_agent_pool

    get_agent_pool().drop(user_id)
    return {"ok": True}


# ── 语音转文字 ──

@router.post("/api/asr")
async def api_asr(file: UploadFile = File(...), user_id: int = Depends(require_user)):
    """语音转文字（StepFun ASR）。"""
    audio_bytes = await file.read()
    text = await asr_transcribe(audio_bytes, file.filename or "audio.m4a")
    if not text:
        raise HTTPException(500, "语音识别失败，请重试")
    return {"text": text}


# ── 语音对话 ──

@router.post("/api/chat/voice")
async def api_chat_voice(
    file: UploadFile = File(...),
    duration_sec: int = Form(0),
    user_id: int = Depends(require_user),
):
    """语音消息：存音频 → ASR → 同一个 Agent 回答。

    语音消息和文字消息的唯一区别是落库时带 `type=voice` + `audio_url`，
    所以这里自己写 user 那一行，Agent 那边 `persist_user=False` 避免重复。
    """
    from garmin_coach.agent import get_agent

    audio_bytes = await file.read()

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        suffix = ".m4a"
    audio_filename = f"voice_{user_id}_{int(dt.datetime.now().timestamp())}{suffix}"
    with open(AUDIO_DIR / audio_filename, "wb") as f:
        f.write(audio_bytes)
    audio_url = f"/static/audio/{audio_filename}"

    text = await asr_transcribe(audio_bytes, file.filename or "audio.m4a")
    if not text:
        raise HTTPException(500, "语音识别失败，请重试")

    try:
        agent = get_agent(user_id)
        result = await agent.collect(
            text,
            user_record={
                "msg_type": "voice",
                "audio_url": audio_url,
                "duration_sec": duration_sec,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    recorded = any(c.name == "record_training" for c in result.tool_calls)
    log.info("[voice] user=%s turns=%s tools=%s", user_id, result.turns, result.tools_used)

    return {
        "text": text,
        "audio_url": audio_url,
        "duration_sec": duration_sec,
        "reply": result.text,
        "recorded": recorded,
    }


# ── 文字（非流式）──

@router.post("/api/chat")
async def api_chat(req: ChatRequest, user_id: int = Depends(require_user)):
    """非流式对话：内部消费 Agent 事件流，返回最终结果。

    落库（user / assistant）和 trace 都由 ``SessionManager`` 负责，
    路由这里不再写数据库。
    """
    from garmin_coach.agent import get_agent

    try:
        result = await get_agent(user_id).collect(req.message)
    except RuntimeError as exc:  # 没有可用 API key
        raise HTTPException(503, str(exc)) from exc

    recorded = any(c.name == "record_training" for c in result.tool_calls)
    log.info(
        "[agent] user=%s turns=%s tools=%s chars=%s",
        user_id, result.turns, result.tools_used, len(result.text),
    )
    return {"reply": result.text, "context": {}, "recorded": recorded}


# ── 文字（流式 SSE）──

@router.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest, user_id: int = Depends(require_user)):
    """流式对话（SSE，第二跳）。

    转发的是 **Agent 层事件**（text / thinking / tool_call / tool_result / done / error），
    不是厂商原始 SSE —— 前端只认这一层，换模型不用改前端。

    用 POST + ``Authorization`` 头（浏览器 EventSource 做不到），
    前端用 ``fetch`` + ``response.body.getReader()`` 消费。
    """
    from garmin_coach.agent import get_agent

    async def gen():
        try:
            async for event in get_agent(user_id).run(req.message):
                payload = json.dumps(to_sse(event), ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except Exception as exc:  # noqa: BLE001 - 流里的错误也要能传到前端
            payload = json.dumps(
                {"type": "error", "message": f"{exc.__class__.__name__}: {exc}"},
                ensure_ascii=False,
            )
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # nginx 不缓冲，否则看不到流
        },
    )


# ── WebSocket（兼容旧客户端的帧格式）──

@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    """流式对话（WebSocket）。

    帧格式保持旧客户端认识的样子：

    - `{"type":"token","token":...}` 逐段正文
    - `{"type":"done","final_reply":...}`
    - `{"type":"error","message":...}`

    内部消费的是 Agent 层事件，所以和 SSE 走同一条链路、同一份落库逻辑。

    注意：`user_id` 仍取自客户端消息体（S1 待修：握手时验 token）。
    """
    from garmin_coach.agent import get_agent

    await ws.accept()
    try:
        while True:
            data = json.loads(await ws.receive_text())
            message = (data.get("message") or "").strip()
            # provider 由后端配置决定（LLM_PROVIDER），客户端传什么都不用
            user_id = data.get("user_id", 0)

            if not message:
                continue

            await ws.send_text(json.dumps(
                {"type": "context", "datetime": dt.datetime.now().strftime("%Y-%m-%d %H:%M")},
                ensure_ascii=False,
            ))

            reply_parts: list[str] = []
            async for event in get_agent(user_id).run(message):
                if isinstance(event, TextEvent):
                    reply_parts.append(event.text)
                    await ws.send_text(json.dumps(
                        {"type": "token", "token": event.text}, ensure_ascii=False
                    ))
                elif isinstance(event, DoneEvent):
                    await ws.send_text(json.dumps(
                        {"type": "done", "final_reply": event.result.text},
                        ensure_ascii=False,
                    ))

    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_text(json.dumps(
                {"type": "error", "message": f"{e.__class__.__name__}: {e}"},
                ensure_ascii=False,
            ))
        except Exception:  # noqa: BLE001 - 连接已断
            pass

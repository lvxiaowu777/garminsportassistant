"""对话：历史 / 语音 / 文字 / WebSocket。

三条链路的现状（重构中）：

- `POST /api/chat` → 新 Agent（`garmin_coach.agent`），异常时回退旧单轮；
- `POST /api/chat/voice` → 旧单轮（`build_chat_context` + `llm_client.chat`）；
- `WS /ws/chat` → 旧单轮流式（`chat_stream`）。

等 B6 把语音/WS 也切到 Agent 之后，`build_chat_context` 与 `markers` 就可以一起删掉。
"""
from __future__ import annotations

import datetime as dt
import json

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

from ..deps import AUDIO_DIR, get_current_user, log
from ..schemas import ChatRequest

from database import (
    add_chat,
    clear_chat_history,
    get_chat_dates,
    get_chat_history,
    record_training,
    save_plan,
)
from garmin_coach.services.chat_context import build_chat_context
from garmin_coach.services.markers import extract_plan, extract_training_record
from llm_client import asr_transcribe, chat_stream

router = APIRouter(tags=["chat"])


@router.get("/api/chat/history")
async def api_chat_history(date: str = "", user_id: int = Depends(get_current_user)):
    """聊天历史，date 可选（YYYY-MM-DD），默认返回最近 200 条。"""
    return get_chat_history(200, user_id, date)


@router.get("/api/chat/dates")
async def api_chat_dates(user_id: int = Depends(get_current_user)):
    """近 7 天有聊天记录的日期列表（供日期下拉框）。"""
    return get_chat_dates(user_id)


@router.delete("/api/chat/history")
async def api_clear_chat(user_id: int = Depends(get_current_user)):
    """清空聊天历史。"""
    clear_chat_history(user_id)
    return {"ok": True}


@router.post("/api/asr")
async def api_asr(file: UploadFile = File(...), user_id: int = Depends(get_current_user)):
    """语音转文字（StepFun ASR）。"""
    audio_bytes = await file.read()
    text = await asr_transcribe(audio_bytes, file.filename or "audio.m4a")
    if not text:
        raise HTTPException(500, "语音识别失败，请重试")
    return {"text": text}


@router.post("/api/chat/voice")
async def api_chat_voice(
    file: UploadFile = File(...),
    duration_sec: int = Form(0),
    user_id: int = Depends(get_current_user),
):
    """语音消息：存音频 + ASR 转文字 + 存语音消息 + AI 回复（旧单轮链路）。"""
    from llm_client import chat as llm_chat

    audio_bytes = await file.read()

    # 1. 存音频文件。扩展名跟随上传的文件（Web 端录的是 wav，Android 是 m4a），
    #    否则浏览器会按错的容器类型解析。
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".wav", ".m4a", ".mp3", ".webm", ".ogg"):
        suffix = ".m4a"
    audio_filename = f"voice_{user_id}_{int(dt.datetime.now().timestamp())}{suffix}"
    audio_path = AUDIO_DIR / audio_filename
    with open(audio_path, "wb") as f:
        f.write(audio_bytes)
    audio_url = f"/static/audio/{audio_filename}"

    # 2. ASR 转文字
    text = await asr_transcribe(audio_bytes, file.filename or "audio.m4a")
    if not text:
        raise HTTPException(500, "语音识别失败，请重试")

    # 3. 存语音消息（content 存 ASR 文字，用户点"转文字"时展示）
    sid = session_id_for(user_id)
    add_chat("user", text, user_id, msg_type="voice", audio_url=audio_url,
             duration_sec=duration_sec, session_id=sid)

    # 4. AI 回复
    ctx = build_chat_context(user_id)
    history = get_chat_history(20, user_id, session_id=sid)
    reply = await llm_chat(text, ctx, history, None)
    clean_reply, record = extract_training_record(reply)
    recorded = record is not None
    if record:
        record_training(
            user_id,
            record.get("type", "strength_training"),
            int(record.get("duration_min", 0) or 0),
            record.get("notes", ""),
            exercises=record.get("exercises"),
        )
        reply = clean_reply or reply
    add_chat("assistant", reply, user_id, session_id=sid)

    return {
        "text": text,
        "audio_url": audio_url,
        "duration_sec": duration_sec,
        "reply": reply,
        "recorded": recorded,
    }


@router.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest, user_id: int = Depends(get_current_user)):
    """流式对话（SSE，第二跳）。

    转发的是 **Agent 层事件**（text / tool_call / tool_result / done / error），
    不是厂商原始 SSE —— 前端只认这一层，换模型不用改前端。

    用 POST + ``Authorization`` 头（浏览器 EventSource 做不到），
    前端用 ``fetch`` + ``response.body.getReader()`` 消费。
    """
    from garmin_coach.agent.bridge import stream_agent_chat
    from garmin_coach.agent.events import to_sse

    async def gen():
        try:
            async for event in stream_agent_chat(
                req.message, user_id, provider_name=req.provider
            ):
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


@router.post("/api/chat")
async def api_chat(req: ChatRequest, user_id: int = Depends(get_current_user)):
    """非流式对话。

    优先走新的带工具 Agent（``garmin_coach.agent``）：模型自己决定查什么数据，
    而不是由服务端把全部数据拼进 prompt。任何环节出问题（没 key、网络、
    协议不兼容）都回退到旧的单轮实现，保证这个接口不会因为重构而不可用。
    """
    from llm_client import chat

    from garmin_coach.agent.session import session_id_for

    sid = session_id_for(user_id)
    history = get_chat_history(20, user_id, session_id=sid)

    agent_result = None
    ctx: dict = {}
    try:
        from garmin_coach.agent.bridge import run_agent_chat

        # 落库（user + assistant）由 SessionManager 统一负责，这里不再写
        agent_result = await run_agent_chat(
            req.message,
            user_id,
            history=history,
            provider_name=req.provider,
        )
        reply = agent_result.text
        log.info(
            "[agent] user=%s sid=%s turns=%s tools=%s chars=%s",
            user_id,
            sid,
            agent_result.turns,
            agent_result.tools_used,
            len(reply),
        )
    except Exception as exc:  # noqa: BLE001 - 重构期的安全网，任何异常都回退
        log.warning("[agent] 失败，回退旧实现: %s: %s", exc.__class__.__name__, exc)
        ctx = build_chat_context(user_id)
        reply = await chat(req.message, ctx, history, req.provider or None)
        # 回退路径不经过 SessionManager，这里自己落库
        add_chat("user", req.message, user_id, session_id=sid)

    # 训练记录：新路径靠 record_training 工具直接落库；
    # 这里仍保留旧的 [RECORD] 标记解析，作为回退路径和模型不听话时的兜底。
    recorded = bool(
        agent_result
        and any(c.name == "record_training" for c in agent_result.tool_calls)
    )
    clean_reply, record = extract_training_record(reply)
    if record:
        record_training(
            user_id,
            record.get("type", "strength_training"),
            int(record.get("duration_min", 0) or 0),
            record.get("notes", ""),
            exercises=record.get("exercises"),
        )
        recorded = True
        reply = clean_reply or reply

    # 未来计划：同上，新路径用 save_plan 工具，旧路径用 [PLAN] 标记
    clean_reply, plan = extract_plan(reply)
    if plan:
        save_plan(user_id, json.dumps(plan, ensure_ascii=False))
        reply = clean_reply or reply

    if agent_result is None:
        # 只有回退路径才需要在这里落 assistant，Agent 路径由 SessionManager 落
        add_chat("assistant", reply, user_id, session_id=sid)

    return {"reply": reply, "context": ctx, "recorded": recorded}


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    """流式对话（旧单轮）。注意：user_id 目前取自客户端消息体，未校验 token。"""
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            message = data.get("message", "")
            provider = data.get("provider", "")
            user_id = data.get("user_id", 0)  # WebSocket 从客户端传入

            if not message.strip():
                continue

            ctx = build_chat_context(user_id)
            sid = session_id_for(user_id)
            history = get_chat_history(20, user_id, session_id=sid)

            add_chat("user", message, user_id, session_id=sid)

            # 先发上下文摘要
            await ws.send_text(json.dumps({
                "type": "context",
                "safety": ctx.get("safety"),
                "datetime": ctx.get("datetime"),
                "period": ctx.get("period"),
            }, ensure_ascii=False))

            # 流式回复
            full_reply = ""
            async for token in chat_stream(message, ctx, history, provider or None):
                full_reply += token
                await ws.send_text(json.dumps({
                    "type": "token",
                    "token": token,
                }, ensure_ascii=False))

            # 解析训练记录标记，落库
            clean_reply, record = extract_training_record(full_reply)
            if record:
                record_training(
                    user_id,
                    record.get("type", "strength_training"),
                    int(record.get("duration_min", 0) or 0),
                    record.get("notes", ""),
                    exercises=record.get("exercises"),
                )
                full_reply = clean_reply or full_reply

            # 提取未来计划 [PLAN]，落库（user_id 隔离）
            clean_reply, plan = extract_plan(full_reply)
            if plan:
                save_plan(user_id, json.dumps(plan, ensure_ascii=False))
                full_reply = clean_reply or full_reply

            add_chat("assistant", full_reply, user_id, session_id=sid)

            await ws.send_text(json.dumps({
                "type": "done",
                "final_reply": full_reply,
            }, ensure_ascii=False))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await ws.send_text(json.dumps({
            "type": "error",
            "message": str(e),
        }, ensure_ascii=False))

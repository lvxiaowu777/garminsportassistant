"""Agent 主循环 —— 对应 MewCode 的 ``mewcode/agent.py``。

规划中的模块：

``loop.py``
    核心 ``run()``：``while True`` 循环，调 LLM → 若有 tool_call 则执行并回填 →
    继续下一轮，直到模型不再要求工具，或到达轮次上限。

``conversation.py``
    ``ConversationManager``：持有消息历史、估算当前 token 数、决定何时压缩。
    对应 ``mewcode/conversation.py``。

``events.py``
    ``AgentEvent``：把循环内部状态（文本增量、工具开始/结束、轮次完成）
    以事件形式吐出去，让 HTTP/WebSocket 层订阅，而不是把回调塞进循环里。

要替代的旧实现：

- ``src/server.py`` 的 ``build_chat_context()`` —— 手写模板把全部数据拼进 prompt
- ``src/llm_client.py`` 的 ``chat_stream()`` —— 单轮调用，没有工具循环

改造后的形态：

    旧：  build_context(全部数据) → LLM → 回答
    新：  LLM ─┬─ tool: get_safety()      ┐
              ├─ tool: get_recent(7)      ├→ LLM → 回答
              └─ tool: get_activity(id)   ┘

模型自己决定查什么，不再由服务端预先猜它需要什么。
"""

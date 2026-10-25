"""LLM 传输层 —— 对应 MewCode 的 ``mewcode/client.py``。

规划中的模块：

``base.py``
    ``LLMClient`` 抽象基类 + ``StreamEvent`` 联合类型。
    MewCode 里 StreamEvent 有 7 种：text / thinking / tool_use_start /
    tool_use_delta / tool_use_end / message_stop / error 之类，
    把不同厂商的 SSE 格式归一化成同一组事件。

``anthropic.py`` / ``openai.py``
    各自把 SSE 解析成 ``StreamEvent``。DeepSeek 走 OpenAI 兼容协议。

``factory.py``
    按配置里的 provider 名字造客户端。

要点：这一层只负责「把我的请求发出去、把流解析成事件」，
**不负责对话历史，也不负责工具执行**。那两件事属于 agent/。

要替代的旧实现：``src/llm_client.py``（19KB，自己手写了两套 HTTP 适配，
且把 PROVIDERS 配置、SYSTEM_PROMPT、context 拼接、ASR 全塞在一个文件里）。
"""

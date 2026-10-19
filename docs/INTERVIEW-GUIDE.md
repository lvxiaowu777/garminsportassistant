# 项目介绍（面试用）

> 这份文档是给你**讲**的，不是给面试官读的。每节末尾的「怎么讲」是提示。
> 所有数字和细节都来自代码和实测，不是估的。

---

## 一、一句话版本

> 一个运动数据聚合 + AI 教练系统：把佳明和高驰手表的数据汇总成健康档案，
> 由一个**能自己调工具查数据**的 Agent 回答「今晚跑不跑」「我这周恢复得怎么样」。
> 后端 FastAPI，前端有原生 Android App 和一个 Web 仪表盘。

**怎么讲**：先说业务，再说技术。面试官不关心你用了什么框架，关心你做的东西解决了什么问题。

---

## 二、业务背景与真正的问题

用户（或者说这个产品的使用者）是一个半马训练者，每天戴着 Garmin 手表。
手表数据散落在官方 App 里，想知道「今晚该不该跑」需要自己翻睡眠、HRV、
静息心率、最近跑量、天气，再自己判断。

所以我做了两件事：

1. **数据聚合**：定时从佳明/高驰拉数据，落进数据库，结构化存储
   （睡眠分项、HRV、心率区间、每公里分段、天气、AQI…）
2. **AI 教练**：一个能对话的教练，回答训练问题

第 2 件事才是难点。**第一版做完之后发现它根本不算「AI 教练」，只是个
高级模板填充**——这个问题后面第三节详细讲。

---

## 三、核心改造：从「单轮 RAG」到「工具调用 Agent」

### 改造前的样子

对话接口是这么工作的：

```python
def build_chat_context(user_id):
    # 110 行手写模板，把能想到的数据全拼成字符串
    parts.append(f"当前时间: {now}")
    parts.append(f"安全档位: {safety['max_tier']}")
    parts.append(f"HRV: {recovery['hrv']}")
    # ... 今日数据、7 天汇总、逐日明细、标准动作名、训练计划 ...
    return "\n".join(parts)

system_msg = SYSTEM_PROMPT + "\n\n以下是用户的健康数据：\n\n" + context
reply = await chat(message, system_msg, history)   # 单次调用
```

问题非常具体：

| 问题 | 后果 |
|---|---|
| 每次提问都塞全量数据 | 哪怕只问「昨天跑了多少」，也要付几千 token |
| 模型没有查询能力 | 追问「那次每公里配速多少」答不了，因为分段数据没拼进去 |
| 想让它看新数据 | 得改那 110 行的拼接代码，加一个字段就动一处逻辑 |
| 数据量和 token 成正比 | 数据越攒越多，每次对话越贵 |

**本质上这是 RAG 的一个退化形态**：不是检索，是把所有可能的上下文一次性灌进去。
它不需要模型思考，只需要模板拼接。

### 改造后的样子

```
用户提问
  → Agent.run()
      ├─ 第 1 轮：模型决定调 get_day({"date": "2026-08-09"})
      ├─ 执行工具，把结果作为 tool 消息塞回对话
      ├─ 第 2 轮：模型看到活动 id，调 get_activity({"activity_id": "626258316"})
      ├─ 执行工具，回填分段配速与心率区间
      └─ 第 3 轮：模型不再要工具，输出分析
```

模型自己决定查什么、查几次。数据不在 prompt 里，在数据库里，按需取。

**这是一次架构升级，不是一次优化。** 区别在于：旧方案的能力边界由那段拼接代码决定，
新方案的能力边界由工具集决定——模型可以组合工具完成设计时没想到的查询。

**怎么讲**：重点讲「为什么旧的不能算 Agent」。很多人会把 RAG 叫成 Agent，
你要能说清楚区别：Agent 有决策循环，RAG 没有。

---

## 四、技术栈

### 后端

| 层 | 选型 | 为什么 |
|---|---|---|
| Web 框架 | FastAPI 0.146 + uvicorn | 异步原生、pydantic 集成、自带 OpenAPI 文档 |
| ORM | SQLAlchemy 2.1 | 支持 SQLite/MySQL 双方言，本地开发不依赖 MySQL |
| 数据校验 | pydantic v2 | 同一套模型既做请求校验，也导出成 LLM 的工具 JSON Schema |
| 鉴权 | PyJWT（HS256）+ PBKDF2-SHA256 | 密码 10 万轮加盐哈希，无外部依赖 |
| 异步 HTTP | httpx | 支持流式响应（`client.stream` + `aiter_lines`） |
| 定时任务 | APScheduler | 每天定时同步手表数据 |
| 手表 SDK | garminconnect | 佳明官方协议封装，含 MFA 两步验证 |
| 推送 | firebase-admin | **可选依赖**，没装时自动降级 |
| LLM | DeepSeek（OpenAI 兼容协议） | 可切换 StepFun / 火山 Ark，一个实现覆盖三家 |

### 前端

| 项 | 选型 |
|---|---|
| 语言/UI | Kotlin + Jetpack Compose（纯原生，不套 WebView） |
| 网络 | OkHttp |
| 推送 | Firebase Cloud Messaging |
| 架构 | ViewModel + 单向数据流 |

**怎么讲**：技术栈不要报菜名。挑 2-3 个说选型理由，比如「为什么用 pydantic 模型
同时做校验和工具 schema 导出」——这是有信息量的细节。注意 `GarminApi.kt` 有 32KB，
是纯手写 HTTP 客户端不用 Retrofit，可以说明「为了少一个依赖」，但也要承认 Retrofit 更标准。

---

## 五、Agent 设计详解（这是重点）

代码在 `garmin_coach/`，约 1300 行，分五层。

```
garmin_coach/
├── llm/          传输层：把各厂商 SSE 归一化成统一事件
├── tools/        工具层：模型能调用的能力
├── agent/        Agent 层：决策循环
├── services/     领域服务：安全档位、训练阶段判定
└── db/           数据访问（过渡期桥接旧 database.py）
```

### 5.1 传输层：为什么要把 SSE 归一化成事件

不同厂商的流式响应格式完全不同。DeepSeek（OpenAI 协议）长这样：

```json
{"choices":[{"delta":{"content":"你好"}}]}
{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","function":{"name":"get_day","arguments":"{\"date\":"}}]}}]}
```

Anthropic 协议又是另一套。如果 Agent 直接解析这些，换一个厂商就要改循环逻辑。

所以我定义了 7 种事件，把差异挡在传输层里：

```python
TextDelta(text)                  # 正文增量
ThinkingDelta(text)              # 思考增量（推理模型）
ToolUseStart(id, name)           # 开始一个工具调用
ToolUseDelta(id, partial_json)   # 工具入参的 JSON 碎片
ToolUseEnd(id, name, arguments)  # 入参完整，可以执行了
MessageStop(stop_reason, usage)  # 本轮结束
StreamError(message)             # 错误作为事件返回，不抛异常
```

上层只 `async for event in client.stream(...)`，不关心底层是哪个厂商。

**这里有一个真实的技术难点值得讲**：工具调用的入参是**分片吐**的。
`function.arguments` 不是一个完整 JSON，而是被切成好几个字符串碎片，
要靠 `index` 字段归位、自己拼起来。我第一版没处理这个，直接 `json.loads` 就崩了。
现在的做法是在 `pending: dict[int, dict]` 里累积，流结束后统一解析，
解析失败就把错误扔回给模型（而不是 500）。

**怎么讲**：「流式协议里工具调用是增量的」这句话本身就能体现你真的写过。
没写过的人会以为一次拿到完整 JSON。

### 5.2 工具层：9 个工具 + 三个设计决策

工具清单（全部复用旧的 `database.py` 查询函数，不重写 SQL）：

| 工具 | 用途 |
|---|---|
| `get_safety` | 安全档位（训练强度上限），**任何训练建议前必须先调** |
| `get_today` | 今天的恢复、睡眠、天气、已做的运动 |
| `get_recent(days)` | 最近 N 天逐日数据 |
| `get_day(date)` | 指定日期 |
| `get_activity(id)` | 单次活动的分段配速、心率区间 |
| `get_weather(date)` | 天气 |
| `get_profile` | 训练目标、比赛倒计时、当前训练阶段 |
| `record_training(...)` | 写训练记录 |
| `save_plan(...)` | 保存未来训练计划 |

三个值得讲的决策：

**① 工具执行失败不抛异常，转成反馈交给模型**

```python
except ValidationError as e:
    return ToolResult(content=f"参数校验失败：{e}", is_error=True)
except Exception as e:
    return ToolResult(content=f"工具 {name} 出错：{e}", is_error=True)
```

「活动 id 不存在」这种错误本质上是给模型的一次反馈，让它换个参数重试，
比直接 500 给用户看要好。这是 Agent 系统和普通 API 的一个根本区别——
普通 API 的错误是给调用者的，Agent 的错误是给模型的。

**② 同步 IO 必须丢到线程池**

`database.py` 是同步 SQLAlchemy。如果在协程里直接调用，**一个慢查询会阻塞
整个事件循环**，所有人的请求都卡住。所以工具内部统一用 `asyncio.to_thread`：

```python
async def _in_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
```

这是「异步框架 + 同步库」的经典接缝，面试里经常被问。

**③ 工具必须绑定 user_id**

HTTP 路由有 `Depends(get_current_user)` 兜着身份，但工具是 Agent **内部**调用的，
没有这层保护。如果工具不绑 user_id，多用户场景下模型可能读到别人的健康数据。
这是个很容易漏的安全点，因为工具层看起来「不接触用户输入」。

**怎么讲**：这三个点任一个展开都能聊 3-5 分钟，选最有把握的。
「工具错误给模型而不是给用户」这个观点比较能体现你对 Agent 范式的理解。

### 5.3 Agent 循环

```python
for turn in range(1, max_turns + 1):
    async for event in llm.stream(messages, system=..., tools=schemas):
        # 收集文本增量、工具调用
    if not pending_calls:
        break                      # 模型不要工具了 → 就是最终答案
    conv.add_assistant_tool_calls(text, pending_calls)
    for call in pending_calls:
        result = await registry.execute(call.name, call.arguments)
        conv.add_tool_result(call.id, result.content)
```

核心逻辑很短，但有三个必须处理的细节：

**① 消息序列的合法性**

OpenAI 协议要求：带 `tool_calls` 的 assistant 消息后面**必须紧跟**对应的 tool 消息，
少一条就 400。所以不能「随便 append 一个 dict」，只能通过语义化方法写入。
`ConversationManager` 只暴露 `add_assistant_tool_calls` / `add_tool_result`，
不提供裸的 append 入口。这是用类型/接口约束协议正确性。

**② 轮次上限**

`max_turns=8`。不设上限会出问题——实测中模型曾在两个工具之间来回试探。
到上限就带着已有文本收尾，而不是报错。

**③ token 用量要累加**

一轮对话可能有 4 次 LLM 调用，每次都有 usage。只看最后一次会严重低估成本。
（这个我第一版写错了，用 `usage = event.usage` 覆盖，后来改成累加。）

### 5.4 System Prompt 的设计

800 行代码里最不「工程」但最有价值的部分。

改造后 prompt 有一个明确的转变——**从「给你数据，你回答」变成「给你工具，你查」**：

```
## 数据获取方式（重要，和以前不一样）
你看不到任何数据，除非主动调工具查。不要凭猜测回答数据类问题。

调用原则：
- 简单寒暄、不带数据的问题，直接回答，不要为了显得勤奋而乱调工具
- 涉及身体状态、训练量、具体某次运动 —— 必须查，不能编
- 需要多个信息时，一次把要调的工具都调了，不要一轮一轮挤
```

最后那条「一次把要调的工具都调了」是实测加上的：不加的话模型会一轮查一个，
3 次往返才能拿完数据，慢且贵。

另外 prompt 里保留了全部领域规则（安全档位五档定义、训练阶段划分、
决策规则、观察点）。**这些是业务知识，和架构无关，重构中不该丢**。

**怎么讲**：prompt 工程不是「写咒语」。可讲的是：如何用 prompt 控制工具调用频率、
如何让模型区分「该查」和「不该查」、安全约束怎么写成硬规则。

---

## 六、关键权衡（面试官爱问「为什么不 X」）

### 为什么只重构对话这条线，不动其他 38 个接口？

其余路由是纯 CRUD（绑定手表、查历史、改档案）。它们不需要 LLM，
套上 Agent 架构是过度设计。**分层要为问题服务，不是为架构图服务。**

如果面试官追问「那你的分层有什么意义」，答：分层的收益在「同一条逻辑被多方复用」
和「变化频率不同的东西隔开」。安全档位计算被 HTTP 和工具同时调用，
所以它必须独立成 services 层——否则会出现「网页显示 EASY、AI 说 HARD」。

### 为什么用绞杀者模式而不是推倒重写？

后端已经在服务器上跑真实数据。推倒重写意味着一段时间没有可用服务，
而且容易漏掉线上才有的行为（脏数据、边界情况）。新旧并存、逐个模块迁移，
每一步都可回滚。

具体做法：旧代码留在 `src/` 继续跑，新代码写进 `garmin_coach/`，
通过 `db/legacy.py` 一个桥接文件把旧模块接进来。将来搬家只改那一个文件。

### 为什么保留旧实现做回退？

`/api/chat` 里：

```python
try:
    result = await run_agent_chat(...)     # 新路径
except Exception:
    ctx = build_chat_context(user_id)      # 旧路径兜底
    reply = await chat(...)
```

这不是懒。**实测它就救了一次场**：部署后发现 `garmin_coach` 导入失败
（`python src/server.py` 时项目根不在 `sys.path`），回退机制正常兜住，
用户完全没感知。修完 `sys.path` 才切到新路径。

### 为什么训练记录从「输出标记」改成「调工具」？

旧版让模型在回复末尾输出 `[RECORD]{...}[/RECORD]`，服务端用正则解析。
这是三个脆弱环节串联：模型要记得输出 → 格式要对 → 正则要匹配上。

新版直接 `record_training` 工具，路由层从 `tool_calls` 里判断有没有记录过。
少一层解析，也多了一份结构化日志。

---

## 七、踩到的真实问题（这些最能体现工程能力）

### 问题 1：模型拿不到 activity_id，连续两次查询失败

第一版工具的 `_fmt_activity_brief` 输出活动摘要时没有带 `activity_id`：

```
running 5.01km 33.0min 配速6'35" 心率158/175
```

模型想查详情，但没有 id，于是**把日期当 id 去猜**：

```
工具调用: get_activity({"activity_id": "2026-08-09"})  → 找不到活动
工具调用: get_activity({"activity_id": "20260809"})    → 找不到活动
```

结果它对用户说「活动 ID 检索失败，隔太久了」。

补上 `id=626258316` 之后，模型一次就串起来了。

**这个问题的价值**：它说明工具的输出格式是 Agent 设计的一部分，不是随手 format。
模型只能看到你给的东西，漏一个关键字段它就断了链条。
而且这个 bug **不会报错**，只会表现为「回答得不好」，很难通过测试发现。

### 问题 2：异步框架里调同步库

`database.py` 是同步 SQLAlchemy。第一版工具直接在协程里调用，
意味着一个慢查询阻塞整个事件循环。改成 `asyncio.to_thread` 之后才是对的。

### 问题 3：流式响应里工具调用是分片的

前面讲过，不再重复。要点是：不能假设 `arguments` 是完整 JSON。

---

## 八、可量化的结果

| 指标 | 数值 |
|---|---|
| 新增 Agent 架构代码 | 约 1300 行（分 5 层 20 个文件） |
| 工具数量 | 9 个 |
| 一次完整对话（3 轮，2 次工具调用）实测 | 单次调用 prompt ~3.9k tokens，其中 prompt cache 命中 3.7k |
| 旧方案单次对话 | 固定携带全量上下文，与数据量成正比 |
| 重构前后接口契约 | 完全兼容，Android 端零改动 |

**关于 prompt cache**：system prompt 和工具 schema 固定在最前面，DeepSeek 的
前缀缓存能命中。实测 3872 prompt tokens 里 3712 是缓存命中，只有 160 是新增。
这一点在讲「成本优化」时可以提，但要说清楚是服务端的缓存机制，不是你实现的。

---

## 九、面试官可能追问的问题

**Q: 你这个 Agent 和 LangChain 有什么区别？**
A: 没用框架，手写的循环。理由是核心逻辑只有 40 行（`agent/loop.py`），
用框架反而要理解框架。而且我需要控制的地方（工具错误处理、消息序列合法性、
token 累加）框架都不一定管。如果是复杂多 Agent 编排我会考虑框架。

**Q: 为什么工具调用失败要返回给模型而不是抛异常？**
A: 因为 Agent 的错误是给模型的反馈。抛异常等于终止对话，返回 is_error 等于
给模型一次自我修正的机会。实测模型拿到「参数校验失败」后通常能自己改对。

**Q: 上下文超长怎么办？**
A: 目前靠 `max_turns` 限制和工具返回的紧凑格式化（工具输出是格式化文本不是
原始 JSON，这一步省了很多 token）。没做自动压缩。如果要做，思路是消息分组 +
摘要替换，把早期轮次压成一段摘要保留结论。

**Q: 怎么保证模型不瞎编数据？**
A: 三层。① prompt 明确写「涉及身体状态、训练量必须查，不能编」；
② 工具返回里把日期、数值都结构化带上，模型有据可依；
③ 安全档位是硬约束，prompt 里写成「必须先调 get_safety」，
且档位定义是服务端规则引擎算的，不是模型判断的。

**Q: 多用户怎么隔离？**
A: JWT 解出 user_id，一路传下去。数据库每张表都有 user_id 字段，
工具绑 user_id 构造，查询全部按 user_id 过滤。工具层是容易漏的点，
因为 HTTP 路由有 `Depends` 兜着，工具是内部调用没有这层。

**Q: 这个项目最难的地方？**
A: 不是写 Agent 循环，是**从「能跑」到「好用」的那一段**。
比如 activity_id 那个 bug，代码不报错、测试也过，但模型就是答不好。
要去看日志、看模型调了什么工具、看工具返回了什么，才能定位。
这类问题的调试方法跟传统 bug 完全不同。

---

## 十、代码走读路径（面试官说「给我看代码」）

按这个顺序讲，20 分钟能过一遍主链路：

1. `src/server.py` 的 `api_chat` —— 入口，看新旧路径如何切换
2. `garmin_coach/agent/bridge.py` —— 装配：LLM + 工具 + Agent
3. `garmin_coach/agent/loop.py` 的 `Agent.run` —— **核心 40 行**
4. `garmin_coach/agent/conversation.py` —— 消息序列合法性
5. `garmin_coach/llm/openai.py` —— 流式解析 + 工具调用分片拼装
6. `garmin_coach/tools/health.py` 挑一个工具 —— 线程池、错误处理、格式化
7. `garmin_coach/tools/registry.py` —— 工具注册与执行

**如果只有 3 分钟**：讲第 3 步（Agent.run 的循环）和第 7 步（工具执行），
这两个能撑起「你懂 Agent 原理」的判断。

---

## 十一、还能改进的地方（主动说，显得有判断）

1. **没有流式输出到前端**。传输层已经支持事件流，但 `/api/chat` 还是等全部
   完成才返回。`Agent.run` 里有 `on_text` 回调预留了接口，接 WebSocket 就能做打字机效果。
2. **没有自动上下文压缩**。对话长了会超窗口，目前靠 `max_turns` 和历史上的
   `get_chat_history(20)` 限制。
3. **工具集是静态的**。所有用户拿同样的 9 个工具。可以按训练目标
   （半马/全马/塑形）裁剪，减少 schema 占用。
4. **没有评测集**。Agent 的效果现在靠人工看回复质量判断，没有自动化回归。
   理想做法是构造一批「问题 + 期望调用的工具」样本做评测。
5. **单进程单实例**。定时同步和 Web 服务在一个进程里，scale 不出去。

---

## 附：数据模型（被问到存储时）

```
users            id, phone, password_hash(PBKDF2), 档案字段(身高体重目标...)
watch_bindings   user_id, brand, account, auth_token, enabled
                 UNIQUE(user_id, brand)
health_daily     (date, user_id, brand) 联合主键
                 睡眠 5 项 / 恢复 6 项 / 训练状态 / 天气 9 项
activities       UNIQUE(user_id, brand, activity_id)
                 距离配速心率 / 心率区间 JSON / 每公里分段 JSON / 训练效果
strength_sets    activity_id, 动作名, 组数, 次数, 重量, 器械
chat_history     user_id, role, content, type(text/voice), audio_url
daily_plans      user_id, date, plan_json
```

双品牌（佳明/高驰）用 `brand` 字段区分，联合主键/唯一键都带上，
所以同一用户在两个品牌下的同一天数据不会互相覆盖。

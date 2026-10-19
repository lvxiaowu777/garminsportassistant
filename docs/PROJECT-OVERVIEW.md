# Garmin Coach 项目详解

> 这份文档描述的是项目**现在的样子**：它由哪些部分构成、每部分为什么这么设计、
> 数据怎么流动。不讲改造历史。

---

## 一、这个项目是什么

一个**运动数据聚合 + AI 教练**系统。

使用者是一个跑半马的人，每天戴 Garmin 手表。手表数据散在官方 App 里，想知道
「今晚该不该跑」得自己翻睡眠、HRV、静息心率、最近跑量、天气，再自己判断。

这个项目把两件事做掉了：

1. **数据聚合**：定时从佳明（Garmin）和高驰（COROS）把数据拉下来，结构化存进数据库
2. **AI 教练**：一个能自己查数据再回答问题的对话助手

从使用者视角看，交互是这样的：

```
「今晚跑不跑？」
  → 助手去查今天的 HRV 和睡眠、最近 7 天跑量、当前天气
  → 按安全档位规则判断今天最多能练到什么强度
  → 回答："今天 HRV 比 7 天均值低 22%，安全档位降到 EASY，
           只能轻松跑，心率压 140 以下、40 分钟以内。"
```

关键在「它自己去查」——不是把一堆数据预先塞给模型，而是给模型一套工具让它按需查询。

---

## 二、整体架构

```
┌──────────────────────────────────────────────────────────┐
│ 客户端                                                    │
│  ├── Web 仪表盘    static/index.html（单文件，无构建）      │
│  └── Android App   Kotlin + Jetpack Compose               │
└───────────────────────┬──────────────────────────────────┘
                        │ HTTP / JSON（JWT 鉴权）
┌───────────────────────▼──────────────────────────────────┐
│ 接口层  FastAPI（约 40 个路由）                            │
│   /api/auth  /api/watch  /api/health  /api/chat  /api/push │
└───────┬────────────────────────────┬─────────────────────┘
        │                            │
┌───────▼──────────────┐  ┌──────────▼─────────────────────┐
│ Agent 层（对话专用）   │  │ 服务层（规则与业务编排）         │
│ ├── Agent 主循环      │  │ ├── 安全档位引擎                 │
│ ├── 工具层（9 个）     │  │ ├── 训练阶段判定                 │
│ ├── LLM 传输层        │  │ ├── 手表同步编排                 │
│ └── 对话历史管理       │  │ └── 天气与推送                   │
└───────┬──────────────┘  └──────────┬─────────────────────┘
        │                            │
┌───────▼────────────────────────────▼─────────────────────┐
│ 数据访问层  SQLAlchemy ORM                                │
└───────────────────────┬──────────────────────────────────┘
                        │
              ┌─────────▼──────────┐
              │ SQLite / MySQL      │
              └─────────▲──────────┘
                        │ 写入
┌───────────────────────┴──────────────────────────────────┐
│ 同步层  APScheduler 定时任务                               │
│  ├── garminconnect（佳明官方 API，含 MFA）                 │
│  ├── COROS 非官方 Web API                                 │
│  └── 和风天气 API                                         │
└──────────────────────────────────────────────────────────┘
```

代码落位：

| 目录 | 内容 |
|---|---|
| `garmin_coach/llm/` | LLM 传输层：把各家厂商的流式协议归一化成统一事件 |
| `garmin_coach/tools/` | 工具层：9 个工具的定义、注册表、执行 |
| `garmin_coach/agent/` | Agent 主循环、对话历史、系统提示词 |
| `garmin_coach/services/` | 领域规则：安全档位、训练阶段 |
| `garmin_coach/db/` | 数据访问（过渡期桥接 `src/database.py`） |
| `src/server.py` | FastAPI 应用与全部路由 |
| `src/database.py` | 所有 SQLAlchemy 查询与写入 |
| `src/models.py` | ORM 表定义 |
| `src/garmin_server_sync.py` | 佳明数据同步 |
| `src/coros_server_sync.py` | 高驰数据同步 |
| `src/auth.py` | 密码哈希与 JWT |
| `static/index.html` | Web 仪表盘 |

---

## 三、数据从哪来：Garmin 官方数据接入

### 3.1 用到的开源库

**[`garminconnect`](https://github.com/cyberjunky/python-garminconnect)** —— 一个 Python 的开源 Garmin
客户端库，封装了佳明官方 API 的登录、鉴权和数据接口。

佳明官方没有公开的 REST API 文档，社区这个库是逆向官方 App/网页的接口实现出来的，
目前是 Python 生态里接入佳明数据最常用的方案。它提供：

- `Garmin(email, password, is_cn=True)` —— 登录，`is_cn=True` 表示用中国区账号
- `return_on_mfa=True` —— 支持两步验证：第一次 `login()` 返回 `needs_mfa`，
  再用验证码调 `resume_login()` 完成
- `client.dump(目录)` —— 把登录后的 token 存到磁盘，下次免密复用
- 各种 `get_*` 方法拿睡眠、HRV、活动、心率区间等

项目里对它的封装在 `src/garmin_server_sync.py`：

```python
g = Garmin(email, password, is_cn=True, return_on_mfa=True)
mfa_status, _ = g.login()
if mfa_status == "needs_mfa":
    # 把 Garmin 对象暂存起来（resume_login 依赖对象内部状态，必须同一实例）
    _PENDING_MFA[user_id] = {"garmin": g, "account": email}
    return True, None        # 告诉前端：需要输入验证码
g.client.dump(token_dir(user_id))   # 存 token
```

之后每次同步只要：

```python
g = Garmin(is_cn=True)
g.login(str(_token_dir(user_id)))    # 读 token 免密登录
```

一个实现细节：**MFA 登录必须保持同一个 `Garmin` 对象实例**。`resume_login` 不是无状态的，
它依赖对象内部已经建立的会话状态。所以项目用 `_PENDING_MFA: dict[user_id, dict]`
暂存这个对象，并用 `threading.Lock` 保护（FastAPI 的同步路由可能在不同线程上）。

### 3.2 高驰（COROS）

COROS 没有官方开放 API，也无可用的成熟开源库，项目里是用它的**非官方 Web API** 直接对接的
（`src/coros_server_sync.py`）。登录方式是邮箱 + 区服，数据结构和佳明不完全一样，
所以同步逻辑是两套独立的实现，但最终写进同一批表。

### 3.3 同步怎么触发

| 方式 | 说明 |
|---|---|
| 定时 | APScheduler，每天 **02:07** 同步佳明、**02:17** 同步高驰，各拉最近 7 天 |
| 手动 | `POST /api/health/sync`，用户在 App 里点"同步" |
| 关停 | 用户可以在设备页关掉某个品牌，`get_watch_enabled()` 为假时跳过 |

同步流程（`sync_user_garmin`）：

```python
for i in range(days):                      # 从今天往前 days 天
    d = (today - timedelta(days=i)).isoformat()
    data = fetch_day(g, d)                 # 拉这一天的全量数据
    upsert_health_daily(data, user_id, "garmin")   # 幂等写入
```

单天失败只打日志、继续下一天，最后返回成功天数——一天的失败不应该让整次同步作废。

### 3.4 一个必须理解的特性：数据新鲜度

AI 回答「今天状态怎么样」时，读的是**数据库中最近一次同步的快照**，不是实时数据。

用户刚跑完步、手表还没同步时，AI 是不知道的。这也是为什么 `get_today` 工具在没有当天
数据时会明确返回「可能今天还没同步手表」，而不是编造一个结果。

这是所有「离线聚合 + AI 查询」架构的固有特性，不是缺陷。

---

## 四、数据怎么存

### 4.1 表结构

```
users
  id / phone / password_hash(PBKDF2)
  档案字段：height_cm, weight_kg, age, gender, body_fat_pct, muscle_mass_kg,
            goal, goal_target, training_years, training_freq_per_week,
            training_types, races(JSON), last_sync_date

watch_bindings                     一对用户可绑多个品牌的表
  id / user_id / brand / account / auth_token / token_expires / enabled
  UNIQUE(user_id, brand)

health_daily                       一天一行，宽表
  PK(date, user_id, brand)
  睡眠 6 项 / 恢复 6 项 / 训练状态 6 项 / 天气 6 项 / 实时天气 4 项 / 步数活动 4 项
  notes(自由文本)

activities                         一次运动一行
  id / user_id / brand / activity_id / date / type / time / location
  距离/时长/配速/心率/卡路里/训练效果/步频/爬升/温度
  hr_zones(JSON)  splits(JSON)
  UNIQUE(user_id, brand, activity_id)

strength_sets                      力量训练的动作明细
  activity_id / name / sets / reps / reps_detail / weight_kg / equipment

chat_history                       对话历史
  user_id / role / content / type(text|voice) / audio_url / duration_sec

daily_plans                        AI 生成的未来训练计划
  user_id / date / plan_json

device_tokens                      推送设备
  token(PK) / platform / last_seen
```

### 4.2 三个设计决策

**① 双品牌用 `brand` 字段隔离，而不是两套表**

佳明和高驰的数据字段高度相似（都有睡眠、HRV、活动），不值得维护两套模型。
联合主键/唯一键都带上 `brand`：

```
health_daily   PK (date, user_id, brand)
activities     UNIQUE (user_id, brand, activity_id)
```

这样同一用户同一天在两个品牌下的数据不会互相覆盖，切换品牌只是换一个
`get_active_brand()` 的返回值，上层查询代码不用改。

**② `health_daily` 用宽表**

睡眠、恢复、训练状态、天气几十个字段全塞一张表，而不是拆成
`sleep` / `recovery` / `weather` 多张。

理由：这些数据是**同一天同一时刻一起产生的**，读取时也几乎总是一起读
（仪表盘、AI 的工具都是"给我这一天"）。宽表一次查询就能拿全，
拆表要 join 四次，收益是零。

代价是列多、改动要加 ALTER。项目里有 `_ensure_health_columns()` 这类
幂等的补列函数，启动时把缺的列补上，算是缓解。

**③ 所有写入都是 upsert（幂等）**

同步是每天跑的，同一天的数据会重复写入。所以全部走 `upsert`：

```python
_upsert(HealthDaily, cols, ["date", "user_id", "brand"])
```

同一 (date, user_id, brand) 重复写入只会更新，不会产生重复行。

这里有个跨方言的处理值得注意：项目要同时支持 MySQL（服务器）和 SQLite（本地开发），
但两家的 upsert 语法不同：

- MySQL：`INSERT ... ON DUPLICATE KEY UPDATE`
- SQLite：`INSERT ... ON CONFLICT (...) DO UPDATE SET`

所以 `database.py` 里有一层按 `engine.dialect.name` 分派的辅助函数
（`_upsert` / `_insert_stmt` / `_append_concat`），业务代码只写一次。
字符串拼接同理：MySQL 用 `func.concat()`，SQLite 要用 `||`。

---

## 五、Agent 是怎么工作的

这是项目最核心的部分。

### 5.1 一次对话的完整生命周期

```
1. 客户端 POST /api/chat  {"message": "今晚跑不跑？"}
        │
2. FastAPI 路由：JWT 解出 user_id，取最近 20 条历史
        │
3. bridge.run_agent_chat(message, user_id, history)
        │
        ├─ 造 LLM 客户端（按 provider 配置）
        ├─ 造工具注册表（9 个工具，全部绑定 user_id）
        └─ 造 Agent 并调用 run()
        │
4. Agent.run()：
        │   messages = [历史...] + [当前问题]
        │
        ├─ 第 1 轮：把 messages + 工具 schema 发给 LLM，流式接收
        │     模型回复：我要调 get_safety({}) 和 get_today({})
        │     → 执行工具，把结果作为 role=tool 的消息塞回 messages
        │
        ├─ 第 2 轮：再次发给 LLM
        │     模型回复：我要再调 get_recent({days: 7})
        │     → 执行，回填
        │
        ├─ 第 3 轮：模型不再要求工具，返回最终文本
        │
5. 路由层：把 AI 回复写进 chat_history，返回 {"reply": ..., "recorded": bool}
        │
6. 前端渲染气泡
```

### 5.2 决策循环：为什么这算 Agent

核心是一个循环，逻辑不到 40 行（`garmin_coach/agent/loop.py`）：

```python
for turn in range(1, max_turns + 1):
    async for event in llm.stream(messages, system=..., tools=schemas):
        # 收集正文增量、收集工具调用
    if not pending_calls:
        break                      # 模型不要工具了 → 这就是最终答案
    conversation.add_assistant_tool_calls(text, pending_calls)
    for call in pending_calls:
        result = await registry.execute(call.name, call.arguments)
        conversation.add_tool_result(call.id, result.content)
```

**和 ReAct 的关系**

ReAct（Reasoning + Acting）是这类架构的经典范式，原始论文里的形式是让模型输出
交错的文本：

```
Thought: 我需要知道今天的 HRV 和睡眠
Action: get_safety()
Observation: 安全档位 EASY，HRV 比 7 日均值低 22%
Thought: 那今天不能做强度课
Action: ...
```

这个项目的循环结构**和 ReAct 是同一个**：决策 → 执行 → 观察 → 再决策。

区别在于「决策」的**表达方式**：不是输出自然语言文本再去正则解析，
而是用模型的 **function calling 能力输出结构化调用**：

```json
{"tool_calls": [{"id": "call_x", "function": {"name": "get_safety", "arguments": "{}"}}]}
```

这么做的收益和代价：

| | 结构化 function calling | 文本解析式 ReAct |
|---|---|---|
| 可靠性 | 高，参数是 JSON、按 schema 校验 | 低，要处理格式漂移、解析失败 |
| 可校验 | 可以 pydantic 校验入参 | 只能靠正则，错了难兜 |
| 依赖 | 需要模型支持 function calling | 任何模型都能跑 |

所以更准确的说法是：**这是一个 tool-calling Agent，循环范式属于 ReAct 家族，
但决策用结构化输出而非文本推理**。

### 5.3 工具层：Agent 的手脚

一共 9 个工具，全部在 `garmin_coach/tools/health.py`：

| 工具 | 作用 | 什么时候该调 |
|---|---|---|
| `get_safety` | 当前安全档位（训练强度上限）与降档原因 | **给任何训练建议前必须先调** |
| `get_today` | 今天的状态、睡眠、天气、今天已做的运动 | 问"今天状态怎么样""今晚能不能跑" |
| `get_recent(days)` | 最近 N 天逐日数据（恢复、睡眠、每天的运动） | 问"这周跑量""最近恢复得怎么样" |
| `get_day(date)` | 指定日期的详细数据 | 用户提到具体日期 |
| `get_activity(activity_id)` | 单次活动的分段配速、心率区间、训练效果 | 问"那次跑步每公里配速多少" |
| `get_weather(date)` | 天气（温度、湿度、AQI、降水概率） | 安排户外训练 |
| `get_profile` | 训练目标、未来比赛倒计时、当前训练阶段 | 做中长期安排前 |
| `record_training(...)` | 把一次训练写进记录 | 用户明确说"我练了什么" |
| `save_plan(...)` | 保存未来几天的训练计划 | 用户明确要"安排未来几天" |

**每个工具的三个要素**（`tools/base.py`）：

```python
class Tool(ABC):
    name: str                        # 模型看到的名字
    description: str                 # 给模型读的说明书
    params_model: type[BaseModel]    # pydantic 模型，既做校验也导出 JSON Schema

    def schema(self) -> dict:
        """导出成 OpenAI function calling 需要的样子"""
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.params_model.model_json_schema(),
        }}

    async def execute(self, params) -> ToolResult: ...
```

用 pydantic 模型同时承担两件事——运行时校验入参、导出给模型的 JSON Schema——
是个很划算的设计：只写一遍，两边都不会漂移。

**`description` 的写法很关键**。模型选不选一个工具，主要看这句说明书，而不是参数定义。
所以每个 description 都刻意写了两件事：这个工具返回什么、**什么场景该调它**。
比如 `get_safety` 写的是「给任何训练建议前必须先调」——这是业务约束，必须显式写进去。

**工具执行失败的处理**

```python
async def execute(self, name, arguments) -> ToolResult:
    tool = self._tools.get(name)
    if tool is None:
        return ToolResult(f"没有名为 {name} 的工具…", is_error=True)

    try:
        params = tool.params_model(**arguments)
    except ValidationError as e:
        return ToolResult(f"参数校验失败：{e}", is_error=True)

    try:
        return await tool.execute(params)
    except Exception as e:
        return ToolResult(f"工具 {name} 出错：{e}", is_error=True)
```

**工具失败不抛异常，而是转成 `is_error` 的结果交还给模型。**

这是 Agent 和普通 API 的一个本质区别：

- 普通 API 的错误是**给调用者**的，调用者应该看到 500
- Agent 的错误是**给模型**的反馈，模型通常能自己修正

实际例子：模型把日期传成 `2026-8-9`（不合法格式），如果抛异常整轮对话就崩了；
返回「参数校验失败：date 格式应为 YYYY-MM-DD」，模型下一轮就改对了。

**一个踩过的坑（很能说明工具输出设计的重要性）**

第一版 `_fmt_activity_brief` 输出活动摘要时没有带 `activity_id`：

```
running 5.01km 33.0min 配速6'35" 心率158/175
```

模型想查详情，但拿不到 id，于是**把日期当 id 去猜**：

```
get_activity({"activity_id": "2026-08-09"})  → 找不到活动
get_activity({"activity_id": "20260809"})    → 找不到活动
```

最后它对用户说"活动 ID 检索失败"。

补上 `id=626258316` 之后，模型一次就串起来了。

**教训**：工具的输出格式是 Agent 设计的一部分，不是随手 format。
模型只能看到你给的东西，漏一个关键字段它就断了链条，而且**不会报错**——
只表现为"回答得不好"，很难通过测试发现。

**两个实现细节**

① **同步 IO 必须丢线程池**

`database.py` 用的是同步 SQLAlchemy。在协程里直接调用会阻塞整个事件循环——
一个慢查询能把所有人的请求卡住。所以工具内部统一包一层：

```python
async def _in_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)

d = await _in_thread(db_get_day, today, self._user_id)
```

这是「异步框架 + 同步库」的经典接缝。

② **工具必须绑定 user_id**

HTTP 路由有 `Depends(get_current_user)` 兜着身份，但工具是 Agent **内部**调用的，
没有这层保护。所以每个工具在构造时就注入 `user_id`：

```python
registry = build_default_registry(user_id)
```

否则多用户场景下，模型可能读到别人的健康数据。这是很容易漏的安全点——
工具层看起来"不接触用户输入"。

### 5.4 LLM 传输层

`garmin_coach/llm/` 把不同厂商的流式协议归一化成同一组事件。

**为什么需要这一层**：DeepSeek / StepFun / 火山 Ark 都走 OpenAI 兼容协议，
但流式响应的细节（尤其是工具调用的分片方式）需要统一处理。如果 Agent 直接解析
原始 SSE，换一家厂商就要改循环逻辑。

定义了 7 种事件（`llm/base.py`）：

```python
TextDelta(text)                  # 正文增量
ThinkingDelta(text)              # 思考增量（推理模型会吐）
ToolUseStart(id, name)           # 开始一个工具调用
ToolUseDelta(id, partial_json)   # 工具入参的 JSON 片段
ToolUseEnd(id, name, arguments)  # 入参完整，可以执行了
MessageStop(stop_reason, usage)  # 本轮结束
StreamError(message)             # 错误（作为事件返回，不抛异常）
```

**这里有个技术难点：工具调用的入参是分片吐的。**

不是一个完整的 JSON，而是被切成好几段字符串，靠 `index` 字段归位：

```
{"delta": {"tool_calls": [{"index": 0, "id": "call_x", "function": {"name": "get_day", "arguments": "{\"date\":"}}]}}
{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"2026-08-09\"}"}}]}}
```

所以 `openai.py` 里要维护一个 `pending: dict[int, dict]` 累积碎片，流结束后
统一解析，再一次性交 `ToolUseEnd` 给上层。解析失败就把错误扔回给模型。

**流式解析的其它细节**：SSE 是按行（`data: {...}`）读，`[DONE]` 是结束标记；
每帧还带 `usage`（token 用量），最后一帧统计。这些统计会一路累加到
`AgentResult.usage`，用于观察成本。

### 5.5 对话消息的合法性

这是容易被忽略但很要命的一点。

OpenAI 协议的硬约束：**带 `tool_calls` 的 assistant 消息后面，必须紧跟对应的
tool 消息**，少一条就是 400。

```
assistant  {tool_calls: [call_A]}
tool       {tool_call_id: "call_A", content: "..."}     ← 必须有
assistant  {content: "最终回答"}
```

所以项目里不提供「随便 append 一个 dict」的入口，只能通过语义化方法写入
（`agent/conversation.py`）：

```python
conv.add_user(text)
conv.add_assistant_tool_calls(text, calls)   # 内部把 arguments 序列化回 JSON 字符串
conv.add_tool_result(call_id, content)
```

用接口约束协议正确性，比靠调用方自觉可靠。

**另外两个细节**：

- `max_turns = 8`：不设上限，模型可能在两个工具之间来回试探。到上限就带着已有
  文本收尾，而不是报错。
- token 用量**累加**而不是覆盖：一轮对话可能有 4 次 LLM 调用，只看最后一次会
  严重低估成本。（这里第一版写错过，用了 `usage = event.usage` 覆盖。）

---

## 六、记忆管理

先给结论：**只有短期对话记忆，没有长期记忆**。

### 6.1 现状

| 机制 | 实现 |
|---|---|
| 存储 | `chat_history` 表，每轮 user / assistant 各一行 |
| 加载 | 每次对话取**最近 20 条**（`get_chat_history(20, user_id)`） |
| 生命周期 | 每次请求新建 `ConversationManager`，不跨请求保留 |
| 清理 | 每天 **03:07** 删除 **7 天前**的记录（`cleanup_old_chat(days=7)`） |

对话流程里的位置：

```python
history = get_chat_history(20, user_id)      # 从库里取
result = await run_agent_chat(msg, user_id, history=history)
```

`ConversationManager.load_history()` 只接受 `user` / `assistant` 两种纯文本消息，
**主动丢掉 tool 消息**：

```python
for item in history[-limit:]:
    if item.get("role") not in ("user", "assistant"):
        continue
    if item.get("content"):
        self.messages.append({"role": role, "content": content})
```

这是刻意的。tool 消息只在「本轮」有意义——上一轮的工具调用结果对当前问题没价值，
而且重新塞回去会让模型误以为那些数据是刚查的。

### 6.2 为什么这样够用

这个场景下，「长期记忆」的价值比通常以为的低：

1. **事实性数据都在数据库里**。用户的历史训练、身体状态不需要模型"记住"，
   工具随时能查。记忆只需要承载**对话的连贯性**（"刚才说的那个方案"）。
2. **记忆窗口只需要覆盖一次会话的长度**。用户问完就走，不会跨天追问细节。
3. **跨会话的偏好已经被固化在其他地方**：训练目标在 `users.goal`、
   档案在 `users` 表、未来计划在 `daily_plans` 表。这些不是"记忆"，
   是结构化数据。

### 6.3 代价与局限

- **超过 20 条的对话会丢上下文**。用户问了很多轮之后，早期的约束消失了。
- **没有摘要压缩**。理想做法是把早期轮次压成一段摘要保留结论，
  目前是直接截断。
- **没有语义检索**。如果历史很长，无法只捞回相关的那几轮。

这三条都是可扩展点，不是当前设计的缺陷——在当前使用强度下（单次会话通常 5-10 轮）
没有触发。

---

## 七、提示词设计

系统提示词在 `garmin_coach/agent/prompts.py`，分两段拼接：
固定的领域规则 + 动态的当前时间。

### 7.1 为什么要注入时间

```python
def build_system_prompt(now=None):
    current = now or datetime.now()
    hour = current.hour
    period = "深夜" if hour < 5 else "早晨" if hour < 10 else ...
    return BASE_PROMPT + f"\n## 当前时间\n{current:%Y-%m-%d %H:%M}（{period}，周X）\n"
```

模型自己不知道现在是几点。而教练角色的回答**强依赖时段**：

> 早晨 5-10 点：今日安排（含天气窗口）+ 昨夜恢复
> 傍晚 18-22 点：今日复盘 + 明天预告
> 夜晚 22 点后：轻声提醒早睡
> **绝对不要在晚上跟用户说"今日运动安排"——时间已经过了**

最后那句是实际踩过的坑：没有这句约束时，晚上 11 点它还在建议"今天下午可以去跑个 5 公里"。

### 7.2 结构：七段，每段解决一个问题

```
① 身份与语气        你是谁、说话什么风格
② 数据获取方式      你有工具，什么时候该调、什么时候不该调
③ 时间感知          不同时段的回答角度
④ 安全硬约束        五档强度上限的定义，不可突破
⑤ 训练阶段规则      距目标赛事的天数决定训练重点
⑥ 决策规则          五条交叉验证逻辑（RPE、完成量、天气…）
⑦ 观察点 + 回复风格  看到什么要主动提、说话多长
```

**② 是这份提示词最重要的一段**，它定义了 Agent 的行为边界：

```
## 数据获取方式（重要，和以前不一样）
你看不到任何数据，除非主动调工具查。不要凭猜测回答数据类问题。

调用原则：
- 简单寒暄、不带数据的问题，直接回答，不要为了显得勤奋而乱调工具
- 涉及身体状态、训练量、具体某次运动 —— 必须查，不能编
- 需要多个信息时，一次把要调的工具都调了，不要一轮一轮挤
- 工具报错就换个参数重试；同一个工具连续失败两次就告诉用户查不到
```

第三条是实测加上的。不加的时候模型很"保守"，一轮只查一个工具：

```
第 1 轮 → get_safety
第 2 轮 → get_today
第 3 轮 → get_recent
```

三次往返、三次 LLM 调用才拿完数据，慢且贵。显式告诉它"一次把要调的都调了"之后，
通常两轮就结束。

第二条里那句"不要为了显得勤奋而乱调工具"同样重要——不写的话，用户说"你好"
它也会去查一遍 HRV。

### 7.3 安全约束怎么写才有效

安全档位不是建议，是**硬闸**。提示词里的写法是三层叠加：

```
## 训练建议的硬约束
安全档位 = 硬闸。`get_safety` 返回的 max_tier 是上限，绝对不能推荐超过它的强度。
- REST: 完全休息或走路 <30min
- EASY: 轻松跑（心率 <140），≤40min
- MODERATE: 有氧稳态（140~155），≤60min
- HARD: 阈值/间歇（155+），≤50min
- LONG: 长距离慢跑（<150），60~120min
如果档位是 REST，必须说"今天必须休息"，并给出原因。不要绕过、不要打折。
```

三层是：

1. **物理层**：档位由 `src/safety_rules.py` 的规则引擎计算（纯函数，有单元测试），
   **不是模型判断的**。模型只是读取结果。
2. **提示词层**：明确写"必须先调 get_safety""不能超过上限""不要绕过"。
3. **工具层**：`get_safety` 的 description 里也写"给任何训练建议前必须先调"。

为什么不能只靠提示词？因为模型可能"善意地"放宽建议。所以真正的硬约束放在
规则引擎里，提示词只负责让模型**去读**这个约束。

### 7.4 领域知识为什么要留在提示词里

训练阶段划分（距赛 >56 天基础期 / 29-56 天专项期 / 15-28 天巅峰期 / ≤14 天减量期）、
五条决策规则（RPE 与生理数据交叉验证、以实际完成量为渐进基准、力量训练时机、
天气自适应、周跑量分配）都是**业务知识**，不是代码逻辑。

这些放在提示词里，而不是写成 if-else：

- 它们本质是"判断准则"，需要模型结合上下文灵活应用，硬编码会僵化
- 但它们的**输入**（距赛天数、当日数据）是结构化的，由工具提供

这是这类系统的一个通用分工：**数据用代码取，判断用提示词指导**。

---

## 八、安全与权限

| 层面 | 做法 |
|---|---|
| 密码存储 | PBKDF2-SHA256，10 万轮 + 每用户随机盐，存成 `salt$hash` |
| 会话鉴权 | JWT（HS256），payload 只放 `user_id` 和签发时间 |
| 传输 | `Authorization: Bearer <token>` |
| 数据隔离 | 每张业务表都有 `user_id`，所有查询按它过滤 |
| 工具层隔离 | 工具构造时注入 `user_id`（详见 5.3） |
| 配置 | 所有密钥走环境变量 / gitignored 的 `.env`，代码里零硬编码 |
| 跨域 | 无（前后端同源部署） |

**一个设计观察**：`get_current_user` 在没带 token 时返回 `user_id=0` 而不是报错，
这是为了兼容早期的单用户模式。代价是这些接口"看起来"不需要登录，
但 `user_id=0` 在数据库里查不到任何数据，所以实际是安全的——只是没登录时
拿到的是空仪表盘而不是 401。

---

## 九、前端

### 9.1 Web（`static/index.html`）

单文件、零构建：纯 HTML + CSS + 原生 JS + Chart.js（CDN）。

- 设计令牌与 Android 的 `Theme.kt` 完全一致（同一套配色与卡片层级）
- 三个 tab：仪表盘 / 对话 / 记录，和 Android 的底部导航对齐
- 仪表盘九张卡片的顺序也照抄 `DashboardScreen.kt`
- 登录 token 存 `localStorage`，401 时自动回登录页

为什么不用框架：这个界面的状态复杂度很低（三个 tab、一个列表、一个弹窗），
引入 React/Vue + 构建链的收益是负的——多一层工具链、多一份依赖，
换来的只是组件复用，而这个页面没有复用需求。
项目展示场景要的是"打开就能跑"，不是"工程规范"。

### 9.2 Android

Kotlin + Jetpack Compose，纯原生，不套 WebView。

- 三个 tab + 登录 + 手表绑定 + 账号页
- `OkHttp` 手写 HTTP 客户端（32KB 的 `GarminApi.kt`），没用 Retrofit
- 与 Web 共享同一套 API 契约

### 9.3 共享的 API 契约

两端都只依赖这几个字段：

```
POST /api/chat  →  {"reply": "...", "recorded": bool}
GET  /api/health/dashboard → {today, recent_7d, charts, daily_stats, safety, datetime, period}
```

`recorded` 表示「这一轮有没有写入训练记录」——后端从 `AgentResult.tool_calls`
里判断有没有调用过 `record_training`，前端据此刷新记录列表。

---

## 十、部署

```
腾讯云 Ubuntu 24.04
├── ~/garmin-coach/
│   ├── src/             后端源码
│   ├── garmin_coach/    Agent 包
│   ├── static/          Web 前端
│   ├── venv/            Python 3.12 虚拟环境
│   ├── config/.tokens/  手表登录令牌（按 user_id 分目录）
│   └── .env             数据库连接串 + API key
├── MySQL 8              业务数据
└── 进程                 nohup + setsid 后台运行，监听 0.0.0.0:8766
```

部署脚本 `deploy.py`（paramiko）：

1. 检查服务器环境（venv、`.env`、`DATABASE_URL` 是否存在），不通过就中止
2. 打包 `src/` + `garmin_coach/` + `static/` + `requirements.txt`
3. 上传
4. **备份**服务器上的 `src/` 到 `src.bak.<时间戳>`
5. 解压覆盖
6. `pip install -r requirements.txt`
7. 重启并验证（首页状态码、`/api/llm/providers`、日志尾部）

不打包 `.env` / `config/.tokens` / `src/data` / `logs` / `static/audio`——
这些是服务器独有的运行时数据，覆盖会丢。

**两个运维上踩过的坑**（写在脚本注释里）：

- `pkill -f 'server.py'` 会匹配到**执行它的那条 shell 命令**（命令行里就含这个字符串），
  把自己杀掉，导致后面的启动指令执行不到。用 `server[.]py` 规避。
- SSH 执行启动命令时用 `get_pty=True`，通道关闭会把后台子进程一起带走，
  `nohup` 也救不了。要去掉 pty，并用 `setsid` + `disown`。

---

## 十一、局限与可扩展点

诚实列出，不是缺陷清单，是"如果继续做会做什么"：

| 项 | 现状 | 可扩展方向 |
|---|---|---|
| 对话流式输出 | 后端传输层已支持事件流，但 `/api/chat` 等全部完成才返回 | 接 WebSocket 做打字机效果，`Agent.run` 已有 `on_text` 回调 |
| 长对话记忆 | 最近 20 条，超过就截断 | 早期轮次摘要压缩 |
| Agent 效果评测 | 靠人工看回复质量 | 构造「问题 + 期望调用的工具」样本集做回归 |
| 工具集 | 所有用户共用 9 个工具 | 按训练目标裁剪，减少 schema 占用 |
| 进程管理 | nohup 进程，服务器重启后不自动起 | systemd unit + `enable` |
| HTTPS | 无（未绑域名） | 绑域名 + Let's Encrypt |
| 单实例 | 定时任务和 Web 服务在同一进程 | 拆开，任务用独立 worker |

---

## 十二、一句话总结每层

| 层 | 一句话 |
|---|---|
| 同步层 | 用 `garminconnect` 每天定时把官方数据拉进库，token 免密复用 |
| 数据层 | SQLAlchemy 宽表 + 联合主键，双品牌隔离，全 upsert 幂等 |
| 工具层 | 9 个只读查询 + 2 个写入，全部绑定 user_id，失败转反馈 |
| Agent 层 | ReAct 家族的 tool-calling 循环，最多 8 轮，token 用量累加 |
| 传输层 | 把 3 家厂商的 SSE 归一化成 7 种事件，处理工具调用分片 |
| 记忆 | 短期：最近 20 条对话；长期：靠结构化数据而非记忆 |
| 提示词 | 数据用代码取、判断用规则指导、安全约束由规则引擎兜底 |

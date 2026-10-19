# 重构计划

> 目标：把后端从「40 个路由塞在一个文件 + 对话靠手拼 prompt」，
> 改成「薄路由 + 领域服务 + 带工具调用的 Agent」，并具备发布到 GitHub 的质量。

---

## 为什么要重构

不是代码「不能跑」，而是它有三处会持续变贵的地方：

**1. `server.py` 一个文件装约 40 个路由**
每个路由都直接调数据库、直接拼返回结构。改一个接口要在 900 行里翻找，
加一个接口要重复抄鉴权和异常处理。

**2. 对话是单轮 RAG，不是 Agent**
`build_context()` 用 ~110 行手写模板，把安全档位、用户档案、今日数据、
7 天汇总、各天明细、标准动作名、训练计划**全部**拼成一大段文本塞进 system prompt。
后果：
- 每次提问都付一份固定的大 token 成本，哪怕用户只问"我昨天跑了多少"
- 想让模型看到新数据，就得改那段 110 行的拼接代码
- 模型无法追问细节（"那次跑步每公里配速多少"），因为它没有查询能力

**3. `llm_client.py` 一个文件混了四件事**
provider 配置、system prompt、上下文拼接、HTTP 适配、ASR，
其中 HTTP 适配还是手写的，没有统一的流式事件模型。

---

## 阶段划分

### 阶段 0 —— 入库与安全清理 ✅

- [x] 完整版后端从 `知识库\试用\garmin-coach` 搬入 `project\garmin-coach`
- [x] `git init`，首个提交 + `v0.1.0-baseline` 标签（可回滚）
- [x] **凭据外置**：9 个部署脚本里硬编码的 IP / 用户名 / SSH 密码 → `.deploy.env`（gitignored）
- [x] `database.py` 里硬编码的 MySQL 账号密码 → 必须由 `DATABASE_URL` 环境变量提供
- [x] 文档与提示词中的真名 / 真实手机号 / 真实域名 → 占位符
- [x] 含服务器地址的个人运维文档 → `docs-private/`（排除出版本库）
- [x] `.gitignore` + `.env.example` + `deploy_config.example.py`
- [x] 重写 README（架构、API 分组、路线图）
- [x] 新架构骨架 `garmin_coach/`（六个包的职责说明，暂无实现）
- [x] **补齐依赖**：`requirements.txt` 原先漏了实际在用的 `apscheduler`（定时调度）
      和 `paramiko`（部署脚本）；`firebase-admin` 改成可选
- [x] **解除 MySQL 绑定**：`database.py` 原先直接用 MySQL 方言的
      `insert().on_duplicate_key_update()`，SQLite 下渲染不了，服务起不来。
      改为按 `engine.dialect.name` 分派的 `_upsert` 系列辅助函数
- [x] **推送改软依赖**：`push_service.py` 的 `import firebase_admin` 包进 try/except，
      未安装时推送功能降级，其余功能不受影响
- [x] **启动验证**：`python src/server.py` 成功，`GET /` 返回 200（Web 仪表盘），
      `GET /api/llm/providers`、`GET /api/health/safety` 均 200

**验收标准**：`git ls-files` 里没有 `.env`、`.deploy.env`、token、数据库文件；
`v0.1.0-baseline` 能 checkout 回原状；本地不装 MySQL 也能把服务跑起来。

**阶段 0 发现的环境债**（留给后续处理）：
- `database.py` / `llm_client.py` 顶部各有一段手写的 `.env` 解析，
  且硬编码了 `C:\Users\<用户名>\study\.env` 这个绝对路径。换机器就失效。
  阶段 1 迁到 `garmin_coach/` 时改用标准 dotenv。
- `requirements.txt` 原先没有锁版本上限，`SQLAlchemy>=2.0` 实际装到 2.1.x 也能跑，
  但建议阶段 5 生成 `requirements.lock`。

---

### 阶段 1 —— LLM 传输层

新建 `garmin_coach/llm/`：

| 文件 | 内容 |
|---|---|
| `base.py` | `LLMClient` 抽象基类；`StreamEvent` 联合类型 |
| `openai.py` | OpenAI 兼容协议（DeepSeek / StepFun / Ark 都走这条） |
| `anthropic.py` | Anthropic 协议（可选，为以后接别的模型留口） |
| `factory.py` | 按 provider 名造客户端 |
| `config.py` | provider 配置，从环境变量读 |

**关键设计**：`StreamEvent` 必须把不同厂商的 stream 归一化。参考 MewCode 的做法，
至少这几种：

```python
TextDelta(text)                  # 正文增量
ThinkingDelta(text)              # 思考增量（DeepSeek reasoner）
ToolUseStart(id, name)           # 开始一个工具调用
ToolUseDelta(id, partial_json)   # 工具入参增量
ToolUseEnd(id, input)            # 工具入参完整
MessageStop(stop_reason)         # 本轮结束
Error(message)                   # 错误
```

**验收标准**：用一个最小脚本，不改动 `src/` 任何代码，就能把
`StreamEvent` 流打印到终端。

---

### 阶段 2 —— 工具层

新建 `garmin_coach/tools/`：

```python
class Tool(ABC):
    name: str                       # 模型看到的名字
    description: str                # 给模型读的说明书
    params_model: type[BaseModel]   # 入参 schema

    @abstractmethod
    async def execute(self, params) -> ToolResult: ...
```

计划实现的工具（全部复用 `services/` 里的函数，不重复写查询）：

| 工具 | 数据来源 | 替代的 HTTP 接口 |
|---|---|---|
| `get_safety` | safety 引擎 | `/api/health/safety` |
| `get_today` | 今日恢复 + 睡眠 + 天气 | `/api/health/dashboard` |
| `get_recent(days)` | 最近 N 天聚合 | `/api/health/recent/{days}` |
| `get_day(date)` | 指定日期 | `/api/health/day/{date}` |
| `get_activity(id)` | 单次活动分段/心率区间 | `/api/health/activity/{id}` |
| `get_weather` | 天气与跑步窗口 | `/api/health/weather` |
| `get_profile` | 目标与未来比赛 | `/api/user/profile` |
| `record_training` | 写入训练记录 | `/api/health/record-training` |

**验收标准**：每个工具都有单元测试，输入固定的假数据能返回预期结构。

---

### 阶段 3 —— Agent 主循环

新建 `garmin_coach/agent/`：

```python
class Agent:
    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        self.conversation.add_user_message(user_message)
        for turn in range(self.max_turns):
            response = await self.llm.stream(self.conversation.messages, tools=...)
            if not response.tool_calls:
                break                      # 模型不再要工具 → 收工
            for call in response.tool_calls:
                result = await self.execute_tool(call)
                self.conversation.add_tool_result(call.id, result)
        ...
```

配套 `ConversationManager`：消息历史、token 估算、超阈值时压缩。
（当前项目的对话历史已经在 `chat_history` 表里，`ConversationManager` 负责
把它和内存中的本轮消息合并，并决定要不要裁剪。）

**删掉的东西**：`build_context()` 那 110 行，以及 `SYSTEM_PROMPT` 里
「以下是用户的健康数据」那段拼接。

**验收标准**：问「我昨天跑了多少」，从日志里能看到模型主动调了
`get_recent` 或 `get_day`，而不是一上来就拿到全量数据。

---

### 阶段 4 —— 路由瘦身

把 `src/server.py` 按资源拆进 `garmin_coach/api/`：

```
api/auth.py     /api/auth/*
api/watch.py    /api/watch/*
api/health.py   /api/health/*
api/chat.py     /api/chat/*  +  /ws/chat
api/push.py     /api/push/*
api/asr.py      /api/asr
```

原则：**路由函数体十行以内**，超过就是逻辑没下沉。
`/api/chat` 改成走 Agent；其余 CRUD 路由只是换个位置，行为不许变。

**验收标准**：Android App 不改一行代码，所有接口行为与重构前一致。

---

### 阶段 5 —— 测试、文档、发布

- 单元测试：safety 引擎（已有 `test_safety_rules.py` 可用）、工具层、序列化
- 集成测试：起一个内存 SQLite，跑通「登录 → 绑定 → 拉数据 → 对话」
- 补充 API 文档（FastAPI 自带 `/docs`，但要写清鉴权方式）
- 发布：确认无凭据 → push → 打 release tag

---

## 关键决策记录

**为什么只重构对话这条线？**
其余约 38 个 CRUD 路由不需要 LLM。把 `Agent` 硬套到 `/api/watch/list`
这类接口上是过度设计，只会让代码更难读。分层要为问题服务，不是为架构图服务。

**为什么用绞杀者模式而不是推倒重写？**
后端已在服务器上跑着真实数据。推倒重写意味着一段时间内没有可用服务，
而且容易在细节上漏掉线上才有的行为。新旧并存、逐个模块迁移，
每一步都可回滚。

**为什么把 `services/` 单列一层？**
安全档位这类规则既被 HTTP 接口用（网页显示当前档位），也被 LLM 工具用
（AI 回答"今晚能不能跑"）。放一起才能保证两边口径一致 —— 否则会出现
「网页说 EASY、AI 说 HARD」这种最糟糕的情况。

---

## 恢复工作的方法

```bash
cd C:\Users\boobyboy\study\project\garmin-coach
git log --oneline              # 看做到哪
git checkout v0.1.0-baseline   # 回到重构前
```

凭据在 `.deploy.env`（部署）与 `.env`（运行时）里，两者都不进版本库。
换机器时参照 `.env.example` / `deploy_config.example.py` 重建。

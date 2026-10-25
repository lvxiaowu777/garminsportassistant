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

### 阶段 1 —— LLM 传输层 ✅

已完成，文件在 `garmin_coach/llm/`：

| 文件 | 内容 |
|---|---|
| `base.py` | `LLMClient` 抽象 + 7 种 `StreamEvent` |
| `openai.py` | OpenAI 兼容流式客户端（DeepSeek / StepFun / Ark 共用） |
| `config.py` | provider 配置，从环境变量读 |
| `factory.py` | 按配置造客户端 |

工具调用在流式里是**分片**吐的（`function.arguments` 被切碎），
拼装逻辑封在 `openai.py` 的 `pending` 字典里，上层拿到的是完整对象。

### 阶段 2 —— 工具层 ✅

已完成，文件在 `garmin_coach/tools/`。9 个工具：`get_safety` / `get_today` /
`get_recent` / `get_day` / `get_activity` / `get_weather` / `get_profile` /
`record_training` / `save_plan`。

三个设计要点：
- 工具执行失败**不抛异常**，转成 `is_error` 的 `ToolResult` 交给模型自我修正
- 同步 SQLAlchemy 查询用 `asyncio.to_thread` 包一层，避免阻塞事件循环
- 每个工具绑定 `user_id`（工具是 Agent 内部调用，没有 `Depends` 兜底）

### 阶段 3 —— Agent 主循环 ✅

已完成，文件在 `garmin_coach/agent/`。`build_context()` 那 110 行已不再被
`/api/chat` 使用（保留在旧路径作回退）。

实测：提问「我 8 月 9 号那次跑步怎么样」，模型自己完成
`get_day` → 看到 activity_id → `get_activity` → 输出分析，3 轮 2 次工具调用。

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

### 阶段 3.5 —— 死代码清理 ✅（2026-10-23）

在动阶段 4 之前先把「已经没人调用的东西」清掉，否则拆路由时会分不清
「这是要搬的逻辑」还是「这是早就不用的残留」。

删掉的文件（`git rm`，需要时 `git checkout <commit> -- <path>` 取回）：

| 删除 | 原因 |
|---|---|
| `src/weather_sync.py` | markdown 时代的天气同步，写 `健康/*.md`；线上早已换 `weather_server.py`（直接写库） |
| `src/frontmatter.py` | 只服务于 markdown 读写，随上面两处一起失效 |
| `src/migrate_data.py` | 一次性迁移（md → 库），2026-08-13 已执行完 |
| `src/migrate_sqlite_to_mysql.py` | 一次性迁移（SQLite → MySQL），2026-08-14 已执行完 |
| `src/init_garmin.py` | 本地单用户 token 引导；多用户改为 `/api/watch/bind` + `config/.tokens/{user_id}/` |
| `check_server.py` / `quick_check.py` / `start_server.py` / `setup_https.py` / `install_nginx.py` / `start_nginx.py` / `_clean_test.py` | 一次性 paramiko 运维脚本，职责已被 `deploy.py`（含 `--check`）覆盖 |
| `static/sw.js` / `static/manifest.json` | PWA 残留，`index.html` 从没注册过 service worker |
| `config/location_cache.json` | 只有已删除的 `weather_sync.py` 在读 |

文件内删掉的死代码：

| 位置 | 内容 |
|---|---|
| `src/garmin_sync.py` 625 → 434 行 | `get_client` / `write_health_md` / `sync_day` / `backfill` / `__main__`（写 md 那一半），只留 `fetch_day` / `fetch_activity_full` |
| `src/server.py` | 本地复制的 `compute_safety`（改为 import `garmin_coach.services.safety`，与 AI 工具共用一份）、死常量 `HEALTH_DIR`、死函数 `activityLabel`、4 个未用 import |
| `src/database.py` | `normalize_exercise_name` / `_row_dict` / `has_health_data` / `get_user_by_id`（零引用） |
| `src/push_service.py` | `send_broadcast`（0 引用）+ 未用 import |
| `garmin_coach/db/legacy.py` | 桥接表里没人用的 3 项（`get_dashboard` / `get_standard_exercise_names` / `has_health_data`） |

验收：`python -m compileall src garmin_coach` 通过；`TestClient` 冒烟
`/`、`/api/llm/providers`、`/api/health/{safety,dashboard,recent/7}`、`/api/chat/dates` 全 200。

### 阶段 4 —— 路由瘦身 ✅（2026-10-24）

`src/server.py` 从 875 行收到约 110 行，只剩：路径引导 → 建 app + 日志 → `include_router` → 挂静态 → 启动。

实际落地结构（和最初规划的差别：`asr` 并入 `chat`，另加了 `user` / `system`）：

```
garmin_coach/api/
  deps.py             鉴权透传、静态目录、SUPPORTED_BRANDS/SYNCERS、logger
                      （副作用：import 它 = 把 src/ 放进 sys.path）
  schemas.py          全部 Pydantic 请求模型
  routers/auth.py     /api/auth/*
  routers/watch.py    /api/watch/*
  routers/health.py   /api/health/*（含 record-training）
  routers/chat.py     /api/chat/* + /api/asr + /ws/chat
  routers/user.py     /api/user/profile
  routers/push.py     /api/push/*
  routers/system.py   / 、/api/llm/providers
garmin_coach/services/
  chat_context.py     build_chat_context（从 server.py 搬出）
  markers.py          [RECORD] / [PLAN] 解析
  clock.py            period_of(hour)
```

原则：**路由函数体十行以内**，超过就是逻辑没下沉。`/api/chat` 走 Agent；其余路由只换位置，行为不变。

**验收（已通过）**：

- 路由集合与重构前**逐条一致**：36 条 API + `/ws/chat`，无缺失、无新增；
- `compileall` 通过；`TestClient` 探活 10 个 GET 全 200；`/ws/chat` 可握手；
- 真打一次 `POST /api/chat`（走 Agent）→ 200，1.3s，回复正常；
- Android / Web 不需要改任何代码。

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
cd garmin-coach   # 项目根目录
git log --oneline              # 看做到哪
git checkout v0.1.0-baseline   # 回到重构前
```

凭据在 `.deploy.env`（部署）与 `.env`（运行时）里，两者都不进版本库。
换机器时参照 `.env.example` / `deploy_config.example.py` 重建。

# Garmin Coach 代码地图

> 逐文件讲「这一块是干什么的、关键函数在哪、数据从哪来到哪去」。
> 和 [`PROJECT-OVERVIEW.md`](PROJECT-OVERVIEW.md) 的分工：那份讲**设计与取舍**，
> 这份讲**代码实体**（哪个文件、哪一行、谁调谁）。行号以 2026-10-23 的 `b305f4e` 为准。

---

## 一、四条主链路（先看这个，再看文件）

**① 数据进库（定时或手动）**

```
APScheduler 02:07 / POST /api/health/sync
  → garmin_server_sync.sync_user_garmin(user_id, days)
      → Garmin() + g.login(config/.tokens/{user_id})     读 token 免密登录
      → 循环 days 天：garmin_sync.fetch_day(g, date)      拉一天 → 嵌套 dict
      → database.upsert_health_daily(data, user_id, brand)  拍平 → upsert 宽表 + 活动
  （高驰同理：coros_server_sync.sync_user_coros → _build_day → 同一个 upsert）
```

**② 一次对话**

```
POST /api/chat  (server.py:600)
  → 取最近 20 条历史 get_chat_history(20, user_id)   ← 注意只取「今天」的
  → add_chat 落用户消息
  → garmin_coach.agent.bridge.run_agent_chat()
       → Agent.run()  循环：调 LLM → 模型要工具 → ToolRegistry.execute → 回填 → 再调
       → 9 个工具内部全部 asyncio.to_thread(同步 SQLAlchemy)
  → 判断有没有用过 record_training 工具（recorded 标记）
  → 兼容旧 [RECORD]/[PLAN] 标记（回退路径用）
  → add_chat 落 AI 回复 → {"reply", "recorded"}
```

**③ 语音 / WebSocket（还是旧单轮链路）**

```
POST /api/chat/voice 或 WS /ws/chat
  → build_chat_context(user_id)        仪表盘数据 + 安全档位 + 档案 + 计划
  → llm_client.chat / chat_stream      把全部数据拼成一段文本塞进 system prompt
  → 正则解析 [RECORD] / [PLAN] 落库
```

**④ 页面渲染**

```
GET /api/health/dashboard
  → get_dashboard()：今日 + 7 天趋势 + daily_stats
  → + compute_safety()   ← 与 AI 工具共用 services/safety 那一份
  → + datetime / period
  → static/index.html renderDashboard() 画 9 张卡 + Chart.js 折线
```

---

## 二、接口层

### `src/server.py`（876 行 · 36 个路由）

应用装配 + 全部 HTTP/WS 入口。**这一层的目标形态是"薄壳"，但目前路由函数里仍有业务逻辑**
（阶段 4 会把它们按资源拆进 `garmin_coach/api/`）。

| 区块 | 行 | 内容 |
|---|---|---|
| 路径引导 | 5-30 | 把 `src/` 和项目根塞进 `sys.path`（所以才能 `import garmin_coach`） |
| 应用与日志 | 56-73 | `FastAPI()`、`logging.basicConfig`（只为让 Agent 的工具调用进日志） |
| `build_chat_context` | 82-138 | **旧链路的上下文装配**：dashboard + safety + 档案 + 比赛倒计时 + 计划 |
| 鉴权路由 | 159-224 | 注册（手机号 ≥11 位、密码 ≥6 位）、登录、`/api/auth/me`（回档案 + `profile_completed`） |
| 手表路由 | 231-337 | 绑定（佳明走 MFA、高驰走逆向登录）、验证码第二步、解绑、开关、列表、单选 |
| 健康路由 | 340-461 | dashboard、某天、最近 N 天、安全档位、单次活动、删/改活动、天气、同步、同步天气 |
| 对话路由 | 463-663 | 历史/日期、ASR、语音消息、清空、`extract_training_record`、`extract_plan`、`POST /api/chat` |
| 其他 | 665-766 | provider 列表、笔记、手动记训练、推送注册/状态/测试/触发 |
| WebSocket | 769-842 | `/ws/chat`，逐 token 推送；`user_id` 取自**客户端消息体**（无 token 校验） |
| 静态 | 844-856 | `/` 返回 `index.html`，`/static` 挂载 |
| 启动 | 858-876 | `init_db()`、`init_firebase()`、APScheduler 注册 5 个 job、`uvicorn.run(8766)` |

**启动时的定时任务**（858-870）：`check_and_notify` 8:07/19:07、`sync_all_users` 2:07、
`sync_all_coros_users` 2:17、`cleanup_old_chat` 3:07。**天气不在里面**，只能靠客户端调
`/api/health/sync-weather`。

---

## 三、数据层

### `src/models.py`（235 行 · 9 张表）

纯 ORM 声明，没有逻辑。三个设计点值得记：

- `HealthDaily` 是**宽表**，主键 `(date, user_id, brand)`；睡眠/恢复/训练状态/天气/实时天气
  几十列全在一行。
- `Activity` 唯一键 `(user_id, brand, activity_id)`，`hr_zones` / `splits` 存 JSON 字符串。
- `StrengthSet` 通过 `activity_id`（字符串）关联 `Activity`，**没有外键约束**。
  所以删活动时必须手动删 `strength_sets`（`delete_activity` 就是这么干的）。

其余：`User`（档案 + `training_types`/`races` 两个 JSON 文本列）、`WatchBinding`（
`UNIQUE(user_id, brand)` + `enabled` 决定数据源）、`ChatHistory`（`type` 区分 text/voice）、
`ExerciseName`（20 个标准动作 + 别名）、`DeviceToken`、`DailyPlan`（3 天计划 JSON）。

### `src/database.py`（1246 行 · 约 45 个函数）

全项目**唯一**的 SQL 出口。按区块：

| 区块 | 行 | 内容 |
|---|---|---|
| 连接与方言适配 | 36-146 | `DATABASE_URL` 必填；`_insert_stmt` / `_upsert` / `_append_concat` / `_upsert_append` 按 `engine.dialect.name` 分派 MySQL 与 SQLite |
| 建表与补列 | 149-218 | `init_db()` = `create_all` + 预填动作名 + 4 个 `_ensure_*_columns()`（幂等 ALTER，给老库补列） |
| 标准动作名 | 221-274 | `DEFAULT_EXERCISES` 20 条；`get_standard_exercise_names()` |
| 写入 | 281-419 | `upsert_health_daily()` 把 `fetch_day` 的嵌套 dict 拍平成列（`None` 直接丢弃，不覆盖旧值）；`_upsert_activity` 写活动 |
| 查询 | 426-687 | `get_active_brand`（第一个 enabled 的绑定）、`get_day`、`get_recent`、`get_dashboard`、`get_activity`、`get_weather`、`_activity_row`（ORM→dict + 解 JSON） |
| 聊天 | 694-778 | `add_chat`、`get_chat_history`（**默认只查今天**）、`get_chat_dates`、`cleanup_old_chat`（连语音文件一起删）、`clear_chat_history` |
| 笔记 | 785-810 | `add_note`（**固定 user_id=0**，历史遗留） |
| 训练记录 | 818-908 | `record_training`：力量训练同一天复用同一条 activity；`manual_{时间戳}` 当 id |
| 用户/档案 | 920-1029 | 注册、按手机号/id 查、档案读写（JSON 列序列化）、`last_sync_date` |
| 手表绑定 | 1036-1118 | bind/unbind/list/enabled/select（单选 = 只有一个是 1）、`get_watch_enabled`、`get_enabled_user_ids` |
| 推送 token | 1125-1193 | 注册/取 30 天内活跃/移除；`get_global_*`（**不过滤用户**，给旧推送逻辑用） |
| 计划 | 1200-1241 | `save_plan`（同日先删后插）、`get_latest_plan`（异常返回 None，绝不影响主流程） |

`get_dashboard()` 的细节值得单独记：它把「睡眠」当成滞后一天的数据（今天是空就拿昨天的补），
然后算 7 天跑量/平均 HRV/静息/睡眠，并产出 `charts`（三条趋势）与 `daily_stats`（逐日明细）。
**它内部开 4 次 session**（`get_day`×2 + `get_recent`），是最典型的 N+1 入口。

---

## 四、规则层（这个项目的核心）

### `src/safety_rules.py`（184 行 · 纯函数，零 IO）

`evaluate(data, recent_hrv_avg, recent_hard_days) -> (max_tier, blockers, notes)`：

从最高档 `LONG` 开始，逐条硬闸往下压，最后返回 `TIER_ORDER[max_idx]`。硬闸顺序：
HRV 比 7 日均值低 20%/10% → 官方 HRV 状态 → ACWR（≥1.8 到 REST、≥1.5 到 EASY）→
训练反馈（OVERREACHING/STRAINED）→ 睡眠时长/得分 → 静息心率 >70 → Body Battery <30 →
训练准备度 → 近 3 天高强度次数 → 高温高湿（热指数 = 温度 + 湿度×0.1）→ AQI → 昨天已练强度。

`blockers` 是「为什么降档」（决策级），`notes` 是「注意一下」（不强制）。前端把两者分开展示。

### `garmin_coach/services/safety.py`（122 行）

`safety_rules` 的**装配层**：从数据库取今天/昨天/近 7 天，算 `recent_hrv_avg` 和
`recent_hard_days`，拍平成 `flat` dict 交给 `evaluate()`。另外提供
`compute_training_phase(days_until_race)`（>56 基础 / 29-56 专项 / 15-28 巅峰 / ≤14 减量）。

**为什么单独一层**：网页 `/api/health/safety` 和 AI 的 `get_safety` 工具都调它，保证
「网页说 EASY、AI 说 HARD」不会发生。`server.py` 里原来那份复制品已在 `b305f4e` 删除。

---

## 五、数据源层

### `src/garmin_sync.py`（434 行）

只做拉取，不登录、不落库、不写文件（写 md 的那一半已删）。三个函数：

- `_safe(fn, ...)`：任何 `garminconnect` 方法挂了就打印一行 `⚠️` 并返回 default —— 所以
  **某个字段拉失败不会中断整天同步，但也不会报错给你**，只留一条 log。
- `_pace_str(m/s)`：配速格式化。
- `fetch_activity_full(g, summary)`：单次活动详情 → 距离/时长/配速/心率/训练效果/步频/
  步幅/爬升/`splits`（每公里分段）/`hr_zones`（`z1_sec`… + 区间下界）。
- `fetch_day(g, date)`：一天的全量，返回嵌套 dict：
  `{date, weekday, sleep{h,w,score,deep_min,…}, recovery{hrv,resting_hr,body_battery_*,readiness,…},
  daily{steps,kcal,floors,stress_breakdown}, fitness{vo2max,training_status,acwr_ratio,race_*},
  vitals{respiration,spo2}, activities[...]}`。

### `src/garmin_server_sync.py`（112 行）

**编排层**，四个职责：

- `login_and_store(user_id, email, pwd)`：`Garmin(..., return_on_mfa=True)` 登录，需要 MFA 就把
  对象暂存进 `_PENDING_MFA`（**必须同一实例**，`resume_login` 依赖对象内部状态），否则
  `client.dump(config/.tokens/{user_id}/)`。
- `complete_mfa(user_id, code)`：取暂存对象 → `resume_login` → dump token。
- `sync_user_garmin(user_id, days)`：先查设备开关 → 用 token 免密登录 → 逐天
  `fetch_day` + `upsert_health_daily`，**单天失败只打日志继续下一天**，返回成功天数。
- `sync_all_users(days)`：`get_enabled_user_ids("garmin")` 循环。

### `src/coros_server_sync.py`（553 行）

最"脏"的一块：高驰没有官方 API，走两套逆向接口。

- **Web API**（`teamcnapi/teameuapi/teamapi`）：`/account/login`（账号 + md5 密码，token 24h）、
  `/analyse/dayDetail/query`（rhr / avgSleepHrv / trainingLoad / trainingLoadRatio）、
  `/analyse/query`（vo2max / lthr / stamina）、`/activity/query`。
- **Mobile API**（`apicn/apieu/api`）：睡眠只在这里有。登录凭据要 **AES-128-CBC 加密**
  （key/IV 从 APK 逆向，`_mobile_encrypt`），带设备指纹 `yfheader`。
- token 存 `config/.tokens/{user_id}/coros_auth.json`（web + mobile 两套）。
- `_build_day()` 把 Coros 字段映射成和佳明同构的 `{recovery, fitness, sleep, activities}`，
  所以两边最终写同一批列。
- 注意：Coros 的日期参数要 `YYYYMMDD`（`_compact`），传带连字符的会报 `Service exceptions`。

### `src/weather_server.py`（127 行）

`sync_weather_to_db(user_id, date, lon, lat)`：经纬度 → `/geo/v2/city/lookup` 反查 LocationID
→ `/v7/weather/3d` 取当天最高/最低/湿度/风/UV/降水 → 当天额外取 AQI（`cn-mee`）和
`/v7/weather/now`（当前温度/体感/天气）→ `upsert_health_daily({"weather": {...}})`。
`lon/lat` 为 0 时返回 `None`（**不做默认城市**，多用户必须由客户端定位传入）。

---

## 六、Agent 层（`garmin_coach/`）

### `llm/`：把三家厂商的 SSE 归一化

| 文件 | 行 | 内容 |
|---|---|---|
| `base.py` | 101 | `LLMClient` 抽象 + 7 种事件：`TextDelta` / `ThinkingDelta` / `ToolUseStart` / `ToolUseDelta` / `ToolUseEnd` / `MessageStop` / `StreamError` |
| `openai.py` | 175 | OpenAI 兼容流式实现。**难点在 tool_calls 分片**：`function.arguments` 是按 `index` 拼的碎片，这里用 `pending: dict[int, dict]` 攒齐，流结束再一次性 `ToolUseEnd`；顺带累加 `usage` |
| `config.py` | 74 | 三家 provider（DeepSeek / StepFun / Ark）从环境变量读，可选 `python-dotenv` 加载项目 `.env` |
| `factory.py` | 20 | `create_client(provider)` → `OpenAIClient`（要接 Anthropic 原生协议只改这里） |

### `tools/`：Agent 的手脚

| 文件 | 行 | 内容 |
|---|---|---|
| `base.py` | 55 | `Tool` 抽象：`name` + `description`（写给模型看的说明）+ `params_model`（pydantic，既校验又导 schema）+ `schema()` |
| `registry.py` | 64 | `register` / `schemas()` / `execute()`：**失败不抛异常**，参数校验错或执行出错都转成 `is_error=True` 的 `ToolResult` 回给模型自纠 |
| `health.py` | 550 | 9 个工具 + `build_default_registry(user_id)` |

9 个工具与「什么时候该调」：

| 工具 | 返回 | 触发场景 |
|---|---|---|
| `get_safety` | 档位 + 降档原因 + 近 7 天 HRV 均值 + 近 3 天强度次数 | **任何训练建议前必须先调** |
| `get_today` | 今日恢复/睡眠/天气/今日运动 | 「今天状态怎么样」「今晚能不能跑」 |
| `get_recent(days)` | 区间汇总（跑量/次数/均值）+ 逐日一行 | 「这周跑量」「最近恢复」 |
| `get_day(date)` | 单日详细一行 + 备注 | 用户提具体日期 |
| `get_activity(id)` | 分段配速、心率区间、训练效果 | 「那次跑步每公里配速」 |
| `get_weather(date)` | 天气九项 | 安排户外训练 |
| `get_profile` | 目标/比赛倒计时/训练阶段/已存计划 | 做中长期安排前 |
| `record_training(...)` | 写 activities + strength_sets | 用户明确说「我练了什么」 |
| `save_plan(days)` | 存 daily_plans | 用户明确要「安排未来几天」 |

实现细节：所有 DB 调用都包 `_in_thread`（`asyncio.to_thread`），因为 SQLAlchemy 是同步的；
每个工具构造时注入 `user_id`（工具层没有 `Depends` 兜底）；`_fmt_activity_brief` 必须带
`id=`，否则模型会拿日期当 activity_id 去猜（踩过的坑）。

### `agent/`：循环与记忆

| 文件 | 行 | 内容 |
|---|---|---|
| `loop.py` | 190 | `Agent.run()`：最多 8 轮「模型 ↔ 工具」。收 `TextDelta` 拼正文、收 `ToolUseEnd` 排队执行；`usage` **累加**；到上限带已有文本收尾并置 `truncated`。返回 `AgentResult{text, tool_calls, turns, usage, error}` |
| `conversation.py` | 101 | 消息序列合法性。不提供「append 任意 dict」，只有 `add_user` / `add_assistant_tool_calls` / `add_tool_result`，保证 `tool_calls` 后面紧跟对应 tool 消息（否则 OpenAI 400） |
| `prompts.py` | 138 | 七段 system prompt：身份 / 数据获取方式（有工具、别乱调）/ 时间感知 / 安全硬约束（五档）/ 训练阶段 / 决策规则五条 / 回复风格；`build_system_prompt()` 追加当前时间与时段 |
| `bridge.py` | 50 | HTTP 层唯一入口 `run_agent_chat()`：取 provider → 造 client → `build_default_registry(user_id)` → `Agent.run()` |

### `db/legacy.py`（39 行）与 `api/__init__.py`（15 行）

`legacy.py` 把 `src/` 插进 `sys.path`，再 re-export 8 个查询函数——绞杀者模式的脚手架，
`database.py` 整体搬进 `garmin_coach/db/` 后就该删。
`api/__init__.py` 只有一段文档：阶段 4 拆路由的**图纸**，目前没有代码。

---

## 七、遗留 LLM 层

### `src/llm_client.py`（433 行）

Agent 接管 `/api/chat` 之前的实现，现在还服务三条路：语音、WebSocket、Agent 失败回退。

- `PROVIDERS`（35-57）：三家 provider 的硬编码配置（地址/模型/`max_tokens`）。
- `SYSTEM_PROMPT`（62-160）：旧版提示词，和 `agent/prompts.py` 有 80% 重复，且**输出约定不同**
  ——旧版要求末尾附 `[RECORD]{...}[/RECORD]` / `[PLAN]{...}[/PLAN]` JSON，新版改为调工具。
- `get_provider(name)`（163）：带 fallback，没 key 就换下一个有 key 的。
- `build_context(data)`（177-284）：把上下文拼成 ~110 行文本（安全档位、档案、今日、7 天、
  逐日、动作名、已存计划）。
- `chat` / `chat_stream`（287-391）：单轮调用，**没有 tools 参数**，每次都新建 `httpx.AsyncClient`。
- `asr_transcribe`（394）：StepFun ASR；`STEPFUN_API_KEY` 为空时**直接返回空串**（接口层会报 500）。

---

## 八、辅助模块

### `src/auth.py`（77 行）

`hash_password`：PBKDF2-SHA256，10 万轮 + 32 字节随机盐，存成 `salt$hash`。
`create_token`：HS256 JWT，**payload 只有 user_id 和 iat，没有 exp（永不过期）**。
`get_current_user`：没带 token 返回 `0`（兼容早期单用户），带错 token 才 401。

### `src/push_service.py`（229 行）

`firebase_admin` 是**可选依赖**（ImportError 就整体降级为不可用，服务照常起）。
`init_firebase` 读 `config/firebase-service-account.json`；`send_push` 发一条 FCM（Android
channel `training_reminder`），token 失效自动从库里移除；`check_and_notify` 是定时任务的判断
逻辑：只在 7-9 点 / 18-20 点生效，按「昨天有没有数据、有没有运动、HRV 是否 <50」选文案。

---

## 九、前端

### `static/index.html`（1037 行，单文件零构建）

| 区块 | 行 | 内容 |
|---|---|---|
| CSS | 8-253 | `:root` 设计令牌（深色主题变量）+ 卡片、tabbar、气泡、弹窗、骨架屏；与 Android `Theme.kt` 同一套配色 |
| 静态骨架 | 258-333 | `#authView` 登录视图、`#appView`（三 tab + 顶栏 + 底部导航）、`#modal`、`#toast` |
| ① 基础设施 | 336-393 | `state`（token/userId/chatMessages）、`$`/`$$`、`toast`、`esc`、`api()`（自动带 Bearer、401 回登录页、`/api/auth/*` 例外） |
| ② 登录 / 退出 | 396-464 | 登录/注册切换、`logout()`、`enterApp()` |
| ③ 仪表盘 | 466-758 | `loadDashboard()`（骨架屏）、`renderDashboard()` 按 9 个模块拼 HTML、`drawTrend()` 用 Chart.js、`renderQuickRow()` |
| ④ 对话 | 761-833 | `renderChat()` 气泡、`loadChatHistory()`、`sendChat()` 乐观追加 + 等回复 |
| ⑤ 训练记录 | 835-970 | `loadHistory()` 取 30 天、按日分组、`openActivityDetail()` / `renderActivityDetail()` |
| ⑥ 弹窗 / 导航 / 事件 | 973-1021 | `openModal` / `closeModal`（点遮罩、Esc 关闭）、`switchTab`、输入框自增高、刷新按钮 |
| ⑦ 启动 | 1024-1035 | 有 token 就 `/api/auth/me` 验一次 |

---

## 十、运维脚本（根目录）

| 文件 | 行 | 内容 |
|---|---|---|
| `deploy.py` | 246 | `--check` 只查环境；默认：打包（`src/` + `garmin_coach/` + `static/` + requirements，排除 `.env`/tokens/data/logs/audio）→ 上传 → 备份服务器 `src/` → 解压 → `pip install` → 重启 → 验证 3 个 URL + 日志尾 |
| `deploy_config.py` | 50 | 读 `.deploy.env`（或环境变量）出 `HOST/USER/PASS`，`require_credentials()` 缺失时报错并给模板 |
| `config_proxy.py` | 72 | 一次性：写 nginx vhost（443 + WebSocket 升级 + 80 跳转）并 reload |
| `get_cert.py` | 35 | 一次性：`certbot --nginx` 签发 + 查看证书 |

`deploy.py` 里两处踩坑注释别删：`pkill -f 'server[.]py'`（避免匹配到自己的 shell）和
不用 `get_pty`（否则通道关闭会把后台进程带走）。

---

## 十一、已知的不一致（改代码前先看这段）

| # | 现象 | 位置 |
|---|---|---|
| 1 | **旧链路读不到今日数据**：`build_context` 读 `today.recovery/sleep/fitness/weather`，但 `get_dashboard` 的 `today` 是**扁平列**（`hrv`/`sleep_hours`/…）→ 语音/WS 的 AI 只看到「今日运动」，看不到 HRV/睡眠/电量/天气 | `llm_client.py:219-248` vs `database.py:579-596` |
| 2 | **计划 JSON 形状不一致**：`SavePlanTool` 存数组，旧 `[PLAN]` 存 `{"days": [...]}`；消费方都写 `plan.get("days")` → Agent 存过计划后，语音链路 500、`get_profile` 工具报错 | `tools/health.py:519` / `llm_client.py:275` |
| 3 | `add_note` 固定写 `user_id=0`，多用户下笔记进公共桶（且 `/api/note` 没有客户端在调） | `database.py:785` |
| 4 | `check_and_notify` 用 `get_global_*`（不过滤用户）→ 按「任意用户」的数据决定给所有人发什么 | `database.py:1171` |
| 5 | `get_chat_history` 默认只查**今天** → 跨天追问会丢上下文（`/api/chat` 传空 date） | `database.py:705` |
| 6 | `/ws/chat` 的 `user_id` 取自客户端消息体，不校验 token | `server.py:769` |
| 7 | 两份 system prompt 已漂移（输出约定不同） | `llm_client.py:62` vs `agent/prompts.py:17` |
| 8 | `manual_{秒级时间戳}` 当 activity_id，同一秒记两次会撞唯一键 | `database.py:829` |
| 9 | 没设 `JWT_SECRET` 时用源码里的默认值；未登录返回 `user_id=0` 而非 401 | `auth.py:16`、`auth.py:69` |

---

## 十二、建议阅读顺序

```
models.py           先有形状
database.py         upsert_health_daily / get_day / get_recent / get_dashboard / record_training
garmin_sync.py      fetch_day 的字段从哪来
garmin_server_sync.py  怎么串成一次同步
safety_rules.py     ← 最值得精读，规则决定 AI 的上限
services/safety.py  规则怎么被喂数据
tools/health.py     数据怎么变成模型能读的文本
tools/registry.py + agent/loop.py   一次工具调用怎么闭环
agent/prompts.py    模型被约束成什么样
server.py           最后看路由（此时每个被调函数你都认识了）
```

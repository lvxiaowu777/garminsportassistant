# Garmin Coach

> 运动数据聚合 + AI 教练。把手表数据汇总成健康档案，
> 由一个会调用工具的 AI 教练回答「今晚跑不跑」「我这周恢复得怎么样」这类问题。
> 数据模型按**多品牌**设计（`brand` 列 + 设备单选），当前只实现了佳明（Garmin）。

后端 FastAPI + SQLAlchemy；前端两个：原生 Android App（Jetpack Compose）和内置 Web 仪表盘。

---

## 能力

| 能力 | 说明 |
|---|---|
| 多用户账号 | 手机号注册/登录，JWT 鉴权，数据按 `user_id` 隔离 |
| 手表绑定 | 佳明（账号密码 + MFA 两步验证）；表结构多品牌就绪，其他品牌可先只记录账号 |
| 数据同步 | 定时 + 手动触发，拉取睡眠 / HRV / 心率区间 / 每公里分段 |
| 安全档位 | 硬规则引擎，把身体状态折算成 REST / EASY / MODERATE / HARD / LONG 五档上限 |
| AI 教练 | 带工具调用的对话教练，按需查询数据而非一次性塞满上下文 |
| 训练记录 | 力量动作归一化，按活动编辑组数/次数/重量 |
| 语音输入 | ASR 转写 + 语音消息 |
| 推送 | Firebase Cloud Messaging（代码就绪，需自备服务账号） |

---

## 架构

```
┌──────────────────────────────────────────────┐
│  客户端                                       │
│  ├── Android App (Kotlin / Compose)          │
│  └── Web 仪表盘 (static/index.html)          │
└───────────────────┬──────────────────────────┘
                    │ HTTPS / JSON
┌───────────────────▼──────────────────────────┐
│  FastAPI (uvicorn, 默认 8766)                 │
│  ├── api/          HTTP 路由（薄壳）           │
│  ├── agent/        Agent 主循环 + 工具调用     │
│  ├── llm/          LLM 客户端（多协议适配）    │
│  ├── services/     业务逻辑（安全档位/同步）    │
│  ├── db/           SQLAlchemy 模型与仓储       │
│  └── sync/         手表数据拉取（当前：佳明）  │
└───────────────────┬──────────────────────────┘
                    │
           ┌────────▼────────┐
           │  SQLite / MySQL  │
           └─────────────────┘
```

> `api / agent / llm / services / db` 是**重构目标**。当前代码仍在过渡期，
> 路由在 `garmin_coach/api/routers/`，业务在 `garmin_coach/services/`，Agent 在 `garmin_coach/agent/`；见下方「目录结构」。

### 一次对话的生命周期（目标形态）

```
用户提问
  → api/chat 路由（鉴权、取 user_id）
  → Agent.run()
      ├─ 构建 system prompt（含安全档位硬约束）
      ├─ 调 LLM，模型决定调用哪些工具
      ├─ 工具执行（查最近 N 天 / 单次活动 / 天气 / 安全档位 …）
      └─ 循环直到模型不再调用工具，输出最终回复
  → 落库到 chat_history
  → 返回给客户端
```

---

## 快速开始

需要 Python 3.11+。

### 1. 装依赖

```bash
git clone https://github.com/lvxiaowu777/garminsportassistant.git
cd garminsportassistant
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

`.env` 至少填：

```ini
DEEPSEEK_API_KEY=your-key
QWEATHER_API_KEY=your-qweather-key
QWEATHER_API_HOST=your-qweather-host

# 本地开发用 SQLite（首次启动自动建表）
DATABASE_URL=mysql+pymysql://root:密码@127.0.0.1:3306/garmin_coach?charset=utf8mb4
# 生产用 MySQL：
# DATABASE_URL=mysql+pymysql://user:pass@localhost:3306/garmin_coach?charset=utf8mb4

# 生产环境务必设置
JWT_SECRET=一串足够长的随机字符串
```

### 3. 启动

```bash
python -m garmin_coach.server
# → http://localhost:8766
```

访问该地址就是 Web 仪表盘；Android App 把 `BASE_URL` 指向同一地址即可。

---

## API 概览

约 40 个接口，分五组。除 `/api/auth/*` 外都需要 `Authorization: Bearer <jwt>`。

| 组 | 代表接口 |
|---|---|
| 认证 | `POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me` |
| 手表 | `POST /api/watch/bind`、`POST /api/watch/bind-mfa`、`GET /api/watch/list` |
| 健康数据 | `GET /api/health/dashboard`、`GET /api/health/recent/{days}`、`GET /api/health/activity/{id}` |
| 对话 | `POST /api/chat/stream`（SSE）、`POST /api/chat/voice`、`GET /api/chat/history` |
| 推送 | `POST /api/push/register`、`POST /api/push/test` |

---

## 目录结构

```
garmin-coach/
├── garmin_coach/
│   ├── agent/                    # Agent 层（一个会话一个实例）
│   │   ├── loop/                 # agent.py（事件生成器）· events.py
│   │   ├── session/              # manager.py（生命周期/落库/恢复）· session.py
│   │   ├── conversation/         # manager.py（消息本）· serialize.py
│   │   ├── prompts/              # base.py（唯一一份提示词）
│   │   ├── tools/                # base · registry · health（9 个工具）
│   │   ├── llmclient/            # base · openai · anthropic · config · factory
│   │   ├── memory/               # store · inject · extract · manage · outbox
│   │   └── bridge.py             # 路由调用入口
│   ├── api/                      # deps · schemas · routers（7 个）
│   ├── services/                 # safety · safety_rules · trace · freshness · asr · clock
│   ├── integrations/             # garmin_client · garmin_sync · weather · push
│   ├── db/                       # database · models
│   ├── auth.py · settings.py · server.py · __main__.py
├── static/                       # Web 仪表盘（单文件，SSE 流式渲染）
├── scripts/                      # seed_dev_data · smoke_eval · migrate_session_id
├── config/                       # .tokens/{user_id}/ 手表令牌（gitignored）
├── docs/                         # CODE-MAP / PROJECT-OVERVIEW / REFACTOR
├── requirements.txt
├── deploy.py · deploy_config.example.py
└── .env.example
```

> 启动方式：`python -m garmin_coach.server`（在项目根执行；取代原来的 `src/server.py`）。

---

## 重构路线

现状：`src/` 已清空（只剩启动器），数据层 / Agent / 工具 / 传输层 / 外部服务都在
`garmin_coach/` 包里；对话走真正的工具调用循环（Agent 事件流 + SSE）。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 代码入库、凭据外置、可回滚基线 | ✅ 完成 |
| 1 | `LLMClient` 抽象（多协议 + 流式事件） | ✅ 完成 |
| 2 | `Tool` 基类 + `ToolRegistry`，数据查询工具化 | ✅ 完成 |
| 3 | `Agent.run()` 主循环 + `ConversationManager` | ✅ 完成 |
| 4 | 路由拆分 + 会话化 + 持久化 + 记忆 + 双协议 + 流式 + trace | ✅ 完成 |
| 5 | 42 题评测、安全加固（WS 验签 / JWT exp）、前端接流式 | 待做 |

阶段 1-3 已落地：对话从「把全部数据拼进 prompt 的单轮调用」改造成
「模型自己调工具查数据」的 Agent 循环，9 个工具，接口契约完全兼容。
详见 [`docs/PROJECT-OVERVIEW.md`](docs/PROJECT-OVERVIEW.md)（整体架构、Agent 循环、
工具层、记忆管理、提示词设计、部署）。

**为什么只重构对话这条线**：其余约 38 个 CRUD 路由不需要 LLM，
硬套 Agent 架构属于过度设计。分层只对「对话」有价值。

---

## 安全

- 凭据一律走环境变量或 gitignored 文件，代码里没有硬编码密钥
- `config/.tokens/`（手表令牌）、`.env`、`.deploy.env` 均已排除在版本库外
- 密码用 PBKDF2-SHA256（10 万轮 + 随机盐）存储，不存明文
- 生产环境务必设置 `JWT_SECRET`，内置默认值只用于本地开发

---

## License

个人项目，未指定开源许可证。

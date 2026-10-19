# Garmin Coach

> 运动数据聚合 + AI 教练。把佳明（Garmin）和高驰（COROS）手表的数据汇总成健康档案，
> 由一个会调用工具的 AI 教练回答「今晚跑不跑」「我这周恢复得怎么样」这类问题。

后端 FastAPI + SQLAlchemy；前端两个：原生 Android App（Jetpack Compose）和内置 Web 仪表盘。

---

## 能力

| 能力 | 说明 |
|---|---|
| 多用户账号 | 手机号注册/登录，JWT 鉴权，数据按 `user_id` 隔离 |
| 手表绑定 | 佳明（账号密码 + MFA 两步验证）、高驰（邮箱 + 区服） |
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
│  └── sync/         佳明 / 高驰 数据拉取        │
└───────────────────┬──────────────────────────┘
                    │
           ┌────────▼────────┐
           │  SQLite / MySQL  │
           └─────────────────┘
```

> `api / agent / llm / services / db` 是**重构目标**。当前代码仍在过渡期，
> 路由和业务逻辑集中在 `src/server.py`，见下方「重构路线」。

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
DATABASE_URL=sqlite:///./src/data/garmin_coach.db
# 生产用 MySQL：
# DATABASE_URL=mysql+pymysql://user:pass@localhost:3306/garmin_coach?charset=utf8mb4

# 生产环境务必设置
JWT_SECRET=一串足够长的随机字符串
```

### 3. 启动

```bash
python src/server.py
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
| 对话 | `POST /api/chat`、`GET /api/chat/history`、`WS /ws/chat` |
| 推送 | `POST /api/push/register`、`POST /api/push/test` |

---

## 目录结构

```
garmin-coach/
├── src/
│   ├── server.py               # FastAPI 应用与全部路由
│   ├── database.py             # 数据访问层（SQLAlchemy）
│   ├── models.py               # ORM 模型
│   ├── llm_client.py           # LLM 与 ASR 调用
│   ├── safety_rules.py         # 安全档位规则引擎
│   ├── auth.py                 # 密码哈希 + JWT
│   ├── push_service.py         # FCM 推送
│   ├── garmin_server_sync.py   # 佳明同步（含 MFA）
│   ├── coros_server_sync.py    # 高驰同步
│   ├── weather_server.py       # 天气同步
│   └── frontmatter.py          # Markdown 元数据解析（早期数据格式）
├── static/                     # Web 仪表盘（单页）
├── config/                     # 城市缓存；.tokens/ 为手表令牌（gitignored）
├── requirements.txt
├── .env.example
└── deploy_config.example.py    # 部署脚本凭据模板
```

---

## 重构路线

现状：能跑，但 `server.py` 一个文件装了约 40 个路由，对话走的是「把数据拼成一大段
文本塞进 system prompt」的单轮做法。目标是把对话换成真正的工具调用循环。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 代码入库、凭据外置、可回滚基线 | ✅ 完成 |
| 1 | `LLMClient` 抽象（多协议 + 流式事件） | ✅ 完成 |
| 2 | `Tool` 基类 + `ToolRegistry`，数据查询工具化 | ✅ 完成 |
| 3 | `Agent.run()` 主循环 + `ConversationManager` | ✅ 完成 |
| 4 | `server.py` 瘦身为薄路由 | 待做 |
| 5 | 测试、文档、发布 | 待做 |

阶段 1-3 已落地：对话从「把全部数据拼进 prompt 的单轮调用」改造成
「模型自己调工具查数据」的 Agent 循环，9 个工具，接口契约完全兼容。
详见 [`docs/INTERVIEW-GUIDE.md`](docs/INTERVIEW-GUIDE.md)（技术细节与设计取舍）。

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

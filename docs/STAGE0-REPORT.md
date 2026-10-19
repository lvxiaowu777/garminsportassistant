# 阶段 0 完成报告

> 执行日期：2026-10-XX（你出门期间）
> 范围：只做阶段 0（入库 + 安全清理 + 可启动验证），没动任何业务逻辑。

---

## 🔴 第一件事：请立刻更换服务器 SSH 密码

原先 **9 个部署脚本**（`deploy.py` / `start_server.py` / `install_nginx.py` /
`setup_https.py` / `get_cert.py` / `check_server.py` / `quick_check.py` /
`config_proxy.py` / `start_nginx.py`）里，**每一个都以明文写着服务器 IP、用户名和
SSH 密码**。

我已经把这些值全部抽到 `.deploy.env`（gitignored，不会进版本库），代码里不再有。
但请注意：

- 这些明文**曾经真实存在过**。如果 `study\知识库` 这个目录被 iCloud / 坚果云 /
  OneDrive 之类的工具同步过，密码已经上云了。
- 如果这个目录曾经给任何人看过、或曾经误传到任何地方，应当视为已泄露。

**建议动作（按优先级）**：

1. 登录服务器改 SSH 密码：`passwd`
2. 更好的做法是改用密钥登录并关掉密码登录：
   `~/.ssh/authorized_keys` 放公钥，`/etc/ssh/sshd_config` 里
   `PasswordAuthentication no`，然后 `systemctl restart ssh`
3. 改完之后更新本地的 `.deploy.env` 里的 `GARMIN_PASS`

> 如果已经用密钥登录，第 1 步可以跳过，但仍建议确认 `PasswordAuthentication no`。

---

## 做了什么

### 代码入库

| 项目 | 位置 | 跟踪文件 | 标签 |
|---|---|---|---|
| 后端 | `study\project\garmin-coach` | 50 | `v0.1.0-baseline`（重构前）、`v0.1.1-stage0-done` |
| 前端 | `study\project\garmin-coach-android` | 42 | 无（首个提交） |

原目录 `知识库\试用\garmin-coach` 和 `code\garmin-coach-android` **完整保留未动**，
可以随时对照。

两个仓库都是独立 git repo，后续可以分别推到 GitHub。

### 安全清理

| 项目 | 处理 |
|---|---|
| 9 个部署脚本的 IP/账号/SSH 密码 | 抽到 `.deploy.env`，新增 `deploy_config.py` 统一加载 |
| `database.py` 里的 MySQL 账号密码 | 删除默认值，改为强制从 `DATABASE_URL` 环境变量读 |
| 真名"吕游"（36 处，主要在 SYSTEM_PROMPT） | 改为"用户" |
| 真实手机号（迁移脚本默认值等） | 改为 `13800138000` |
| 真实域名 `*.space` | 改为 `example.com` |
| 含服务器信息的 5 份运维文档 | 移入 `docs-private/`（gitignored） |
| `.env` / `.deploy.env` / token / 数据库 / 日志 | 全部 gitignored |

**验证方式**：`git grep --cached` 扫描过真实 IP、密码、手机号、域名、真名，
已跟踪文件里零命中。

### 让服务能在本地跑起来（原代码跑不起来）

原本 `python src/server.py` 直接崩，有三层阻断：

1. **缺 `apscheduler`** —— `server.py:896` 的定时调度 import 它，`requirements.txt` 里没有。
2. **缺 `paramiko`** —— 9 个部署脚本依赖它，也没写进 requirements。
3. **绑死 MySQL** —— `database.py` 直接用 `sqlalchemy.dialects.mysql.insert` +
   `on_duplicate_key_update`，SQLite 渲染不了 `OnDuplicateClause`，启动即抛
   `UnsupportedCompilationError`。

第 3 条我改成了跨方言实现：新增 `_insert_stmt` / `_upsert` / `_append_concat` /
`_upsert_append` 四个辅助函数，按 `engine.dialect.name` 分派，替换掉 8 处 MySQL
专有写法。其中 notes 字段的追加原来用 `func.concat`（MySQL 专有函数），
改成了跨方言的 `||` 拼接。

**收益**：现在本地不装 MySQL 也能开发，阶段 5 的集成测试可以用内存 SQLite。

另外 `push_service.py` 的 `import firebase_admin` 写成了硬依赖，已改为可选
（try/except 软导入），没装时推送功能降级，其余功能不受影响。

### 新架构骨架

新建 `garmin_coach/` 包，六个子包只有 `__init__.py` 写清职责与 MewCode 对应关系，
暂无实现：

```
garmin_coach/
├── api/       HTTP 路由（薄壳）
├── agent/     Agent 主循环        ← mewcode/agent.py
├── llm/       LLM 传输与流式解析   ← mewcode/client.py
├── tools/     工具注册表          ← mewcode/tools/
├── services/  领域规则（安全档位）
└── db/        数据访问（SQLAlchemy）
```

采用**绞杀者模式**：旧代码留在 `src/` 继续跑，新代码逐个模块搬进来，
搬完一个就在旧文件留转发，直到 `src/` 可以整体删除。这样每一步都可回滚。

---

## 验证结果

用的是 SQLite（`src/data/garmin_coach.db`，你本地的旧数据副本）：

```
python src/server.py
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8766

GET /                     → 200  (44042 bytes, Web 仪表盘)
GET /api/llm/providers    → 200
GET /api/health/safety    → 200
```

所有 `src/*.py` 通过 `py_compile` 语法检查。

---

## 你回来后可以这样确认

```powershell
# 1. 看提交历史
cd C:\Users\boobyboy\study\project\garmin-coach
git log --oneline --decorate
git diff v0.1.0-baseline..HEAD --stat    # 看我到底改了什么

# 2. 自己启动一次
.\venv\Scripts\Activate.ps1
python src/server.py
# 浏览器打开 http://localhost:8766

# 3. 确认没有敏感文件被跟踪
git ls-files | Select-String "\.env$|\.deploy\.env|\.tokens|\.db"
```

依赖已经装好（`venv/` 已建，不在版本库里）。

---

## 需要你决定的事

**1. GitHub 仓库名和可见性**
我没法替你建远程仓库。你决定名字后用：

```powershell
cd C:\Users\boobyboy\study\project\garmin-coach
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin master --tags
```

前端仓库同理。**推之前再跑一次上面第 3 条检查**。

**2. 要不要把两个仓库合并成一个 monorepo**
现在是两个独立仓库。合并的好处是 Android 和后端版本能对齐；
坏处是 clone 后端的人会连带下载 Android 源码。我建议保持分开。

**3. 阶段 1 从哪开始**
按 `docs/REFACTOR.md`，阶段 1 是 LLM 传输层（`garmin_coach/llm/`），
成果是「不改 `src/` 任何代码就能把 `StreamEvent` 流打到终端」。
这是后面所有工作的地基，也是最独立、风险最低的一块。

---

## 关于网站/域名（你说的第二阶段）

现状：`nginx` 相关脚本（`install_nginx.py` / `config_proxy.py` / `get_cert.py` /
`setup_https.py`）都已脱敏并在入库，但**这次没动它们**，也没动服务器配置。

域名被个人网站占用这个问题，常见的三种解法（等你决定再动）：

- **子域名**：`garmin.你的域名` → 新 server block，最干净
- **子路径**：`你的域名/garmin/` → nginx `location /garmin/` 反代，但要改前端所有请求路径
- **换端口直连**：`你的域名:8766` → 不走 443，最简单但地址难看、且部分网络会拦非常规端口

具体等你说清楚"个人网站"和这个服务分别要怎么访问，再出方案。

---

## 遗留的技术债（已记在 REFACTOR.md）

- `database.py` / `llm_client.py` 顶部各有一段手写 `.env` 解析，
  且硬编码了 `C:\Users\boobyboy\study\.env` 这个绝对路径，换机器就失效。阶段 1 清掉。
- `requirements.txt` 没锁版本上限，建议阶段 5 生成 lock 文件。
- `server.py` 里 `WebSocket` 和 `scheduler` 都在模块级启动，测试时不好隔离，阶段 4 处理。

# garmin-coach：基础信息改造 + AI 训练档案注入（2026-08-20）

> 把基础信息从 onboarding 流程搬到账号页面（可编辑）、新增训练类型 + 比赛字段（含倒计时）、AI 训练建议读取用户档案。
> 关联：[[踩坑与操作]] | [[启动机制与构建策略]] | [[../服务器操作指南]] | [[../../../../../.claude/projects/C--Users-boobyboy-study----/memory/garmin-coach项目意向|garmin-coach项目意向]]

---

## 1. 改造目标

| Before | After |
|---|---|
| 新用户注册后强制走 `ProfileScreen` 全屏 onboarding 收集 10 个基础字段 | 直接进 dashboard，账号页面内"未完善"banner 引导填 |
| 基础信息只能在首次填一次 | 账号页面**全字段可编辑** |
| 账号页面只显示手机号+注册时间 | 账号页面展示基础信息 + 训练类型 + 比赛（含倒计时） |
| AI 训练建议不读用户目标 | AI 读取训练目标 / 训练类型 / 即将比赛，写入 prompt |
| onboarding 检测靠客户端 SharedPreferences | 后端 `goal IS NOT NULL` 决定 `profile_completed` |

---

## 2. 数据模型

### User 表新增 2 列（JSON TEXT）

| 列 | 类型 | 存什么 |
|---|---|---|
| `training_types` | TEXT | JSON 数组，如 `["marathon","half_marathon"]` |
| `races` | TEXT | JSON 数组，如 `[{"name":"上海马拉松","date":"2026-11-30"}]` |

迁移机制：复用现有 `_ensure_user_columns` 幂等 ALTER TABLE 模式（idempotent，新装机和旧装机都安全）。

---

## 3. 关键设计决策

- **`profile_completed` 标志**：`goal IS NOT NULL`。不新增列——goal 字段本身语义上就是"用户有没有选定训练方向"，符合"清空 goal = 未提交"的语义。
- **JSON 存储**：与 `hr_zones` / `splits` / `plan_json` 风格一致。MySQL `TEXT` 列存 JSON 字符串，应用层解析。
- **空数组语义**：客户端始终发 `JSONArray()`（空数组），服务端能正确清空——避免"未设" vs "清空" 歧义。
- **比赛日期**：ISO `YYYY-MM-DD` 字符串，无时区。客户端 `java.time.LocalDate.parse`（API 26+）。
- **历史比赛**：不删。客户端显示"已结束"灰色，服务端 `build_chat_context` 只喂 `days >= 0` 的未来比赛给 AI。

---

## 4. 后端改动

### `src/models.py`（User 模型，line 44 后）

加两列：

```python
training_types = Column(Text)   # JSON: ["marathon","half_marathon",...]
races = Column(Text)            # JSON: [{"name":"上海马拉松","date":"2026-11-30"}]
```

### `src/database.py`

- **`_ensure_user_columns`** (line 60-74) 加 2 列到 `new_cols` dict
- **`_PROFILE_COLS`** (line 854-856) 加 2 字段
- **`get_user_profile`** (line 859-882) 返回 dict 前对 `training_types` / `races` 做 JSON 解码，失败 fallback 到空列表
- **`update_user_profile`** (line 885-901) 收到 list/dict 时 JSON 序列化再写库

### `src/server.py`

**`/api/auth/me` 重写**（line 238-284）：返回字段从 `{user_id, phone, nickname, created_at}` 改成 `{user_id, profile_completed, 10 个基础字段, training_types, races}`。**去掉 phone/created_at/nickname**。

**`UpdateProfileRequest`**（line 552-557）加：`training_types: list[str] | None`、`races: list[dict] | None`。

**`build_chat_context`**（line 144-186）末尾：调 `get_user_profile(user_id)`，构造 `profile_payload` 含 `goal` / `goal_target` / `training_types` / `upcoming_races`（按距赛天数排序）/ `soonest_race_days`，塞进返回 dict 的 `profile` 字段。

### `src/llm_client.py`

**`build_context`**（line 196-213 加）：在"安全档位"块后插"用户训练档案"section——训练目标（中文标签）、训练类型（中文）、未来比赛列表（含距赛天数）。

---

## 5. Android 改动

### `data/model/Models.kt`

- 新 `Race` data class: `name`、`date`
- `UserProfile` 加 `training_types: List<String>` + `races: List<Race>`

### `data/api/GarminApi.kt`

- **`updateUserProfile`** (line 136-180)：JSON body 加 `training_types`（始终发 `JSONArray`）和 `races`（`JSONArray`，每项是 `{name, date}` 对象）—— 空数组也发让服务端能清空
- **`parseUserProfile`** (line 159-198)：解析 `training_types` JSONArray + `races` JSONArray（每项构造 `Race` 对象）
- `getMe()` 不动（仍返回 raw JSONObject，AccountScreen 用 `opt*` 取新字段）

### `ui/auth/ProfileScreen.kt`

签名加 `prefill: UserProfile? = null`，所有 `remember { mutableStateOf("") }` 用 prefill 值初始化。保存时用 `prefill?.training_types ?: emptyList()` 和 `prefill?.races ?: emptyList()` 保留这两个字段。

### `ui/auth/AccountScreen.kt` — 整体重写

新结构：
1. **顶部"未完善" banner**（仅 `!profileCompleted` 时显示）
2. **基础信息卡片** —— 身高 / 体重 / 年龄 / 训练频率 / 性别 chips / 目标 chips / 目标成绩 / 训练经验
3. **训练类型 chips** —— 全马 / 半马（多选）
4. **我的比赛列表** —— 每行：名字 TextField + 日期 TextField + 倒计时（"还有 N 天" / "今天比赛" / "已结束"） + 删除按钮 + 底部"添加比赛"按钮
5. **保存按钮** —— 一把存 `updateUserProfile(profile)`
6. **设备管理入口** —— 跳 WatchBindingScreen（保留）
7. **退出登录** —— 保留

`daysUntil()` helper：`ChronoUnit.DAYS.between(today, race.date)`，`>0` 还有 N 天 / `==0` 今天比赛 / `<0` 已结束。

### `MainActivity.kt` — 状态机精简

- 删除 `KEY_PROFILE_LEGACY`、`keyProfile()`、`isProfileFilled()`、`showProfile` state
- `runLegacyMigration` 删 `KEY_PROFILE_LEGACY` 处理（保留 `KEY_BOUND_LEGACY`）
- `onCreate` 删 `profileFilled` / `showProfile` 计算
- `loginSuccess` 三分支 `when` 变两分支：`isBound` → `goToMain()`，否则 → `showBinding = true`
- 删 `showProfile` 整段分支
- onboarding 流变：`Auth → WatchBinding（如果未绑）→ MainApp`。首次填基础信息从账号页面 banner 进入。

---

## 6. API 契约

### `GET /api/auth/me`

请求：Bearer token  
响应（200）：
```json
{
  "user_id": 8,
  "profile_completed": true,
  "height_cm": 175.0,
  "weight_kg": 65.0,
  "age": 28,
  "gender": "male",
  "body_fat_pct": null,
  "muscle_mass_kg": null,
  "goal": "half_marathon",
  "goal_target": "1:55:00",
  "training_years": 3.0,
  "training_freq_per_week": 4,
  "training_types": ["marathon","half_marathon"],
  "races": [{"name":"上海马拉松","date":"2026-11-30"}]
}
```

### `POST /api/user/profile`

请求：
```json
{
  "height_cm": 175.0,
  "weight_kg": 65.0,
  "age": 28,
  "gender": "male",
  "body_fat_pct": 15.0,
  "muscle_mass_kg": 50.0,
  "goal": "half_marathon",
  "goal_target": "1:55:00",
  "training_years": 3.0,
  "training_freq_per_week": 4,
  "training_types": ["marathon","half_marathon"],
  "races": [{"name":"上海马拉松","date":"2026-11-30"}]
}
```
响应：`{"ok": true}`

---

## 7. 验证 Case

| Case | 期望 |
|---|---|
| 全新注册 → 进 dashboard | 无 ProfileScreen 强制流，账号页"未完善"banner |
| 填 goal=half_marathon + 加比赛 → 保存 → 重开 | banner 消失，倒计时显示 |
| 比赛日期已过 | 客户端显示"已结束"灰色，AI 不读 |
| 改 goal=shaping 不加比赛 → chat 问"我该练什么" | AI 回答反映塑形目标，不含比赛信息 |
| 切换登录（不同账号） | 各自独立 profile_completed 状态 |
| 退出登录（token 清空） | 基础信息+训练类型+比赛服务端保留 |

---

## 8. 边界与不做的事

- **客户端 `profile_<uid>` 旧键**（per-user onboarding 改动遗留）：变成死键，忽略。下次 `runLegacyMigration` 跑过 `onboarding_migrated_v1=true` 后不会再写它。
- **未做**：服务端 logout 撤销 token 接口（之前 plan 提的，需求里没要）。
- **未做**：账号页面内的"添加比赛"快捷模板（比赛模板等）。

---

相关：[[踩坑与操作]] | [[启动机制与构建策略]] | [[../服务器操作指南]] | [[../../../../../.claude/projects/C--Users-boobyboy-study----/memory/garmin-coach项目意向|garmin-coach项目意向]]
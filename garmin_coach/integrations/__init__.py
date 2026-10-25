"""外部服务对接层（可替换的适配器）。

```
garmin_client.py   单日/单活动拉取（garminconnect 的薄封装，纯解析）
garmin_sync.py     编排：登录 / MFA / token 落盘 / 逐天入库 / 定时全量
weather.py         和风天气：经纬度 → 天气 → 入库
push.py            FCM 推送（可选依赖，未装则整体降级）
```

放进一个 `integrations/` 而不是塞进 `services/` 的原因：
这些模块**依赖第三方网络与凭证**，单元测试时应当被 mock 掉；
`services/` 里是纯业务规则（安全档位、记忆、trace），不碰外部网络。
"""

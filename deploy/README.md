# 部署

两台机器各自每天 12:30 跑一次增量同步：`python -m wechat_vault.incremental`。失败时发飞书群通知（见下）。

流程：取云端该来源（`win` / `mac`）的水位（`wx_sync_state` 的最大 `last_create_time`），回溯 7 天，
直读本机数据库导出这段时间的消息和媒体，再幂等合并到 Tablestore / OSS。已存在的消息按 `(conv_id, msg_uid)` 跳过，
已关联媒体的行不覆盖。同一来源同时只会跑一个实例（文件锁），上一次未结束则本次直接跳过。
每次成功后把摘要写到 `manifests/<account_id>/<source>/incremental-latest.json`。

常用参数：`--dry-run`（只导出校验，不写云端）、`--full`（全量，恢复用）、`--since EPOCH`、`--lookback-days`、`--no-media`。
首次运行或云端没有该来源的水位时，必须带 `--full` 或 `--since`。

## macOS launchd

`launchd.plist.example` 是模板，每天 12:30（`StartCalendarInterval`，登录/加载时也会跑一次，幂等）：

1. 确认 `uv` 与 `infisical` 在登录 shell 的 PATH 中（模板用 `zsh -lc`）。
2. 建日志目录：`mkdir -p ~/Library/Logs/wechat-vault`。
3. 复制到 `~/Library/LaunchAgents/com.makewheels.wechat-vault.plist`。
4. 加载：`launchctl load ~/Library/LaunchAgents/com.makewheels.wechat-vault.plist`。

密钥由 `run-mac.sh` 按账号和设备从 Infisical 注入，直接读取本机数据库，不依赖 WeFlow。

## Windows 任务计划

- `run-windows.ps1`：从 Infisical 读取云端凭据和本机设备密钥（仅进当前进程环境变量），自动选可连通的代理（北京 → 中继），
  跑增量同步，日志在 `%LOCALAPPDATA%\wechat-vault\logs\incremental-YYYYMMDD.log`（保留 30 天）。多余参数原样传给 `incremental`。
- `register-windows-task.ps1`：注册任务 `wechat-vault-incremental`，默认每天 12:30（`-At HH:mm` 可改），仅当前用户登录时运行
  （Infisical CLI 登录按用户保存），错过的会在开机后补跑，上一次未结束则忽略新实例。

```powershell
powershell -ExecutionPolicy Bypass -File deploy\register-windows-task.ps1 [-At 12:30]
Start-ScheduledTask wechat-vault-incremental     # 手动触发
Unregister-ScheduledTask wechat-vault-incremental -Confirm:$false   # 卸载
```

## 失败通知（飞书）

同步失败（导出/校验/导入出错）时 `incremental` 向飞书群发一条消息：来源、主机、错误类型。`--no-notify` 关闭，`--test-notify` 发测试消息。
`notify.py` 优先用开放平台 API（`FEISHU_APP_ID` + `FEISHU_APP_SECRET` + `FEISHU_CHAT_ID`），否则用已登录机器人的 `lark-cli`（只需 `FEISHU_CHAT_ID`）。
`FEISHU_CHAT_ID`、`FEISHU_APP_ID` 在 Infisical `/wechat-vault`。Mac 靠 `lark-cli`（launchd 下可访问钥匙串，ssh 里不行）；
Windows 没有 `lark-cli`，需要在 Infisical 补 `FEISHU_APP_SECRET` 才能发。
通知本身不会因发送失败而改变退出码；Windows 的代理/密钥读取阶段失败（还没进 Python）只写日志，不会通知。

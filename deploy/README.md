# 部署

## macOS launchd

`launchd.plist.example` 是模板：

1. 确认 `uv` 与 `infisical` 的绝对路径（`which uv` / `which infisical`），必要时改模板。
2. 建日志目录：`mkdir -p ~/Library/Logs/wechat-vault`。
3. 复制到 `~/Library/LaunchAgents/com.makewheels.wechat-vault.plist`。
4. 加载：`launchctl load ~/Library/LaunchAgents/com.makewheels.wechat-vault.plist`。

模板已改为按账号和设备从 Infisical 注入密钥，直接读取本机数据库，不依赖 WeFlow。
当前直读命令每次做全量快照再幂等导入，模板间隔仍为 15 分钟；正式启用前应按数据量调整间隔。
本次仅更新模板，未安装或启用定时任务。媒体使用主 README 中的独立命令。

## Windows

Windows 侧由 `HANDOFF-WINDOWS.md` 的导出器负责，不需要跑这个 CLI。

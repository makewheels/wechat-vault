# 部署

## macOS launchd

`launchd.plist.example` 是模板：

1. 确认 `uv` 与 `infisical` 的绝对路径（`which uv` / `which infisical`），必要时改模板。
2. 建日志目录：`mkdir -p ~/Library/Logs/wechat-archive`。
3. 复制到 `~/Library/LaunchAgents/com.makewheels.wechat-archive.plist`。
4. 加载：`launchctl load ~/Library/LaunchAgents/com.makewheels.wechat-archive.plist`。

每 15 分钟跑一次增量；WeFlow 没开时 `sync` 会报错退出，launchd 下次再试。

## Windows

Windows 侧由 `HANDOFF-WINDOWS.md` 的导出器负责，不需要跑这个 CLI。

# wechat-archive

微信聊天归档：WeFlow（读微信本地库）→ Tablestore（消息/会话/同步状态）+ OSS（解密后的媒体）。

## 文档

- [DESIGN.md](DESIGN.md)：总体设计、表结构、去重规则、媒体解密、成本、实施阶段。
- [HANDOFF-WINDOWS.md](HANDOFF-WINDOWS.md)：给 Windows 机器的 AI 的交接契约（暂存包格式、导出算法、验收）。

## 当前状态

- 设计定稿（2026-11-22），已确认：同一个微信号、Mac/Windows 都用 WeFlow、媒体全量（含视频解密）、新建 Tablestore 实例、全量会话含公众号。
- 云资源尚未创建；Windows 侧可先按交接文档做导出器。
- 代码未开始（P1）。

## 边界

- 只读微信数据库；不做自动回复/自动发送。
- 聊天记录与媒体只进私有 OSS / Tablestore，不进 Git。
- 凭据统一走 Infisical，不落仓库。

## 成本量级

Tablestore 约 3～6 元/月，OSS 43GB 约 5～7 元/月，合计约 10～12 元/月；首月含一次性写入约 12～15 元。以实际账单为准。
# wechat-vault

微信聊天归档：WeFlow（读微信本地库）→ Tablestore（消息/会话/同步状态）+ OSS（解密后的媒体）。

## 文档

- [DESIGN.md](DESIGN.md)：总体设计、表结构、去重规则、媒体解密、成本、实施阶段。
- [HANDOFF-WINDOWS.md](HANDOFF-WINDOWS.md)：给 Windows 机器的 AI 的交接契约（暂存包格式、导出算法、验收）。
- [LICENSE](LICENSE)：MIT。

## 当前状态

- 设计定稿（2026-11-22）：同一个微信号、Mac/Windows 都用 WeFlow、媒体全量（含视频解密）、新建 Tablestore 实例、全量会话含公众号。
- Mac 侧 CLI 已实现：`init` / `import-staging` / `sync` / `media` / `verify` / `stats`，18 个单测通过。
- Windows 侧按 `HANDOFF-WINDOWS.md` 产出暂存包；Mac 用 `import-staging` 入库。
- 云资源（Tablestore 实例、OSS bucket、RAM）未创建；跑 `init` 前先注入凭据。

## 快速开始

```bash
uv sync

# 凭据只从环境/Infisical 来，不写进仓库
export ALIBABA_CLOUD_ACCESS_KEY_ID=...
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
export WEFLOW_ACCESS_TOKEN=...          # platform-accounts/dev /weflow

# 1) 建表（幂等）
uv run wechat-vault init

# 2) 先看 Windows 暂存包，不写云
uv run wechat-vault import-staging --dir /path/to/wechat-export-win --limit 500 --dry-run

# 3) 导入（媒体已在 OSS 则只 HEAD；本地有 media/ 则上传）
uv run wechat-vault import-staging --dir /path/to/wechat-export-win
# 或者 Windows 已上传到 OSS 暂存前缀：
uv run wechat-vault import-staging --oss-prefix staging/win/<run_id>

# 4) Mac 自己从 WeFlow 增量/全量
uv run wechat-vault sync --source mac --full
# 推断不到自己的 wxid 时：uv run wechat-vault sync --source mac --my-wxid wxid_xxx
uv run wechat-vault sync --source mac --media --limit-convs 3   # 冒烟

# 5) 校验
uv run wechat-vault verify
uv run wechat-vault stats
```

## 命令

| 命令 | 作用 |
|---|---|
| `init` | 创建 `wx_*` 表（已存在跳过） |
| `import-staging --dir ...` | 读 Windows 暂存包：消息写 Tablestore，媒体校验/上传 OSS |
| `sync --source mac` | 从本机 WeFlow 拉会话/消息；`--full` 忽略水位 |
| `media` | 处理 `wx_media_tasks` 待办媒体 |
| `verify` | 重复 `msg_uid`、每会话计数、媒体状态 |
| `stats` | 各表行数 |

## 表

`wx_accounts`、`wx_conversations`、`wx_messages`、`wx_contacts`、`wx_sync_state`、`wx_media_tasks`、`wx_conflicts`。字段和主键见 `DESIGN.md`。

## 免责声明

本项目仅用于导出、归档**自己的**微信聊天记录。读取/解密本地微信数据库可能违反微信用户协议；使用者需自行确认合规并承担风险。项目不包含解密实现，依赖 WeFlow。作者不对数据丢失、账号风控或滥用负责。

## 边界

- 只读微信数据库；不做自动回复/自动发送。
- 聊天记录与媒体只进私有 OSS / Tablestore，不进 Git。
- 凭据统一走 Infisical，不落仓库。

## 开发

```bash
uv run pytest
uv run ruff check src tests
```

## 成本量级

按 54 个包实测 491 B/条、全量 100～150 万条估：Tablestore 约 1～3 元/月，OSS 35～43GB 约 4～6 元/月，合计约 **5～9 元/月**；首月含一次性写入约 6～10 元。以实际账单为准。

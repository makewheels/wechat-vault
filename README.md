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
- 云资源已存在（2026-11-24 实查）：Tablestore 实例和私有 OSS 均为 `wechat-vault`。应用凭据不含建表/删行权限；现有库导入无需跑 `init`。

## Mac 直读与密钥隔离

Mac 的 WeFlow 当前卡在原生库初始化（`InitProtection -101`），改用仓库内直读器：
只读打开加密数据库、内存解密、校验各页 HMAC、合并已提交 WAL，验证暂存包后入云。
临时导出只放系统临时目录，退出时清理；正文和原始 XML 都移除媒体解密参数。

Infisical `tools/dev`：云凭据位于 `/wechat-vault`；设备密钥按
`/wechat-vault/<微信ID>/<机器标识>` 隔离。
当前 Mac 为 `/wechat-vault/wxid_vizj1s0qmxl922/mac-mint-mac-mini`，
Windows 为 `/wechat-vault/wxid_vizj1s0qmxl922/windows`（后者主机名未核实）。
各自使用 `WECHAT_DB_KEY`；Mac 图片密钥为 `WECHAT_IMAGE_AES_KEY`。
公共目录不再存设备密钥；禁止将 Mac 密钥注入 Windows。

```bash
export WECHAT_ACCOUNT_ID=wxid_vizj1s0qmxl922
export WECHAT_DEVICE_ID=mac-mint-mac-mini
# account_root 指向该账号本机目录，包含 db_storage/ 和 msg/
account_root="$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_vizj1s0qmxl922_2b0d"
sh deploy/run-mac.sh uv run python -u -m wechat_vault.direct_sync --account-root "$account_root"
sh deploy/run-mac.sh uv run python -u -m wechat_vault.local_media --account-root "$account_root" --account "$WECHAT_ACCOUNT_ID" --device "$WECHAT_DEVICE_ID"
sh deploy/run-mac.sh uv run python -u -m wechat_vault.media_finish --account-root "$account_root" --account "$WECHAT_ACCOUNT_ID" --device "$WECHAT_DEVICE_ID" --only-voice
# 等前三阶段完成，转换图片、关联媒体、备份去重并独立回读校验：
sh deploy/run-mac.sh uv run python -u -m wechat_vault.complete_mac --account-root "$account_root" --account "$WECHAT_ACCOUNT_ID" --device "$WECHAT_DEVICE_ID" --maintenance-profile default
# 已有数据去重：先只读检查；--apply 会先在私有 OSS 备份并回读验证，再删除等价重复行。
sh deploy/run-mac.sh uv run python -m wechat_vault.dedupe
# 清理已确认等价的历史重复（日常凭据不授予删除权限）：
```

## 增量同步（定时）

`python -m wechat_vault.incremental --account-root <账号目录> --source win|mac`：以云端水位回溯 7 天，
直读本机库导出消息+媒体并幂等合并。Windows 用 `deploy/run-windows.ps1` + `deploy/register-windows-task.ps1`
（任务计划，每小时），Mac 用 `deploy/launchd.plist.example`（每小时）。详见 `deploy/README.md`。

## 云端格式与去重

Tablestore 存项目自定义结构，**不是 ChatLab JSON**：`wx_messages` 每条消息一行，
保存会话、发送者、时间、类型、清洗后的正文/XML，以及 OSS 媒体 SHA-256 引用。
其余表保存账号、会话、联系人、同步水位、媒体任务和内容冲突。
若需导入 ChatLab，需另做格式转换；不能把表扫描结果直接当成 ChatLab 文件。

去重身份为 `(conv_id, msg_uid)`：优先使用服务端消息 ID，无 ID 时使用内容指纹。
设备本地 `sort_seq` 仅供排序，导入时复用同身份已有行的排序键。
历史重复清理按会话隔离，正文/发送者/时间不一致时保留并报告，不直接删除。
媒体按解密后 SHA-256 去重。原始 `wxgf` 解密容器保留，同时生成 JPEG 查看副本；
语音保留 SILK，未下载到本机的媒体明确标为缺失。索引和运行摘要位于私有 OSS `manifests/`。

## 原 WeFlow 路径（仅在其 API 可用时）

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

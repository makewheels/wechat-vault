# 微信聊天归档（wechat-archive）设计

> 目标：把 Mac 和 Windows 两台机器上的微信聊天记录，去重后归档到阿里云 Tablestore（消息）+ OSS（媒体/原始文件），可增量、可校验、可离线检索。
>
> 状态：设计稿 v2（2026-11-22）。已确认：同一微信号、Mac/Windows 都用 WeFlow、媒体全量且视频解密后入 OSS、新建 Tablestore 实例、全量会话含公众号。

## 0. 结论

- **能做**。不自己重新实现微信数据库解密：WeFlow 已经在做“解密 + 解析 + 暴露 API”，我们把它当作**数据源适配器**。以后 WeFlow 不可用，再换 DirectDB 适配器。
- **不把对象存储当数据库**：OSS 存媒体和原始导出（二进制/JSONL），Tablestore 存会话、消息、同步状态；本地 SQLite 只做检索副本。
- **双端去重**：消息用 `serverId` 做全局身份，媒体用 SHA-256 内容寻址；同一账号的两台机器最终合并成一份，不同账号按 `account_id` 隔离。
- **新建私有仓库** `makewheels/wechat-archive`，复用 `wechat-analyzer` 的 WeFlow 客户端代码；不复用 `weixin-chat-log`（老 Android 库结构，见 2.3）。
- **新建 Tablestore 实例** `wechat-archive`（已确认）；全量会话（含 431 个公众号）都存，媒体全量，视频解密后再入 OSS。
- **成本量级**：Tablestore 每月 3～6 元，OSS 43GB 约 5～7 元/月，合计 **10～12 元/月**；首月含一次性写入约 12～15 元。

## 1. 关键决策（ADR）

| # | 决策 | 理由 |
|---|---|---|
| D1 | WeFlow 作为唯一数据源，先不自己读微信 DB | 4.x 的 SQLCipher/密钥/媒体解密都由 WeFlow 处理；自研维护成本高、易被微信升级打断 |
| D2 | 每台机器本地跑 ingester，不把 WeFlow API 暴露到局域网 | API 只绑 `127.0.0.1`；旧方案 `netsh portproxy` 会把带 token 的接口暴露给整个 LAN |
| D3 | 新建 `wechat-archive` 私有仓库 | 归档是“写入服务”，与 chat-review（分析）、wechat-copilot（实时助手）、wechat-analyzer（导出骨架）职责不同 |
| D4 | 消息主键含 `msg_uid`，`sort_seq` 用来源 `sortSeq` | 双端同一条消息落到同一行；按会话时间范围扫描仍然高效 |
| D5 | 媒体按 SHA-256 内容寻址 | 同一张图在群里/私聊/两台机器上只存一份，天然去重 |
| D6 | 追加式归档，先写先得 | 备份语义：撤回/删除不应让归档丢原文；冲突单独记录，不覆盖 |
| D7 | 不建多元索引，v1 不做云端全文检索 | 多索引有预留 CU 成本；检索先落到本地 SQLite FTS5 |
| D8 | 不存任何带 token 的 URL 或图片 aeskey | 原始 XML 里含解密材料，落库前清洗 |

## 2. 现状与可复用资产

### 2.1 已有工具

| 项目 | 定位 | 与本设计的关系 |
|---|---|---|
| [wechat-analyzer](../../media/wechat-analyzer/README.md) | WeFlow 导出器（分页、媒体下载、断点续传）+ 解析/分析骨架 | **复用它的 WeFlow 客户端**，不直接依赖仓库 |
| [wechat-copilot](../wechat-copilot/docs/决策记录.md) | 实时读最近 N 条，生成回复/记忆 | 不相关；未来可读归档库 |
| [chat-review](../chat-review/README.md) | 全量导出 + 复盘 HTML + OSS 发布 | 产物是分析页，不是归档 |
| `weixin-chat-log`（GitHub） | 老版微信 DB 解析（Android `EnMicroMsg.db` / `WxFileIndex`，Spring Boot 2.2） | **只作参考**，结构与微信 4.x 桌面端不同，不基于它改 |

### 2.2 已有数据

- 54 个相亲对象、**85,281 条**纯文本消息，在 `chat-review/data/weflow/zipped/*.zip`（每 zip 一个 `messages.json`，共 7.4MB）。
- 原始全量导出是 1452 会话（431 公众号 + 211 群 + 789 一对一 + 系统号）/ 43GB（含媒体），**当前不在本机**；需要时用 WeFlow 重新导出，或从备份盘找。本次**全量都存**。
- 消息字段：`localId / serverId / createTime / sortSeq / isSend / senderUsername / localType / content / rawContent / parsedContent`（媒体消息另有 `mediaUrl / mediaFileName` 等）。
- 若 zip 内有重复 `serverId`，去重后的唯一消息数会略少于 85,281；验收按唯一 `msg_uid` 计。

### 2.3 关于“直接读数据库”

`weixin-chat-log` 解析的是 Android 时代的 `EnMicroMsg.db`，微信 4.x 桌面端已经换成新的加密库 + 新的消息表，老代码不能直接用。WeFlow 4.5.1 本身就是“直接读库”的工具：它持有 `dbPath` 和 `decryptKey`，对外暴露 `/api/v1/*`。因此 v1 的正确做法是**让 WeFlow 读库，我们读 WeFlow**；把“读库”封装在 `Source` 接口后面，未来可替换。

## 3. 数据源

### 3.1 WeFlow API（v1）

| 用途 | 接口 | 参数 |
|---|---|---|
| 健康检查 | `GET /health` | 无 |
| 会话列表 | `GET /api/v1/sessions` | `limit` |
| 联系人 | `GET /api/v1/contacts` | `limit`, `keyword` |
| 消息分页 | `GET /api/v1/messages` | `talker`, `limit`, `offset`, `media`, `start`, `end` |
| 媒体 | `GET /api/v1/media/<path>` 或消息里的 `mediaUrl` | token |

认证：`access_token`（现有值在 Infisical `platform-accounts/dev /weflow`；本机 WeFlow 配置里也有 `httpApiToken`）。
Mac 现状：`httpApiEnabled=true`、`127.0.0.1:5031`，当前进程没启动；启动 WeFlow 即可同步。
Windows：同样跑 WeFlow，或提供只读 API；**不要**用 `netsh portproxy` 跨机暴露。

### 3.2 账号与命名空间

- `my_wxid` 从 `isSend=1` 的 `senderUsername` 推断（两端都能拿到），或读 WeFlow 配置。
- `account_id = "a" + sha1(my_wxid)[:12]`。
- **已确认同账号**：两端 `account_id` 相同，会话/消息合并去重。
- **不同账号**：`conv_id` 前缀不同，互不合并。不要按显示名合并，显示名会改。

### 3.3 Source 适配器接口

```python
class Source:
    id: str                  # "mac" / "win" / "zip"
    def account(self) -> Account
    def sessions(self) -> list[Session]
    def messages(self, talker, cursor) -> Iterator[Message]
    def media(self, msg) -> BinaryIO   # 可选，失败返回 None
```

v1 实现：`WeFlowSource(dir/API)`、`ExportZipSource(path)`。未来可选 `DirectDBSource`。

## 4. 数据模型（Tablestore）

实例：**新建 `wechat-archive`**（cn-beijing，CU 模式，容量型/HYBRID，0 预留 CU，已确认）。创建实例本身不收费，按存储、读写 CU、外网下行计费。表名统一 `wx_` 前缀。

### 4.1 表

```text
wx_accounts
  PK  account_id (string)
      my_wxid, name, last_sync_at, sources

wx_conversations
  PK  conv_id (string)           # account_id + ":" + talker
      account_id, talker, name, remark, is_group, member_count,
      first_ts, last_ts, updated_at

wx_messages
  PK  conv_id (string), sort_seq (int), msg_uid (string)
      account_id, talker, sender, is_send (bool), kind, local_type (int),
      text, raw, local_seq (int), create_time (int),
      media_sha256, media_key, media_size, media_mime, media_state,
      src, content_hash, ingested_at, flags

wx_contacts
  PK  account_id (string), wxid (string)
      alias, nickname, remark, is_friend, updated_at

wx_sync_state
  PK  source_id (string), conv_id (string)
      last_sort_seq, last_create_time, last_run, status, last_error, msgs_written

wx_media_tasks
  PK  msg_uid (string)
      conv_id, media_ref, kind, state (pending/running/done/failed),
      attempts, last_error, updated_at

wx_conflicts
  PK  msg_uid (string), src (string)
      conv_id, existing_hash, incoming_hash, existing_text, incoming_text, created_at
```

- 所有表 `MaxVersions=1`、`TTL=-1`（永久归档），不开启多版本，避免额外存储。
- `sort_seq = 来源 sortSeq`，没有则 `createTime * 1000`；样例里两者相等。
- `msg_uid`：`"s" + str(serverId)`；无 serverId 时 `"h" + sha1(conv|createTime|isSend|localType|content|sender)[:20]`。
- `local_seq` 只作同一秒内的显示排序，不参与主键。
- `content_hash = sha1(kind|text|sender|create_time)`；不含 `raw`，避免 CDN/媒体 URL 波动造成误报冲突。
- 行大小限制 4MB；`raw` 超过 256KB 截断并置 `flags.truncated`。

### 4.2 `kind` 归一化

| localType | kind |
|---|---|
| 1 | text |
| 3 | image |
| 34 | voice |
| 43 | video |
| 47 | emoji |
| 48 | location |
| 49 及高位复合值 | app（链接/文件/小程序/引用等） |
| 42 | card |
| 10000 | system |
| 其他 | other |

`text`：文本消息用 `content`；app 消息尽量抽 `<title>`/`<des>`/`<url>`；媒体消息保留 `[图片]` 这类占位并写 `raw`。完整 XML 保留在 `raw`（清洗后）。

### 4.3 写入

- 消息用 `PutRow` + `RowExistenceExpectation.EXPECT_NOT_EXIST`（先写先得），4-8 并发；失败指数退避。
- 条件失败说明行已存在：读旧行比对 `content_hash`，不同则写 `wx_conflicts`，相同则跳过。
- 首次大规模导入可用 `--overwrite` 走 `BatchWriteRow`（每批 25 行）提速；默认不开，避免覆盖撤回前的原文。
- 每页/每会话成功后推进 `wx_sync_state`；崩溃重跑靠主键条件保证不出重复行。

## 5. 媒体（OSS）

### 5.1 Bucket

新建私有 bucket `wechat-archive`（cn-beijing）：

- ACL private，开 SSE-OSS（AES256），不开公共读；不开版本控制（key 内容寻址、不可变）。
- 生命周期：30 天后转 IA，180 天后可转 Archive（按需；聊天图片可能常看，先 IA 即可）。
- RAM 只给 `PutObject/GetObject/HeadObject/ListObjects`，不给删除。

### 5.2 Key 设计

```text
media/sha256/<sha256>.<ext>               # 内容寻址，key 里不含 wxid/文件名
exports/<source>/<yyyy-mm-dd>/manifest.jsonl.gz
```

- 下载 WeFlow 媒体到临时文件，流式计算 SHA-256；先 `HEAD` 目标 key，存在则复用；不存在再上传。
- 大文件用 `oss2.resumable_upload`；小文件 `put_object`。
- `Content-Type` 按扩展名；`x-oss-meta-sha256` 冗余校验；原文件名只进消息属性，不进 key。
- 同一张图在多个会话、两台机器上 → 同一个 SHA-256 → 只存一份。

### 5.3 与消息的关联

- 入库时若消息有 `mediaUrl/mediaFileName`，先写 `wx_media_tasks`（`state=pending`），消息行 `media_state=pending`。
- 媒体 worker 处理任务：下载 → hash → OSS → 更新消息行（`media_sha256/media_key/media_size/media_mime/media_state=done`）→ 任务行标 `done`（不删，留审计；无 DeleteRow 权限）。
- 失败保留任务、`attempts+1`，超过 5 次标 `failed`，不阻塞文本归档。
- **不持久化 `mediaUrl`**（含 token）；任务里只存相对 `media_ref`，处理时重新解析。

### 5.4 解密与校验

- 微信 4.x 的图片/视频/语音在本地是加密容器；WeFlow 的 `/api/v1/media` 或 `mediaUrl` 可能已经解密，但**先抽样验证**，不要假设。
- 视频：下载后 `ffprobe` 验证容器/时长；若是加密头或 ffprobe 失败，用消息 `rawContent` 里的 `aeskey`（仅内存）走 WeFlow 的解密路径，解出可播放的 mp4 再上传。
- 图片：验证 JPEG/PNG/WebP/GIF magic；加密 `.dat` 同理处理。
- 语音：silk 原样存一份；需要可播放副本再转 wav（v2 可选）。
- OSS 只放解密后的明文；aeskey、加密文件、临时文件不落库、不残留。原始 `rawContent` 入库前移除 `aeskey`/`cdnthumbaeskey`（见 D8）。

## 6. 流程

### 6.1 首次回填

1. **Infra**：Tablestore 实例/表、OSS bucket、RAM 用户、Infisical 路径（见 8）。
2. **导入现有 zip**：54 个 zip → 85,281 条文本；不依赖 WeFlow 运行。验收数对得上。
3. **Mac 全量文本**：WeFlow 打开，`--media=0` 拉**全部会话**（一对一 + 群 + 431 公众号 + 系统号），共 1452 个；公众号也存，补上 zip 里没有的会话。
4. **Windows 全量文本**：Windows 也跑 WeFlow，同样全量；同账号按 `msg_uid` 去重。
5. **媒体回填**：全量（含视频）；先抽样验证解密，再分批过夜跑。
6. **增量**：launchd / Task Scheduler 每 15 分钟。

### 6.2 增量

- 拉 `/sessions`，只处理 `lastTimestamp` 变了的会话。
- 对每个会话，从 `wx_sync_state.last_create_time` 往前回看 1 天（防漏），按 `start/end` 或 offset 拉，过滤 `sort_seq > last_sort_seq`。
- 写入成功后推进水位；失败不推进。
- 媒体任务独立队列，文本先落库，媒体后补。

### 6.3 校验

```bash
wechat-archive verify --counts        # 每会话行数 / 首末时间
wechat-archive verify --dup-msg-uid   # 同一 msg_uid 多行（跨端 sortSeq 不一致）
wechat-archive verify --media         # pending/failed/missing 统计
wechat-archive sample --conv <id> --n 20
```

- 重复 `msg_uid`：保留最早 `ingested_at`，其余标 `dup_of`（不物理删除，留审计）；或人工合并。
- 媒体：同一 SHA-256 只能有一个 OSS key；抽查 100 条图片/文件可打开。

## 7. 去重规则（重点）

| 对象 | 身份 | 跨端行为 |
|---|---|---|
| 账号 | `my_wxid` → `account_id` | 相同则合并；不同则隔离 |
| 会话 | `account_id + ":" + talker(wxid/@chatroom)` | 同名不同 wxid 不合并；显示名只做展示 |
| 消息 | `msg_uid = serverId`（缺失用指纹） | 同一行；`sort_seq` 相同则覆盖，不同则 verify 报重复 |
| 媒体 | 解码后字节的 SHA-256 | 同一 OSS 对象；消息各自引用 |
| 联系人 | `account_id + wxid` | 合并，保留最新昵称/备注 |

为什么不用 `localId`：`localId` 是每台设备/每次迁移都会重置的本地序号，跨端一定冲突；`serverId` 才是服务端消息号。样例里 `sortSeq` 恒等于 `createTime*1000`，所以两端能算出同样的排序键。

冲突策略：

- 文本：先写先得；同 `msg_uid` 且 `content_hash` 不同 → 写 `wx_conflicts`，不改原行。
- 撤回：后到的“撤回”系统消息正常入库；原消息保留（这是备份，不是镜像）。
- 媒体：同一 `msg_uid` 先到先得；后到但 hash 不同（缩略图/转码）→ 保留两份，消息指向先到的那份，另一份记冲突。

## 8. 权限、密钥、合规

- **RAM 用户** `wechat-archive`：
  - Tablestore：`GetRow/GetRange/PutRow/UpdateRow/BatchWrite/BatchGet`，资源 `acs:ots:*:*:instance/wechat-archive/table/wx_*`。
  - OSS：`PutObject/GetObject/HeadObject/ListObjects`，资源 `acs:oss:*:*:wechat-archive/*`。
  - 不给 `DeleteRow`、不给删 bucket；需要清理时用管理员临时授权。
- **Infisical**：新路径 `tools/dev /wechat-archive`，键 `TABLESTORE_ENDPOINT/INSTANCE/ACCESS_KEY_ID/ACCESS_KEY_SECRET`、`OSS_*`；WeFlow token 复用 `platform-accounts/dev /weflow`，不复制。
- **本机**：WeFlow API 只绑 `127.0.0.1`；ingester 在本机跑。不要 `netsh portproxy` 到 LAN；跨机只走 SSH 隧道。
- **仓库**：数据、导出、SQLite、日志一律 gitignore；测试用合成 fixture，绝不用真实聊天。
- **合规**：解密本地微信库通常违反微信用户协议；只读、自用、不分发。媒体是他人隐私，私有 bucket + 不公开链接；不做自动回复/自动发送。
- **日志**：不打印完整 URL（token 在 query 里）、不打印消息正文；只打印 conv_id 哈希前缀和计数。

## 9. 成本

按 1452 会话、200 万～300 万条消息（含公众号文本）、文本+raw 约 2～3GB、媒体 43GB 估算（公开价，实际以账单为准）：

| 项目 | 用量 | 单价量级 | 月费 |
|---|---|---|---|
| Tablestore 实例创建 | — | 0 元 | 0 元 |
| Tablestore 存储 | 2～3GB | 0.1～0.6 元/GB/月 | 0.3～1.8 元 |
| Tablestore 写 CU（导入一次性） | 200～300 万 CU | 0.004～0.01 元/万 CU | 2～3 元（一次性） |
| Tablestore 读 CU + 外网下行 | 日常少量 | 同上 / ~0.5 元/GB | < 2 元 |
| **Tablestore 小计** | | | **约 3～6 元/月** |
| OSS 标准存储 | 43GB | 0.12 元/GB/月 | ~5 元 |
| OSS 请求/流量 | 少量 | — | < 1 元 |
| **合计** | | | **约 10～12 元/月** |

首月含一次性写入约 **12～15 元**；创建实例和 bucket 本身不收费。

- 媒体涨到 200GB：OSS 约 24 元/月；转 IA 后约 16 元/月。
- 不建多元索引（会按索引规模自动产生预留读 CU）；需要云端全文检索时再评估。
- 不需要云服务器：ingester 跑在 Mac/Windows 本机，只往云上写。

## 10. 仓库与代码结构

新仓库 `makewheels/wechat-archive`（private）：

```text
wechat-archive/
├── DESIGN.md
├── README.md
├── pyproject.toml
├── src/wechat_archive/
│   ├── cli.py            # init/import-zips/sync/media/verify/export-sqlite
│   ├── config.py         # 非敏感配置 + env
│   ├── model.py          # Message/Conversation 归一化、msg_uid、kind、清洗
│   ├── sources/weflow.py # 从 wechat-analyzer 移植
│   ├── sources/zip.py
│   ├── store/tablestore.py
│   ├── store/oss.py
│   ├── sync.py
│   ├── media.py
│   └── verify.py
├── tests/fixtures/       # 合成消息，无真实数据
├── deploy/launchd.plist
└── deploy/windows-task.ps1
```

- 复用 `wechat-analyzer/export/weflow_client.py` 的分页/媒体下载逻辑（复制并注明来源）。
- 不把 `wechat-analyzer` 作为依赖，避免两个项目生命周期绑死。
- CI：只跑单测和 `ruff`，不连云、不读真实数据；云上 smoke test 手动跑。

## 11. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 本设计确认；开私有仓库 | 决策表签字 |
| P1 | 新建 `wechat-archive` 实例、bucket、RAM、Infisical；`init` 建表 | 空表可读写，权限最小化验证 |
| P2 | 导入 54 个 zip；Mac 全量（含公众号） | 85,281 条对得上；重跑不增行 |
| P3 | 媒体管线（含视频解密）；抽样 100；全量分批 | 同 hash 只 1 个对象；ffprobe 可播；失败可重试 |
| P4 | Windows 交接（见 HANDOFF-WINDOWS.md）；跨端去重 | 同 `msg_uid` 只 1 行 |
| P5 | launchd/Windows 定时；SQLite 导出 | 断网/重启后自动续传 |
| P6 | 可选：多元索引 / 给 agent 的查询接口 | 按需 |

## 12. 已确认与待办

已确认（2026-11-22）：

1. Mac / Windows 是**同一个微信号** → 合并去重。
2. Windows 也跑 WeFlow，API 与 Mac 相同。
3. 媒体**全量**（含视频），视频解密后入 OSS；新建 bucket `wechat-archive`。
4. **新建** Tablestore 实例 `wechat-archive`。
5. 公众号 431 个也存，能存的都存。

待办/风险：

- 视频解密路径先抽样 20 条验证；WeFlow 若返回加密容器，按 5.4 处理。
- 费用确认：约 **10～12 元/月**，首月 12～15 元；确认后开 P1。
- Windows 侧交接：见 [HANDOFF-WINDOWS.md](HANDOFF-WINDOWS.md)。
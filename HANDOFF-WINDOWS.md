# Windows 侧交接文档（给 Windows 上的 AI）

> 读者：在 Windows 机器上工作的 coding agent。
> 任务：把 Windows 微信（同一个微信号）的聊天记录和媒体，按本文件定义的**暂存包契约**导出，可选上传到阿里云 OSS；**不要写 Tablestore**，最终入库由 Mac 侧完成。
> 先读同目录的 [DESIGN.md](DESIGN.md)。仓库：`makewheels/wechat-vault`（public，MIT）。

## 0. 一句话

在 Windows 上跑一个导出器：调 WeFlow 的本地 HTTP API，把 **1452 个会话（含 431 个公众号、群、系统号）** 的消息归一化成 JSONL，媒体解密后按 SHA-256 命名存到 `media/`，可选传到 OSS；产出 `manifest.json` 后交给 Mac 侧合并去重、写 Tablestore。

## 1. 边界（先看，避免做错）

你负责：

- 确认 Windows 微信已登录、WeFlow 已启动、HTTP API 可用。
- 写/跑 Windows 导出器，产出第 3 节定义的暂存包。
- 媒体解密、校验、SHA-256、按内容寻址命名。
- 可选：用**只写**凭据把媒体和消息上传到 OSS。
- 产出 `report.md`，把数量、错误、路径报回来。

你不要做：

- **不要**创建/写入 Tablestore 表；不要碰 `wx_*` 表。
- **不要**删除或修改微信数据库、WeFlow 配置（除了打开 HTTP API）。
- **不要**把 WeFlow 的 API 暴露到 `0.0.0.0` 或局域网；不要用 `netsh portproxy`。
- **不要**把聊天记录、媒体、token、manifest 提交进 Git。
- **不要**把加密媒体当成品上传；必须先解密并验证。
- **不要**把 `mediaUrl`（带 token）写进 JSONL 或日志。

Mac 侧负责：Tablestore 实例/表、OSS bucket、最终入库、跨端去重、正式媒体 key、查询。

## 2. 先决条件与 10 分钟检查

Windows 机器需要：

- Windows 10/11，微信 4.x 已登录（**与 Mac 同一个微信号**）。
- WeFlow 已安装并完成数据库解密；UI 里能正常看聊天。
- WeFlow 设置里开启 HTTP API：`127.0.0.1:5031`，记下 `access_token`。
- Python 3.11+（导出器用标准库即可）；建议装 `ffmpeg/ffprobe`（视频校验）。
- 至少 60GB 空闲磁盘（43GB 媒体 + 临时文件）。
- 可选上传：能访问 `oss-cn-beijing.aliyuncs.com`，拿到只写 RAM 凭据。

逐项检查（PowerShell）：

```powershell
# 1) WeFlow 是否在跑
Invoke-RestMethod http://127.0.0.1:5031/health

# 2) token 是否有效
$env:WEFLOW_ACCESS_TOKEN = "<向 Mac 侧要，或从 WeFlow 设置里复制>"
Invoke-RestMethod "http://127.0.0.1:5031/api/v1/sessions?limit=5&access_token=$env:WEFLOW_ACCESS_TOKEN"

# 3) 消息接口
Invoke-RestMethod "http://127.0.0.1:5031/api/v1/messages?talker=<一个 wxid>&limit=2&offset=0&media=1&access_token=$env:WEFLOW_ACCESS_TOKEN"

# 4) 工具
python --version
ffprobe -version
```

预期：

- `/health` 返回 200/JSON。
- `/sessions` 返回 `sessions` 数组；元素有 `username/displayName/lastTimestamp/sessionType`。
- `/messages` 返回 `messages` 数组和 `hasMore`；消息有 `serverId/localId/createTime/sortSeq/isSend/senderUsername/localType/content/rawContent`，媒体消息可能有 `mediaUrl/mediaFileName`。

常见失败：

- 连接被拒：WeFlow 没启动 / HTTP API 没开 / 端口不是 5031。
- 401/403：token 错；token 放在 query 里，别写进脚本常量。
- 消息为空：数据库没解密/没加载；先在 WeFlow UI 打开该会话，再重试（客户端本身有“首次偶尔空”的现象，重试 3 次）。
- 媒体 404：本地文件已被清理；记 `media_missing`，不要中断整个导出。

## 3. 暂存包契约（必须严格按这个产出）

本地目录：

```text
wechat-export-win/
├── manifest.json
├── sessions.jsonl
├── contacts.jsonl
├── messages/
│   └── <conv_key>.jsonl
├── media/
│   └── <sha256>.<ext>
├── state.json
└── errors.jsonl
```

规则：

- UTF-8，无 BOM；JSONL 一行一个对象，`ensure_ascii=False`。
- 消息按 `(sort_seq, msg_uid)` 升序。
- `conv_key = sha1(talker.encode("utf-8")).hexdigest()[:16]`，避免 Windows 路径/中文/长文件名问题。
- 所有写入先写 `.tmp` 再原子 rename；重跑覆盖同名文件不产生重复。
- `server_id` 用**字符串**（19 位会超过 JS 安全整数；Python 没问题，但统一字符串最稳）。

### 3.1 `manifest.json`

```json
{
  "schema_version": 1,
  "source": "win",
  "run_id": "20261122T2300-win",
  "account_id": "a0123456789ab",
  "my_wxid_hash": "sha256:<hex>",
  "weflow_version": "4.5.1",
  "exported_at": "2026-11-22T23:00:00+08:00",
  "full": true,
  "counts": {
    "sessions": 1452,
    "messages": 1234567,
    "media_files": 12345,
    "media_bytes": 43000000000,
    "errors": 0
  },
  "media_prefix": "media/sha256/",
  "staging_prefix": "staging/win/20261122T2300-win/",
  "notes": "optional"
}
```

- `account_id = "a" + sha1(my_wxid)[:12]`；`my_wxid` 从 `isSend=1` 的消息 `senderUsername` 推断。
- **不要**在 manifest 里写明文 `my_wxid`；只写 `account_id` 和 hash。
- `run_id` 用 UTC+8 时间；每次全量/增量一个新 run，便于回滚。

### 3.2 `sessions.jsonl`

```json
{"talker":"wxid_xxx","name":"显示名","is_group":false,"session_type":"single","last_ts":1720177182,"msg_count":148}
```

- `talker` 是原始 wxid 或 `xxx@chatroom`；这是私有暂存包，允许出现，但不能进 Git。
- `is_group`：`talker.endswith("@chatroom")` 或 API `sessionType=="group"`。
- `session_type`：`single/group/official/system/other`；公众号是 `gh_` 开头。
- `msg_count` 是本次导出条数（不是微信里的总数）。

### 3.3 `messages/<conv_key>.jsonl`

```json
{
  "schema_version": 1,
  "conv_id": "a0123456789ab:wxid_xxx",
  "talker": "wxid_xxx",
  "msg_uid": "s5352695609301753000",
  "server_id": "5352695609301753000",
  "local_id": 1,
  "local_seq": 1,
  "sort_seq": 1720177182000,
  "create_time": 1720177182,
  "is_send": 1,
  "sender": "wxid_xxx",
  "local_type": 1,
  "kind": "text",
  "text": "我以前也是",
  "raw": "我以前也是",
  "media_ref": null,
  "media_sha256": null,
  "media_ext": null,
  "media_size": null,
  "media_mime": null,
  "flags": []
}
```

字段规则：

- `msg_uid`：
  - 有 `serverId`（非 0）→ `"s" + str(serverId)`。
  - 没有 → `"h" + sha1(f"{talker}|{create_time}|{is_send}|{local_type}|{content}|{sender}")[:20]`。
- `sort_seq`：优先 `sortSeq`；没有则 `createTime * 1000`。样例里两者相等。
- `local_seq`：`localId`；只用于同一秒内显示排序，不参与去重。
- `kind` 映射：

  | localType | kind |
  |---|---|
  | 1 | text |
  | 3 | image |
  | 34 | voice |
  | 43 | video |
  | 47 | emoji |
  | 48 | location |
  | 42 | card |
  | 49 及高位复合值 | app |
  | 10000 | system |
  | 其他 | other |

- `text`：
  - text：`content`。
  - app：从 `rawContent` 抽 `<title>`；没有则 `[链接]` / `[文件]`。
  - 媒体：`content`（通常是 `[图片]`/`[视频]`）。
  - system：保留可读文本。
- `raw`：
  - 优先 `rawContent`，否则 `content`。
  - **清洗**：删掉 `aeskey="..."`、`cdnthumbaeskey="..."`、任何 `access_token=...`/`token=...`；其余 XML 保留。
  - 超过 256KB 截断，`flags` 加 `truncated_raw`。
- `media_ref`：WeFlow 返回的相对路径/文件名；**不要**存完整 `mediaUrl`（含 token）。拿不到就 null。
- `media_*`：媒体下载成功后填；未下载/失败为 null。
- `flags`：`media_missing` / `media_encrypted` / `media_unverified` / `truncated_raw` 等。

### 3.4 `contacts.jsonl`

```json
{"wxid":"wxid_xxx","alias":"","nickname":"","remark":"","is_friend":true}
```

能拿到就写，拿不到写空文件；不是阻塞项。

### 3.5 `media/<sha256>.<ext>`

- 内容是**解密后**的原始字节（图片/视频/语音/文件），不是微信的 `.dat` 加密容器。
- `sha256` 是这些小写十六进制；`ext` 用实际类型（jpg/png/gif/webp/mp4/silk/pdf/...）。
- 同一字节只存一份；重跑命中同名文件直接复用。
- 媒体大小上限先设 4GB；超过记 `media_too_large`，不阻塞。
- 临时文件用 `media.tmp`，完成后删。

### 3.6 `state.json`

```json
{
  "version": 1,
  "conversations": {
    "wxid_xxx": {"done": true, "last_sort_seq": 1720177182000, "messages": 148, "media": 12, "errors": 0, "updated_at": "2026-11-22T23:00:00+08:00"}
  }
}
```

- 每个会话导出成功才 `done=true`；失败保留 `done=false`，下次重试。
- `--full` 忽略 state；`--incremental` 从 `last_sort_seq` 往前回看 1 天。

### 3.7 `errors.jsonl`

```json
{"ts":"2026-11-22T23:00:00+08:00","talker":"wxid_xxx","stage":"messages","offset":1200,"error":"timeout","retryable":true}
```

不要写消息正文；错误信息里如果含 URL，先脱敏 token。

## 4. 导出算法

```text
sessions = GET /api/v1/sessions?limit=5000
for s in sessions:
    if state[s.talker].done and not --full: continue
    offset = 0
    seen = set()
    while True:
        page = GET /api/v1/messages?talker=<urlencode>&limit=200&offset=offset&media=1
        if page empty and retry<3: retry
        for m in page.messages:
            msg = normalize(m)
            if msg.msg_uid in seen: continue
            seen.add(msg.msg_uid)
            if m has media:
                media = download_and_decrypt(m)
                if media: msg.media_sha256 = sha256(media); write media/<sha>.<ext>; fill size/mime/ext
                else: msg.flags.append("media_missing" or "media_encrypted")
            append msg to messages/<conv_key>.jsonl
        offset += len(page.messages)
        if not page.hasMore: break
    state[s.talker] = {done: true, ...}
```

细节：

- 每页 200；某页失败降到 50，再失败记录错误并停止该会话（不标 done）。
- API timeout 30s；媒体 120s；重试 3 次，指数退避 1/2/4s。
- 并发 2～4 个会话；同一会话内串行，避免 WeFlow 压力。
- `--conv <talker>` 只导一个会话；`--limit-convs N` 只导前 N 个（冒烟用）。
- `--no-media` 先导文本；`--media-only` 只补媒体（扫描已有 JSONL 里 `media_sha256` 为空的消息，重新下载）。
- 增量：先拉最近 2～3 页，遇到 `sort_seq <= last_sort_seq` 停止；再按 `msg_uid` 去重。
- 断点：每导完一个会话写一次 `state.json`（原子写）。
- 不要在内存里攒全量；逐会话 streaming 写。

## 5. 媒体解密与校验（重点）

微信 4.x 的图片/视频/语音在本地可能是加密容器；WeFlow 的 `mediaUrl`/`/api/v1/media` **可能**已经解密，但不要假设。

流程：

1. 下载到临时文件（流式，别一次读进内存）。
2. 读前 16 字节判断 magic：
   - JPEG `FF D8 FF`；PNG `89 50 4E 47`；GIF `47 49 46`；WebP `RIFF....WEBP`；MP4 第 4–8 字节 `ftyp`；PDF `%PDF`；ZIP `PK\x03\x04`。
   - 语音 silk 以 `#!SILK_V3` 开头。
3. 如果 magic 不像明文：
   - 优先走 WeFlow 的解密/导出路径（它已经持有 key）；或从 `rawContent` 取 `aeskey`，在内存里解密（**不要落库、不要写日志**）。
   - 仍失败 → 记 `media_encrypted`，跳过，不上传加密文件。
4. 视频额外用 `ffprobe`：
   ```powershell
   ffprobe -v error -show_entries format=duration,format_name -of json <file>
   ```
   失败 → 视为未解密/损坏；记 `media_unverified`，不要上传。没有 ffprobe 时至少校验 magic + 文件大小，并记 `media_unverified`。
5. 计算解密后字节的 SHA-256；写 `media/<sha>.<ext>`；消息里填 `media_sha256/media_size/media_ext/media_mime`。
6. 删除临时文件；不要把 `aeskey` 写进 JSONL/manifest/日志。

语音：silk 原样存 `.silk`；如果后续要可播放，再转 wav（v2，不阻塞本次）。

## 6. 可选：上传 OSS

Mac 侧会提供：

- bucket：`wechat-vault`，region `cn-beijing`，endpoint `oss-cn-beijing.aliyuncs.com`。
- 只写 RAM 凭据：只能 `PutObject` 到 `media/sha256/*` 和 `staging/win/*`；不能读、不能删、不能列出整个 bucket。
- 期望的 `run_id`。

上传顺序：

1. 先传媒体到最终内容寻址前缀：`oss://wechat-vault/media/sha256/<sha>.<ext>`。
   - 内容寻址，重复上传同一个 key 无副作用；已存在可跳过（HEAD/stat）。
2. 再传 `messages/`、`sessions.jsonl`、`contacts.jsonl`、`errors.jsonl`。
3. **最后**传 `manifest.json` 和 `state.json` 到 `oss://wechat-vault/staging/win/<run_id>/`；manifest 是“提交标记”，Mac 侧看到它才开始导入。

用 `ossutil`（Windows 版）示例：

```powershell
ossutil cp -r .\media\ oss://wechat-vault/media/sha256/ --update
ossutil cp -r .\messages oss://wechat-vault/staging/win/<run_id>/messages/
ossutil cp .\sessions.jsonl oss://wechat-vault/staging/win/<run_id>/
ossutil cp .\manifest.json oss://wechat-vault/staging/win/<run_id>/
```

（`--update` 跳过已存在的同名对象；媒体 key 不可变，安全。）

不能上传时：

- 写到外接盘/共享盘，目录结构保持不变；
- 在 `report.md` 里写清楚路径和总大小；
- 不要用微信/网盘压缩包二次打包，保持原始文件，Mac 侧直接读。

上传耗时参考：43GB 在 10Mbps 上行约 10 小时，100Mbps 约 1 小时；可过夜跑，`ossutil` 支持断点续传。

## 7. 安全与隐私

- `WEFLOW_ACCESS_TOKEN` 只放环境变量；不要写进脚本、命令行参数、日志。
- WeFlow HTTP API 只绑 `127.0.0.1`；不要改 `0.0.0.0`，不要 `netsh portproxy`。
- 暂存目录用 BitLocker/受控目录；完成后删临时文件；本地包是否保留由用户决定。
- 不上传加密文件；不把 `aeskey`/token/完整 `mediaUrl` 写进任何产物。
- 不把 `wechat-export-win/` 提交进 Git；`.gitignore` 已覆盖 `media/`、`*.jsonl`、`manifest.json` 等。
- 只读微信数据库；不删除、不修改、不“清理”聊天记录。

## 8. 验收（按顺序做）

1. `/health` 200；`/sessions` 能返回，数量接近 1452。
2. `account_id` 与 Mac 侧给的值一致（同账号）。
3. 冒烟：选 3 个会话（一对一 / 群 / 公众号），各导 50 条：
   - 消息数与 WeFlow UI 对齐；
   - 第一条/最后一条时间对齐；
   - `msg_uid` 无重复；
   - 至少 20 个媒体文件能打开；5 个视频 `ffprobe` 通过。
4. 重跑同一会话：消息数不变、媒体数不变、无重复 `msg_uid`。
5. 全量：记录 `sessions/messages/media_files/media_bytes/errors`；错误率 < 1%。
6. 若上传 OSS：`manifest.json` 在最后出现；Mac 侧能读到 `staging/win/<run_id>/manifest.json`。

## 9. 回报格式（写到 `report.md`，同时贴给用户）

```text
source: win
run_id: 20261122T2300-win
account_id: a0123456789ab
weflow_version: 4.5.1
sessions: 1452 (single 789 / group 211 / official 431 / system 21)
messages: 1234567
media: 12345 files / 43.0 GB
media_verified: 12000 / unverified 300 / missing 45 / encrypted 0
errors: 12
staging: oss://wechat-vault/staging/win/20261122T2300-win/
duration: 3h20m
notes: ...
```

## 10. 与 Mac 侧的接口

Mac 侧会提供：

- `account_id` 期望值；
- bucket/region/endpoint；
- 只写 RAM 凭据的交付方式（不要发到公开聊天/仓库）；
- `run_id` 约定；
- 导入器就绪后，按 `schema_version=1` 读取本暂存包。

如果契约有歧义：

- **先在 `report.md` 里列出来，不要自己改字段名**；
- Mac 侧会更新 `DESIGN.md`/本文件并同步版本号；
- 字段只能加，不能悄悄删/改名。

## 11. 常见问题

| 现象 | 处理 |
|---|---|
| `Connection refused` | WeFlow 没开 / API 端口不是 5031 / 防火墙拦本机 |
| 401/403 | token 错或没带；从 WeFlow 设置重新复制 |
| 消息为空但 UI 有 | 先打开会话，重试 3 次；仍空则记录并跳过 |
| 媒体 404 | 本地缓存被清理；记 `media_missing`，继续 |
| 视频 ffprobe 失败 | 可能未解密/损坏；记 `media_unverified`，不上传 |
| 中文路径/长路径报错 | 用 `conv_key` 做文件名；开启 Windows 长路径 |
| 磁盘不够 | 先 `--no-media` 导文本；媒体分批/外接盘 |
| 上传 403 | 凭据不是只写前缀，或 bucket/region 错 |
| 导出很慢 | 降低并发到 1～2；WeFlow 在大会话上本身慢 |
| 电脑睡眠中断 | 电源计划设为“不睡眠”，或分会话续跑 |

## 12. 最小实现建议

- 语言：Python 3.11+，只用标准库（`urllib/json/hashlib/pathlib`）即可；不要为了 HTTP 装重依赖。
- 结构：
  ```text
  windows/
  ├── export_weflow.py     # 单文件 CLI
  ├── report.md            # 本次结果
  └── README.md            # 怎么跑
  ```
- CLI：
  ```text
  python export_weflow.py --out D:\wechat-export-win --full
  python export_weflow.py --out ... --conv <talker> --limit 50
  python export_weflow.py --out ... --incremental
  python export_weflow.py --out ... --no-media
  ```
- 先跑冒烟，再跑全量；全量前把电源/睡眠/磁盘检查一遍。

## 13. 最后

- 本文件是契约，不是建议；字段/目录/命名按第 3 节来。
- 有任何不确定，**先停下来记录问题**，不要猜字段或偷偷改结构。
- 完成后把 `report.md`、`manifest.json`（不含敏感内容）和暂存路径/OSS 前缀发给用户和 Mac 侧。
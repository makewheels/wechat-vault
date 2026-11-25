# Windows 导出器

## 密钥隔离

Infisical 项目 `tools`、环境 `dev`：按 `/wechat-vault/<微信ID>/<机器标识>` 存放密钥。
当前账号 `wxid_vizj1s0qmxl922` 的 Windows 密钥位于
`/wechat-vault/wxid_vizj1s0qmxl922/windows`，键名 `WECHAT_DB_KEY`。
`windows` 暂为这台 Windows 的逻辑标识，尚未核实主机名。
Mac 使用独立路径 `/wechat-vault/wxid_vizj1s0qmxl922/mac-mint-mac-mini`；禁止跨机器混用。
云凭据仍在 `/wechat-vault`；密钥不再放在该公共路径。

Windows 运行时先注入云凭据，再注入该机器密钥：

```powershell
infisical run --projectId=79c61872-d992-44c7-a5ae-d3e70e004e7f --env=dev --path=/wechat-vault -- infisical run --projectId=79c61872-d992-44c7-a5ae-d3e70e004e7f --env=dev --path=/wechat-vault/wxid_vizj1s0qmxl922/windows -- python windows/export_snapshot.py --out W:\wechat-export-win --check
```

## 推荐：直读数据库（export_snapshot.py，不依赖 WeFlow）

WeFlow 内置到期（`wcdb_init -1005`，无新版），改为直接读微信 4.x 加密库：`wcdb_snapshot.py` 在内存中解密（SQLCipher 4，合并 WAL，逐页 HMAC 校验），磁盘上不落明文。`export_snapshot.py` 复用下面 `export_weflow.py` 的整条管线，只把数据源换成数据库，产出同样的暂存包。

```powershell
python windows\export_snapshot.py --out W:\wechat-export-win --check        # 只读：解密、校验表结构、打印计数
python windows\export_snapshot.py --out W:\wechat-export-win --full          # 全量，含媒体；--no-media 只导文本
python windows\export_snapshot.py --out W:\wechat-export-win --verify
wechat-vault import-staging --dir W:\wechat-export-win --workers 16          # 入 Tablestore + OSS，幂等
```

- 口令：环境变量 `WECHAT_DB_KEY`（64 位 hex），或 WeFlow 配置里的 `decryptKey`。口令不会被打印。
- 表结构不符（微信升级）会直接报错退出，不会产出错误数据；仅在 4.2.0.53 上验证过。
- 微信账号推断：目录名去掉 `_xxxx` 后缀，并用 `Name2Id` 校验；不对时设 `WECHAT_MY_WXID`。
- 分库的同一会话（message_0/1/2 与 biz_message_0）会合并，排序为 `(create_time, sort_seq, local_id)`。
- 跨设备去重靠 `(conv_id, sort_seq, msg_uid)`；Mac 与 Windows 的 `sort_seq` 不一致时会产生重复，入库后用 `wechat-vault verify --dup-msg-uid` 检查。
- `--source`（默认 `win`）写进 manifest，决定同步状态的 `source_id`；在 Mac 上跑设 `mac-db`。Mac 步骤见上级 `HANDOFF-MAC.md`。

媒体（`snapshot_media.py` + `wechat_media.py`，只读、不改微信目录）：

- 图片：`msg\attach\<md5(会话)>\<月>\Img\<md5>[_h|_W|_t|_t_W].dat`，md5 取自消息行的 `packed_info_data`；优先高清（`_h`），只有缩略图时打 `media_thumbnail_only`。V2 格式用 AES 密钥 + XOR 密钥解（`WECHAT_IMAGE_AES_KEY`/`WECHAT_IMAGE_XOR_KEY` 或 WeFlow 配置的 `imageAesKey`/`imageXorKey`），旧格式单字节 XOR；wxgf（HEVC）用 `ffmpeg` 转成 JPEG（有损重编码，需要 `ffmpeg` 在 PATH）。
- 视频：`msg\video\<月>\<md5>.mp4`；文件：`msg\file\<月>\<标题>`，有 XML md5 时校验内容；语音：`message\media_*.db` 的 `VoiceInfo`（SILK）。
- 解出的图片/语音先放 `<out>\.media-work`，入包后即删；本地没有的（未下载）标 `media_missing`，不是错误。
- 表情（emoji）只有 CDN 链接、没有本地文件，不导出。
- 入库时已存在的行只补媒体字段（`media_*`、`flags`）；OSS 并发上传限 8、失败自动退避重试（出口代理不稳）。

测试：`cd windows; python -m unittest`。

---

# 旧路径：WeFlow HTTP API（export_weflow.py）

从本机 WeFlow 的 HTTP API 导出暂存包，格式见上级 `HANDOFF-WINDOWS.md`；Mac 侧用 `wechat-vault import-staging` 入库。只用 Python 标准库，无需安装依赖。

## 使用

1. 打开 WeFlow → 设置 → API 服务 → 打开「HTTP API 服务」（2.1.4 重启后需重新打开，只监听 `127.0.0.1:5031`，无 token）。
2. 冒烟（3 个会话各最新 50 条，部分包）：

```powershell
python windows\export_weflow.py --out W:\wechat-export-win-smoke --conv <单聊wxid> --conv <群@chatroom> --conv <gh_公众号> --limit 50
python windows\export_weflow.py --out W:\wechat-export-win-smoke --verify
```

3. 全量：`python windows\export_weflow.py --out W:\wechat-export-win --full`；之后 `--incremental`；中断后直接重跑（默认续传，跳过已完成会话）。

常用参数：`--no-media` / `--media-only`、`--workers 1..4`、`--page-size`、`--limit-convs N`、`--account-id`（必须和 Mac 一致）、`--ffprobe none`。账号推断不到时设环境变量 `WECHAT_MY_WXID`。退出码：0 正常，3 有错误（见 `errors.jsonl`），1 致命，4 校验有问题，130 被中断。

## 与契约的差异（WeFlow 2.1.4）

- API 无 token、无 `/api/v1/media`、无 `mediaUrl`：媒体只从 WeFlow 的 `media.exportPath` 下复制本地文件；没有本地路径的标 `media_missing`。若换成 4.5.1 风格 API，需要补 `mediaUrl` 下载。
- 消息接口升序；增量按 `create_time` 回看 1 天后重取并合并。
- 语音是 `.wav`；文件附件（type 49/6）WeFlow 不导出。
- 字符串里的孤立 UTF-16 代理项会换成 U+FFFD，并加 `bad_utf16` flag（Mac 侧入库器无法处理孤立代理项）。
- `--limit` 产出的是部分包（`manifest.full=false`，会话 `done=false`）。清空过的会话可能每次都出现 `empty_page`。
- WeFlow 自己的媒体缓存会保留，全量时磁盘占用约翻倍。

## 测试

```powershell
cd windows; python -m unittest -v
```

14 个用例（进程内模拟 WeFlow 2.1.4）；已用 Mac 侧 `StagingPackage` 读取验证过包格式。未覆盖：真实 WeFlow、真实视频的 ffprobe、Mac 的 `account_id` 比对。

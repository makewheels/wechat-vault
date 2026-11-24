# Mac 端交接：把 Mac 微信聊天记录（文字 + 媒体）并入 wechat-vault

目标：Mac 上的微信数据库（同一个账号 `wxid_vizj1s0qmxl922`）导出后写入同一个 Tablestore / OSS。
消息按 `msg_uid`（`s<server_id>`）+ `sort_seq` 去重，Windows 已经传过的行不会重复，只会补缺的行。

状态（截至交接时）：

- Windows 端已完成：342,016 条消息 + 30,480 个媒体文件已导出，文字已在 Tablestore，媒体导入 OSS 进行中/已完成（见 `windows/README.md`）。
- Mac 端**未做**，卡在一步：Mac 微信数据库密钥存在 WeFlow 的钥匙串项里，**Mac 屏幕锁屏时 SSH 读不到**（`security` 报 `User interaction is not allowed`，`CGSSessionScreenIsLocked=true`）。
- Windows 的密钥对 Mac 的库无效（HMAC 全部失败），Mac 有自己的密钥。

## 0. 前提

- Mac：`Mint-Mac-mini`，用户 `mint`，已解锁并登录 GUI；微信保持登录。
- 仓库在 `~/workspace/tools/wechat-vault`，分支 `windows-exporter`（或合并后的 main），先 `git pull`。
- `uv`（`~/.local/bin`）、`infisical`（`/opt/homebrew/bin`）已装；需要 `brew install ffmpeg`（部分图片是 wxgf/HEVC，要转成 JPEG）。

## 1. 取出 Mac 密钥（必须在已解锁的 GUI 会话里做）

在 Mac 的“终端”里运行（不要用 SSH）：

```bash
cd ~/workspace/tools/wechat-vault && python3 mac/wxkey_mac.py; cat /tmp/wxkey.status
```

macOS 弹出钥匙串授权时点“允许”（会要登录密码）。输出 `ok WECHAT_DB_KEY_MAC,...` 即成功。
脚本把 `WECHAT_DB_KEY_MAC`、`WECHAT_IMAGE_AES_KEY_MAC`、`WECHAT_IMAGE_XOR_KEY_MAC` 写进 Infisical
`tools-secrets / dev / /wechat-vault`，不打印。

解锁后也可以让远程 agent 代跑：先 `ssh mac` 写一个 `.command` 包装（`cd` 到仓库后 `python3 mac/wxkey_mac.py`），
再 `open -a Terminal` 它，并在屏幕上点“允许”。

## 2. 载入环境变量

```bash
cd ~/workspace/tools/wechat-vault
eval "$(infisical export --projectId 79c61872-d992-44c7-a5ae-d3e70e004e7f --env dev --path /wechat-vault --format dotenv-export)"
export WECHAT_DB_KEY="$WECHAT_DB_KEY_MAC" WECHAT_MY_WXID=wxid_vizj1s0qmxl922
export WECHAT_IMAGE_AES_KEY="$WECHAT_IMAGE_AES_KEY_MAC" WECHAT_IMAGE_XOR_KEY="$WECHAT_IMAGE_XOR_KEY_MAC"
unset WECHAT_DB_KEY_WIN
DB="$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_vizj1s0qmxl922_2b0d/db_storage"
```

如果 Mac 的图片密钥没取到，先试 Windows 同一账号的图片密钥（`.dat` 的 V2 密钥可能与账号相关）；
`export_snapshot.py` 解不出的图片会计入 `image_undecodable`，不会写错数据。

## 3. 先体检再导出（只读）

```bash
uv run --no-project --with cryptography python windows/export_snapshot.py --db-storage "$DB" --check
```

`bad_hmac=0` 且各表列检查通过才继续。若报 HMAC 失败：Mac 版 SQLCipher 参数/页大小可能与 Windows 不同，
需要对照 `windows/wcdb_snapshot.py` 的常量（`PAGE/RESERVE/KDF_ITER`）调整，不要强行导出。

```bash
uv run --no-project --with cryptography python windows/export_snapshot.py \
  --db-storage "$DB" --out ~/wechat-export-mac --full --source mac-db --workers 4 --ffprobe none
uv run --no-project --with cryptography python windows/export_snapshot.py --out ~/wechat-export-mac --verify --ffprobe none
```

`--source mac-db` 让同步状态与 Windows 分开，互不覆盖。`--verify` 里 “key or token text left in raw”
若只是聊天正文里字面写着 `token=`，属于已知误报（Windows 那份有 2 个）。

Mac 媒体目录布局与 Windows 相同（`msg/attach|video|file`，已确认），媒体约 16 GB，磁盘需留足。

## 4. 导入（先试跑）

```bash
uv run wechat-vault import-staging --dir ~/wechat-export-mac --dry-run
uv run wechat-vault import-staging --dir ~/wechat-export-mac --workers 16
```

已存在的行不重写，只补媒体字段；OSS 按内容哈希去重（`media/sha256/<sha>.<ext>`），Windows 已传的同内容图片不会重复上传。

## 5. 校验

```bash
uv run wechat-vault stats
uv run wechat-vault verify --counts --dup-msg-uid
```

`--dup-msg-uid` 是全表扫描，经 Windows 的代理会超时，所以在 Mac 上直连跑。
若发现同一 `msg_uid` 在不同 `sort_seq` 下出现两次（Mac 与 Windows 的 `sort_seq` 不一致），先不要删，
记下样本，调整去重键或做合并后再清理。

## 6. 收尾

- 删除 `~/wechat-export-mac`（含明文聊天内容）、`/tmp/wxkey*`、`/tmp/mac-screen.png`、`/tmp/wx-check`。
- 不要提交任何密钥；Infisical 里的 `WECHAT_*_MAC` 用完可保留。

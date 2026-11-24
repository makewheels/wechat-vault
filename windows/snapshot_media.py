"""Locate (and where needed decode) the local media behind WeChat 4.x messages. Read-only.

  image  msg/attach/<md5(talker)>/<YYYY-MM>/Img/<md5>[_h|_W|_t|_t_W].dat   (md5 comes from packed_info_data)
  video  msg/video/<YYYY-MM>/<md5>.mp4
  file   msg/file/<YYYY-MM>/<title>                                          (checked against the XML md5)
  voice  message/media_N.db VoiceInfo.voice_data (SILK)

Images and voice are written to a private temp folder so the exporter can hash and copy them like any
other file; the caller removes them after ingesting. Nothing outside the temp folder is modified.
"""
from __future__ import annotations

import collections
import hashlib
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import wcdb_snapshot as ws
import wechat_media as wm

HEX32 = re.compile(rb"[0-9a-f]{32}")
IMAGE_NAME_RE = re.compile(r"^([0-9a-f]{32})(.*)\.dat$")
IMAGE_RANK = {"_h": 0, "_h_W": 1, "": 2, "_W": 3, "_t": 4, "_t_W": 5}
THUMBNAIL_SUFFIXES = {"_t", "_t_W"}
SILK_HEADER = b"#!SILK_V3"


@dataclass
class Located:
    path: Path
    thumb_only: bool = False
    fallback_ext: str | None = None
    name: str | None = None


def xml_value(xml, tag):
    match = re.search(rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", xml, re.S)
    return match.group(1).strip() if match else None


def md5_of(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MediaLocator:
    def __init__(self, account_dir, db_storage, passphrase, workdir, image_aes=None, image_xor=None, lock=None):
        self.account = Path(account_dir)
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.image_aes = image_aes
        self.image_xor = image_xor
        self.lock = lock or threading.RLock()
        self.ffmpeg = shutil.which("ffmpeg")
        self.counts = collections.Counter()
        self.md5_cache = {}
        self.index_images()
        self.index_videos()
        self.index_files()
        self.index_hardlinks(Path(db_storage), passphrase)
        self.index_voice(Path(db_storage), passphrase)

    # -- indexes ---------------------------------------------------------------

    def index_images(self):
        self.images = collections.defaultdict(dict)  # (talker_md5, file_md5) -> {suffix: path}
        for path in (self.account / "msg" / "attach").glob("*/*/Img/*.dat"):
            match = IMAGE_NAME_RE.match(path.name)
            if match:
                talker_md5 = path.parents[2].name
                self.images[(talker_md5, match.group(1))].setdefault(match.group(2), path)

    def index_videos(self):
        self.videos = {}
        for path in (self.account / "msg" / "video").glob("*/*.mp4"):
            self.videos.setdefault(path.stem, path)

    def index_files(self):
        self.files = collections.defaultdict(list)  # file name -> [path]
        for path in (self.account / "msg" / "file").glob("*/*"):
            if path.is_file():
                self.files[path.name].append(path)

    def index_hardlinks(self, db_storage, passphrase):
        self.hardlinks = {"image": {}, "video": {}, "file": {}}
        path = db_storage / "hardlink" / "hardlink.db"
        if not path.exists():
            return
        con, _ = ws.open_memory(path, passphrase)
        for kind in self.hardlinks:
            for md5, name in con.execute(f"select md5, file_name from {kind}_hardlink_info_v4"):
                if md5 and name:
                    self.hardlinks[kind].setdefault(md5, []).append(name)
        con.close()

    def index_voice(self, db_storage, passphrase):
        self.voice_by_svr = {}
        self.voice_by_local = {}
        self.voice_dbs = []
        for path in sorted((db_storage / "message").glob("media_[0-9]*.db")):
            con, _ = ws.open_memory(path, passphrase)
            names = {rowid: name for rowid, name in con.execute("select rowid, user_name from Name2Id")}
            self.voice_dbs.append(con)
            for rowid, chat_id, local_id, svr_id in con.execute(
                    "select rowid, chat_name_id, local_id, svr_id from VoiceInfo"):
                chat = names.get(chat_id)
                if chat is None:
                    continue
                if svr_id:
                    self.voice_by_svr[(chat, svr_id)] = (con, rowid)
                self.voice_by_local[(chat, local_id)] = (con, rowid)

    # -- resolution ----------------------------------------------------------------

    def resolve(self, talker, m):
        base = m["localType"] & 0xFFFFFFFF
        sub = m["localType"] >> 32
        if base == 3:
            return self.image(talker, m)
        if base == 43:
            return self.video(m)
        if base == 34:
            return self.voice(talker, m)
        if base == 49 and sub == 6:
            return self.attachment(m)
        return None

    def candidates(self, m, *xml_attrs):
        found = [hit.decode() for hit in HEX32.findall(m.get("packedInfo") or b"")]
        for attr in xml_attrs:
            for hit in re.findall(rf'\b{attr}="([0-9a-f]{{32}})"', m["rawContent"]):
                found.append(hit)
        return list(dict.fromkeys(found))

    def image(self, talker, m):
        talker_md5 = hashlib.md5(talker.encode("utf-8")).hexdigest()
        best = None
        for candidate in self.candidates(m, "md5"):
            variants = self.images.get((talker_md5, candidate))
            if variants:
                suffix = min(variants, key=lambda s: IMAGE_RANK.get(s, 9))
                best = (suffix, variants[suffix])
                break
        if best is None:
            self.counts["image_not_on_disk"] += 1
            return None
        suffix, path = best
        plain = wm.decode_dat(path.read_bytes(), self.image_aes, self.image_xor)
        if plain is not None and plain[:4] == b"wxgf":
            plain = wm.wxgf_to_jpeg(plain, self.ffmpeg)
            self.counts["image_wxgf_converted" if plain else "image_wxgf_failed"] += 1
        if plain is None:
            self.counts["image_undecodable"] += 1
            return None
        out = self.workdir / f"i{talker_md5[:8]}_{m['localId']}.img"
        out.write_bytes(plain)
        self.counts["image_thumbnail_only" if suffix in THUMBNAIL_SUFFIXES else "image_ok"] += 1
        return Located(out, thumb_only=suffix in THUMBNAIL_SUFFIXES, name=self.rel(path))

    def video(self, m):
        names = []
        for candidate in self.candidates(m, "md5", "newmd5", "rawmd5"):
            names.append(candidate)
            for name in self.hardlinks["video"].get(candidate, ()):
                names.append(Path(name).stem)
        for name in names:
            path = self.videos.get(name)
            if path:
                self.counts["video_ok"] += 1
                return Located(path, name=self.rel(path))
        self.counts["video_not_on_disk"] += 1
        return None

    def voice(self, talker, m):
        entry = (self.voice_by_svr.get((talker, m["serverId"])) if m["serverId"] else None) \
            or self.voice_by_local.get((talker, m["localId"]))
        if entry is None:
            self.counts["voice_not_found"] += 1
            return None
        con, rowid = entry
        with self.lock:
            row = con.execute("select voice_data from VoiceInfo where rowid=?", (rowid,)).fetchone()
        data = bytes(row[0]) if row and row[0] else b""
        if data[:1] == b"\x02":
            data = data[1:]
        if not data.startswith(SILK_HEADER):
            self.counts["voice_unexpected_format"] += 1
            return None
        out = self.workdir / f"v{rowid}_{m['localId']}.silk"
        out.write_bytes(data)
        self.counts["voice_ok"] += 1
        return Located(out, name=f"voice/{m['localId']}.silk")

    def attachment(self, m):
        raw = m["rawContent"]
        title = xml_value(raw, "title")
        md5 = (xml_value(raw, "md5") or "").lower()
        names = [title] if title else []
        names += self.hardlinks["file"].get(md5, [])
        month = datetime.fromtimestamp(m["createTime"]).strftime("%Y-%m")
        found = []
        for name in dict.fromkeys(names):
            found += self.files.get(name, [])
        if md5 and len(md5) == 32:
            found = [p for p in found if self.file_md5(p) == md5]
        if not found:
            self.counts["file_not_on_disk"] += 1
            return None
        found.sort(key=lambda p: p.parent.name != month)
        path = found[0]
        ext = path.suffix.lower().lstrip(".") or (xml_value(raw, "fileext") or "").lower() or "bin"
        self.counts["file_ok"] += 1
        return Located(path, fallback_ext=re.sub(r"[^a-z0-9]", "", ext) or "bin", name=path.name)

    def rel(self, path):
        return path.relative_to(self.account).as_posix()

    def file_md5(self, path):
        if path not in self.md5_cache:
            self.md5_cache[path] = md5_of(path)
        return self.md5_cache[path]

    def release(self, path):
        path = Path(path)
        if self.workdir in path.parents:
            try:
                path.unlink()
            except OSError:
                pass

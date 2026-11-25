#!/usr/bin/env python3
"""Export Windows WeChat 4.x chats into a wechat-vault staging package, reading the databases directly.

    python export_snapshot.py --out W:\\wechat-export-win [--limit 50 --limit-convs 3] [--check]

WeFlow is not needed. The encrypted databases are opened read-only and decrypted in memory (see
wcdb_snapshot.py); no plaintext copy is written. The passphrase comes from WECHAT_DB_KEY or
WeFlow's config and is never printed. The package format, msg_uid rules and verification are the
same as export_weflow.py, so `export_weflow.py --verify` and the Mac's `import-staging` accept it.

The exporter refuses to run when a table does not have the columns it needs, so a WeChat update
that changes the schema fails loudly instead of producing wrong data. Media (images, voice, video,
files) is located and decoded read-only into a temporary folder under --out, ingested, and removed;
use --no-media for text only. Emoji have no local file and are skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from collections import OrderedDict
from pathlib import Path

import export_weflow as ew
import wcdb_snapshot as ws
from snapshot_media import MediaLocator

try:
    from compression import zstd as _zstd  # Python 3.14+

    def zstd_decompress(data):
        return _zstd.decompress(data)
except ImportError:  # pragma: no cover
    import zstandard

    def zstd_decompress(data):
        return zstandard.ZstdDecompressor().decompress(data)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SENDER_PREFIX_RE = re.compile(r"^([A-Za-z0-9_@.\-]{1,80}):\n")
SNAPSHOT_VERSION = "wcdb-direct-1"
CACHE_CONVERSATIONS = 8

REQUIRED_COLUMNS = {
    "SessionTable": {"username", "last_timestamp", "sort_timestamp"},
    "contact": {"username", "alias", "remark", "nick_name", "local_type"},
    "Name2Id": {"user_name"},
    "Msg": {"local_id", "server_id", "local_type", "sort_seq", "real_sender_id", "create_time", "status",
            "message_content", "packed_info_data"},
}
MESSAGE_SQL = ('select local_id, server_id, local_type, sort_seq, real_sender_id, create_time, '
               'message_content, compress_content, packed_info_data from "{table}"')


def md5_hex(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def table_columns(con, table):
    return {row[1] for row in con.execute(f'pragma table_info("{table}")')}


def require_columns(where, con, table, required):
    have = table_columns(con, table)
    if not have:
        raise ew.ExportError(f"{where}: table {table} is missing; the WeChat database layout has changed")
    missing = sorted(required - have)
    if missing:
        raise ew.ExportError(f"{where}: table {table} lacks columns {missing}; the WeChat database layout "
                             f"has changed and the exporter needs to be updated")


def decode_content(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        if value[:4] == ZSTD_MAGIC:
            value = zstd_decompress(value)
        return value.decode("utf-8", "replace")
    return str(value)


class SnapshotClient:
    """Serves the small slice of WeFlow's HTTP API that Exporter uses, straight from the databases."""

    def __init__(self, db_storage, passphrase, my_wxid=None, log=print):
        self.root = Path(db_storage)
        self.log = log
        self.lock = threading.RLock()
        self.passphrase = passphrase
        self.decode_failures = 0
        self.locator = None
        self.stats = {}
        self.session_con = self.open("session/session.db")
        self.contact_con = self.open("contact/contact.db")
        require_columns("session.db", self.session_con, "SessionTable", REQUIRED_COLUMNS["SessionTable"])
        require_columns("contact.db", self.contact_con, "contact", REQUIRED_COLUMNS["contact"])
        self.message_dbs = []
        for path in sorted((self.root / "message").glob("*message_[0-9]*.db")):
            if path.name.endswith(("_fts.db", "_resource.db")):
                continue
            con, stats = ws.open_memory(path, passphrase)
            self.stats[path.name] = stats
            require_columns(path.name, con, "Name2Id", REQUIRED_COLUMNS["Name2Id"])
            names = {row[0]: row[1] for row in con.execute("select rowid, user_name from Name2Id")}
            tables = {}
            for (name,) in con.execute("select name from sqlite_master where type='table' and name like 'Msg\\_%' escape '\\'"):
                require_columns(f"{path.name}/{name}", con, name, REQUIRED_COLUMNS["Msg"])
                tables[name[4:]] = name
            self.message_dbs.append({"name": path.name, "con": con, "names": names, "tables": tables})
        if not self.message_dbs:
            raise ew.ExportError(f"no message_*.db under {self.root / 'message'}")
        self.known_names = set()
        for db in self.message_dbs:
            self.known_names.update(db["names"].values())
        self.my_wxid = my_wxid or self.guess_my_wxid()
        self.cache = OrderedDict()
        self.build_index()

    def open(self, rel):
        con, stats = ws.open_memory(self.root / rel, self.passphrase)
        self.stats[rel] = stats
        return con

    def guess_my_wxid(self):
        base = self.root.parent.name
        candidates = [base]
        stripped = re.sub(r"_[0-9a-f]{4}$", "", base)
        if stripped != base:
            candidates.insert(0, stripped)
        for candidate in candidates:
            if candidate in self.known_names:
                return candidate
        raise ew.ExportError("cannot tell this account's wxid; set WECHAT_MY_WXID")

    def build_index(self):
        self.username_by_md5 = {}
        for name in self.known_names:
            self.username_by_md5[md5_hex(name)] = name
        self.contacts = {}
        for username, alias, remark, nick, local_type in self.contact_con.execute(
                "select username, alias, remark, nick_name, local_type from contact"):
            self.contacts[username] = (alias or "", remark or "", nick or "", local_type)
            self.username_by_md5.setdefault(md5_hex(username), username)
        sessions = {}
        for username, last_ts, sort_ts in self.session_con.execute(
                "select username, last_timestamp, sort_timestamp from SessionTable"):
            sessions[username] = (ew.to_int(last_ts), ew.to_int(sort_ts))
            self.username_by_md5.setdefault(md5_hex(username), username)
        self.talkers = {}
        for db in self.message_dbs:
            for digest, table in db["tables"].items():
                talker = self.username_by_md5.get(digest)
                if talker is None:
                    raise ew.ExportError(f"{db['name']}/{table}: cannot map this table to a conversation")
                self.talkers.setdefault(talker, []).append((db, table))
        self.session_rows = sessions
        self.no_message_sessions = sorted(set(sessions) - set(self.talkers))

    # -- the WeFlow-shaped API ---------------------------------------------

    def get_json(self, path, params=None, timeout=None):
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/v1/sessions":
            return {"sessions": self.sessions()}
        if path == "/api/v1/contacts":
            return {"contacts": self.contact_list()}
        raise ew.ApiError(f"unsupported path {path}", 404, False)

    def display_name(self, username):
        alias, remark, nick, _ = self.contacts.get(username, ("", "", "", 0))
        return remark or nick or username

    def sessions(self):
        rows = []
        for talker, parts in self.talkers.items():
            last_ts = self.session_rows.get(talker, (0, 0))[0]
            if not last_ts:
                with self.lock:
                    last_ts = max((db["con"].execute(f'select max(create_time) from "{table}"').fetchone()[0] or 0)
                                  for db, table in parts)
            rows.append({"username": talker, "displayName": self.display_name(talker), "lastTimestamp": last_ts,
                         "sort": self.session_rows.get(talker, (0, last_ts))[1] or last_ts})
        rows.sort(key=lambda r: r["sort"], reverse=True)
        return rows

    def contact_list(self):
        return [{"username": u, "alias": a, "nickname": n, "remark": r, "isFriend": lt == 1}
                for u, (a, r, n, lt) in self.contacts.items()]

    def messages(self, talker, offset, limit, media, start=None):
        rows = self.conversation(talker)
        if start:
            rows = [m for m in rows if m["createTime"] >= start]
        page = rows[offset:offset + limit]
        result = {"messages": page, "hasMore": offset + len(page) < len(rows)}
        if media and self.locator is not None:
            page = [self.with_media(talker, m) for m in page]
            result.update(messages=page, media={"exportPath": str(self.locator.workdir)})
        return result

    def with_media(self, talker, m):
        base = m["localType"] & 0xFFFFFFFF
        if base not in (3, 34, 43, 49):
            return m
        found = self.locator.resolve(talker, m)
        if found is None:
            return m
        copy = dict(m, mediaPath=str(found.path), mediaFileName=found.name)
        if found.fallback_ext:
            copy["mediaFallbackExt"] = found.fallback_ext
        if found.thumb_only:
            copy["mediaThumbOnly"] = True
        return copy

    def enable_media(self, account_dir, workdir, image_aes=None, image_xor=None):
        self.locator = MediaLocator(account_dir, self.root, self.passphrase, workdir, image_aes, image_xor)
        return self.locator

    # -- conversion --------------------------------------------------------

    def conversation(self, talker):
        with self.lock:
            if talker in self.cache:
                self.cache.move_to_end(talker)
                return self.cache[talker]
            rows = []
            for db, table in self.talkers.get(talker, []):
                names = db["names"]
                self_id = next((rid for rid, name in names.items() if name == self.my_wxid), None)
                for row in db["con"].execute(MESSAGE_SQL.format(table=table)):
                    rows.append(self.convert(talker, names, self_id, row))
            rows.sort(key=lambda m: (m["createTime"], m["sortSeq"], m["localId"]))
            self.cache[talker] = rows
            while len(self.cache) > CACHE_CONVERSATIONS:
                self.cache.popitem(last=False)
            return rows

    def convert(self, talker, names, self_id, row):
        local_id, server_id, local_type, sort_seq, sender_id, create_time, content, _compress, packed = row
        try:
            raw = decode_content(content)
        except Exception:
            self.decode_failures += 1
            raw = ""
        sender_name = names.get(sender_id, "")
        is_send = self_id is not None and sender_id == self_id
        text = raw
        if talker.endswith("@chatroom") and not is_send:
            match = SENDER_PREFIX_RE.match(raw)
            if match and match.group(1) == sender_name:
                text = raw[match.end():]
        if is_send:
            sender = self.my_wxid
        else:
            sender = sender_name or ("" if talker.endswith("@chatroom") else talker)
        return {"localId": local_id, "serverId": server_id, "localType": local_type, "sortSeq": sort_seq,
                "createTime": create_time, "isSend": 1 if is_send else 0, "senderUsername": sender,
                "content": text, "rawContent": raw,
                "packedInfo": bytes(packed) if isinstance(packed, (bytes, bytearray, memoryview)) else b""}


class SnapshotExporter(ew.Exporter):
    def __init__(self, opts, client):
        super().__init__(opts, client)
        media = client.locator is not None
        self.notes_base = ("direct read of the Windows WeChat 4.x databases (in-memory decrypt); "
                           + ("text and media (images, voice, video, files; no emoji)" if media else "text only"))

    def wants_media(self, rec, m):
        if self.client.locator is None:
            return False
        if rec["kind"] in ("image", "voice", "video"):
            return True
        return rec["kind"] == "app" and rec["local_type"] >> 32 == 6

    def resolve_media(self, raw_path):
        path = Path(raw_path)
        return (path, None) if path.is_file() else (None, "missing")

    def release_media(self, path):
        self.client.locator.release(path)

    def media_ref_for(self, m, raw_path):
        return m.get("mediaFileName")

    def attach_media(self, conv, rec, m):
        super().attach_media(conv, rec, m)
        if m.get("mediaThumbOnly") and rec.get("media_sha256"):
            rec["flags"].append("media_thumbnail_only")
            self.inc("media_thumbnail_only")


def build_parser():
    parser = ew.build_parser()
    parser.description = "Export WeChat chats from the Windows databases into a staging package."
    parser.add_argument("--db-storage", help="<account folder>\\db_storage (default: from WeFlow's config)")
    parser.add_argument("--source", default="win",
                        help="source label stored in the manifest/sync state, e.g. mac-db when run on the Mac")
    parser.add_argument("--weflow-config", help="WeFlow-config.json holding decryptKey (default: %%APPDATA%%)")
    parser.add_argument("--check", action="store_true",
                        help="decrypt, verify the schema and print counts; write nothing")
    return parser


def run_check(client):
    total = 0
    for db in client.message_dbs:
        rows = sum(db["con"].execute(f'select count(*) from "{t}"').fetchone()[0] for t in db["tables"].values())
        total += rows
        stats = client.stats[db["name"]]
        print(f"{db['name']:20} conversations={len(db['tables']):4} messages={rows:7} "
              f"wal_pages={stats['wal_pages']} bad_hmac={stats['bad_hmac']}")
    print(f"conversations with messages: {len(client.talkers)}, sessions without messages: "
          f"{len(client.no_message_sessions)}, total messages: {total}, contacts: {len(client.contacts)}")
    print("schema check: OK (account id", ew.account_id_for(client.my_wxid) + ")")
    return 0


def load_image_keys(weflow_config=None):
    """(aes_key bytes|None, xor_key int|None) from WECHAT_IMAGE_AES_KEY/XOR_KEY or WeFlow's config."""
    cfg = {}
    path = Path(weflow_config) if weflow_config else Path(os.environ.get("APPDATA", "")) / "weflow" / "WeFlow-config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    aes = os.environ.get("WECHAT_IMAGE_AES_KEY") or cfg.get("imageAesKey") or ""
    xor = os.environ.get("WECHAT_IMAGE_XOR_KEY") or cfg.get("imageXorKey")
    aes_key = aes.encode("ascii", "ignore") if isinstance(aes, str) and len(aes) == 16 else None
    try:
        xor_key = int(xor, 0) if isinstance(xor, str) else (int(xor) if xor is not None else None)
    except ValueError:
        xor_key = None
    return aes_key, xor_key if xor_key is None or 0 <= xor_key < 256 else None


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    opts = build_parser().parse_args(argv)
    try:
        if opts.verify:
            return ew.main(["--out", opts.out, "--verify", "--ffprobe", opts.ffprobe])
        passphrase = ws.load_passphrase(opts.weflow_config)
        db_storage = ws.find_db_storage(opts.db_storage)
        client = SnapshotClient(db_storage, passphrase, os.environ.get("WECHAT_MY_WXID"))
        if opts.check:
            return run_check(client)
        workdir = Path(opts.out) / ".media-work"
        if not opts.no_media:
            aes_key, xor_key = load_image_keys(opts.weflow_config)
            client.enable_media(db_storage.parent, workdir, aes_key, xor_key)
        opts.media_only = False
        opts.workers = max(1, min(4, opts.workers))
        opts.page_size = max(1, opts.page_size)
        opts.weflow_version = SNAPSHOT_VERSION
        os.environ["WECHAT_MY_WXID"] = client.my_wxid
        try:
            code = SnapshotExporter(opts, client).run()
        finally:
            if client.locator is not None:
                shutil.rmtree(workdir, ignore_errors=True)
                print("media:", dict(client.locator.counts))
        if client.decode_failures:
            print(f"warning: {client.decode_failures} messages could not be decoded (exported with empty text)")
            code = code or 3
        return code
    except (ew.ExportError, ws.SnapshotError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

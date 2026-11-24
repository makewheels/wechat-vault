#!/usr/bin/env python3
"""Read-only snapshot of a Windows WeChat 4.x database folder into plaintext SQLite copies.

    python wcdb_snapshot.py snapshot --out C:\\workspace\\wechat-db-snapshot
    python wcdb_snapshot.py schema   --snapshot C:\\workspace\\wechat-db-snapshot

The passphrase is read from WECHAT_DB_KEY or from WeFlow's config (never printed). The source
files are only opened for reading. Each database is decrypted page by page (SQLCipher 4:
PBKDF2-HMAC-SHA512 256000 rounds, AES-256-CBC, 80-byte page reserve, HMAC-SHA512 per page),
and committed WAL frames are overlaid so the copy includes messages not yet checkpointed.
The output is private chat data: keep it local, out of Git and out of the cloud.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

try:
    from Crypto.Cipher import AES as _AES

    def aes_cbc_decrypt(key, iv, data):
        return _AES.new(key, _AES.MODE_CBC, iv).decrypt(data)
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def aes_cbc_decrypt(key, iv, data):
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return decryptor.update(data) + decryptor.finalize()

PAGE = 4096
RESERVE = 80
HMAC_LEN = 64
KDF_ITER = 256000
SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_MAGIC = (0x377F0682, 0x377F0683)


class SnapshotError(RuntimeError):
    pass


def load_passphrase(weflow_config=None):
    value = os.environ.get("WECHAT_DB_KEY", "").strip()
    if not value:
        path = Path(weflow_config) if weflow_config else Path(os.environ.get("APPDATA", "")) / "weflow" / "WeFlow-config.json"
        try:
            value = str(json.loads(path.read_text(encoding="utf-8")).get("decryptKey") or "").strip()
        except (OSError, ValueError):
            value = ""
    if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise SnapshotError("no usable 64-hex passphrase (set WECHAT_DB_KEY or configure WeFlow)")
    return bytes.fromhex(value)


class PageCipher:
    def __init__(self, passphrase, salt):
        self.salt = salt
        self.enc_key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, 32)
        self.mac_key = hashlib.pbkdf2_hmac("sha512", self.enc_key, bytes(b ^ 0x3A for b in salt), 2, 32)

    def decrypt(self, page, pgno):
        """Return (plain_page, hmac_ok). An all-zero page is passed through."""
        if not any(page):
            return page, True
        start = 16 if pgno == 1 else 0
        body_end = PAGE - RESERVE
        iv = page[body_end:body_end + 16]
        stored = page[PAGE - HMAC_LEN:]
        calc = hmac.new(self.mac_key, page[start:PAGE - HMAC_LEN] + struct.pack("<I", pgno), hashlib.sha512).digest()
        ok = hmac.compare_digest(calc, stored)
        plain = aes_cbc_decrypt(self.enc_key, iv, page[start:body_end])
        head = SQLITE_MAGIC if pgno == 1 else b""
        return head + plain + page[body_end:], ok


def wal_checksum(data, big_endian, s0=0, s1=0):
    fmt = (">" if big_endian else "<") + f"{len(data) // 4}I"
    words = struct.unpack(fmt, data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def read_wal(path):
    """Return ({pgno: encrypted page}, db_size_pages, frames_total) for committed WAL frames."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, 0, 0
    if len(raw) < 32:
        return {}, 0, 0
    magic, _version, page_size, _seq, salt1, salt2, c1, c2 = struct.unpack(">8I", raw[:32])
    if magic not in WAL_MAGIC or page_size != PAGE:
        return {}, 0, 0
    big = bool(magic & 1)
    if wal_checksum(raw[:24], big) != (c1, c2):
        return {}, 0, 0
    s0, s1 = c1, c2
    frame_size = 24 + PAGE
    pending, committed, db_size, frames = {}, {}, 0, 0
    for offset in range(32, len(raw) - frame_size + 1, frame_size):
        header = raw[offset:offset + 24]
        pgno, commit_size, f_salt1, f_salt2, f_c1, f_c2 = struct.unpack(">6I", header)
        if (f_salt1, f_salt2) != (salt1, salt2) or pgno == 0:
            break
        data = raw[offset + 24:offset + frame_size]
        s0, s1 = wal_checksum(header[:8], big, s0, s1)
        s0, s1 = wal_checksum(data, big, s0, s1)
        if (s0, s1) != (f_c1, f_c2):
            break
        pending[pgno] = data
        frames += 1
        if commit_size:
            committed.update(pending)
            pending = {}
            db_size = commit_size
    return committed, db_size, frames


def decrypt_database(src, passphrase, out):
    """Decrypt one database (+ committed WAL frames) into the writable, seekable file object `out`."""
    wal_pages, wal_size, wal_frames = read_wal(src.with_name(src.name + "-wal"))
    with open(src, "rb") as fh:
        first = fh.read(PAGE)
        if len(first) < PAGE:
            raise SnapshotError("file shorter than one page")
        if first.startswith(SQLITE_MAGIC):
            raise SnapshotError("database is not encrypted")
        cipher = PageCipher(passphrase, first[:16])
        main_pages = (src.stat().st_size + PAGE - 1) // PAGE
        total = max(main_pages, wal_size)
        stats = {"pages": total, "wal_frames": wal_frames, "wal_pages": len(wal_pages), "bad_hmac": 0}
        for pgno in range(1, total + 1):
            if pgno in wal_pages:
                page = wal_pages[pgno]
            else:
                fh.seek((pgno - 1) * PAGE)
                page = fh.read(PAGE).ljust(PAGE, b"\0")
            plain, ok = cipher.decrypt(page, pgno)
            if not ok:
                stats["bad_hmac"] += 1
            out.write(plain)
    out.seek(28)  # header page count must match the overlaid size
    out.write(struct.pack(">I", total))
    out.seek(0, 2)
    return stats


def snapshot_db(src, dst, passphrase):
    """Decrypt one database to a plaintext file at dst. Returns a stats dict."""
    tmp = dst.with_name(dst.name + ".part")
    with open(tmp, "w+b") as out:
        stats = decrypt_database(src, passphrase, out)
    os.replace(tmp, dst)
    return stats


def open_memory(src, passphrase, allow_bad_pages=False):
    """Open an encrypted database as an in-memory read-only SQLite connection; nothing touches disk."""
    buf = io.BytesIO()
    stats = decrypt_database(src, passphrase, buf)
    if stats["bad_hmac"] and not allow_bad_pages:
        raise SnapshotError(f"{src.name}: {stats['bad_hmac']} pages failed integrity check (wrong key?)")
    con = sqlite3.connect(":memory:", check_same_thread=False)
    image = bytearray(buf.getbuffer())
    image[18] = image[19] = 1  # WAL has already been merged; an in-memory image cannot be opened in WAL mode
    con.deserialize(bytes(image))
    con.execute("pragma query_only=1")
    return con, stats


def check_sqlite(path):
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = con.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
        try:
            quick = con.execute("pragma quick_check(1)").fetchone()[0]
        except sqlite3.Error:
            quick = "skipped(custom tokenizer)"  # FTS indexes need WeChat's own tokenizer; not used by the exporter
    finally:
        con.close()
    return tables, quick


def find_db_storage(root=None):
    if root:
        return Path(root)
    config = Path(os.environ.get("APPDATA", "")) / "weflow" / "WeFlow-config.json"
    cfg = json.loads(config.read_text(encoding="utf-8"))
    base = Path(cfg["dbPath"])
    wxid = str(cfg.get("myWxid") or "")
    candidates = [p for p in base.iterdir() if p.is_dir() and (p / "db_storage").is_dir() and p.name == wxid]
    if not candidates:
        raise SnapshotError("cannot locate the account's db_storage; pass --db-storage")
    return candidates[0] / "db_storage"


def cmd_snapshot(args):
    passphrase = load_passphrase(args.weflow_config)
    source = find_db_storage(args.db_storage)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    report = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source_name": source.parent.name[:4] + "***", "databases": {}}
    problems = 0
    for src in sorted(source.rglob("*.db")):
        rel = src.relative_to(source)
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        try:
            stats = snapshot_db(src, dst, passphrase)
            tables, quick = check_sqlite(dst)
            stats.update(tables=tables, quick_check=quick, size=dst.stat().st_size)
            if stats["bad_hmac"] or not (quick == "ok" or quick.startswith("skipped")):
                problems += 1
        except (SnapshotError, OSError, sqlite3.Error) as exc:
            stats = {"error": f"{type(exc).__name__}: {exc}"}
            problems += 1
        stats["seconds"] = round(time.time() - started, 1)
        report["databases"][rel.as_posix()] = stats
        print(f"{rel.as_posix():40} " + " ".join(f"{k}={v}" for k, v in stats.items()))
    (out_root / "snapshot.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done: {len(report['databases'])} databases, {problems} with problems")
    return 0 if problems == 0 else 3


def cmd_schema(args):
    root = Path(args.snapshot)
    only = set(args.db or [])
    for path in sorted(root.rglob("*.db")):
        rel = path.relative_to(root).as_posix()
        if only and rel not in only and path.name not in only:
            continue
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
            print(f"== {rel}  ({len(tables)} tables)")
            groups = {}
            for name in tables:
                columns = tuple((c[1], c[2]) for c in con.execute(f'pragma table_info("{name}")'))
                groups.setdefault(columns, []).append(name)
            for columns, names in groups.items():
                label = names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1} same-shaped: e.g. {names[1]})"
                print(f"  {label}: " + ", ".join(f"{n} {t}" for n, t in columns))
        finally:
            con.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="decrypt every .db into --out (read-only on the source)")
    snap.add_argument("--out", required=True)
    snap.add_argument("--db-storage", help="path to <account>\\db_storage (default: from WeFlow config)")
    snap.add_argument("--weflow-config")
    snap.set_defaults(func=cmd_snapshot)
    sch = sub.add_parser("schema", help="print table/column layout (no row data) of a snapshot")
    sch.add_argument("--snapshot", required=True)
    sch.add_argument("--db", action="append", help="limit to this relative path or file name (repeatable)")
    sch.set_defaults(func=cmd_schema)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

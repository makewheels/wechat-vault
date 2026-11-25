#!/usr/bin/env python3
"""Export WeChat chats from WeFlow's local HTTP API into the wechat-archive staging package.

Implements the staging package contract in HANDOFF-WINDOWS.md section 3 with the standard
library only. It talks to WeFlow on loopback, never reads the WeChat database, never edits
WeFlow's configuration and never writes Tablestore.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import html
import http.client
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
CST = timezone(timedelta(hours=8))
DEFAULT_API_BASE = "http://127.0.0.1:5031"
DEFAULT_PAGE_SIZE = 200
FALLBACK_PAGE_SIZE = 50
PROBE_PAGE_SIZE = 500
API_TIMEOUT = 30
MEDIA_PAGE_TIMEOUT = 120
RETRY_BACKOFF = (1, 2, 4)
LOOKBACK_SEC = 24 * 3600
MAX_RAW_BYTES = 256 * 1024
MAX_MEDIA_BYTES = 4 * 1024 ** 3
COPY_CHUNK = 1024 * 1024
MEDIA_KINDS = frozenset({"image", "voice", "video", "emoji"})
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SLEEP = time.sleep

KIND_BY_TYPE = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji",
                48: "location", 42: "card", 49: "app", 10000: "system"}

SYSTEM_TALKERS = frozenset({
    "weixin", "filehelper", "fmessage", "medianote", "floatbottle", "newsapp", "notifymessage",
    "tmessage", "qmessage", "qqmail", "masssendapp", "officialaccounts", "brandsessionholder",
    "brandservicesessionholder", "notification_messages", "opencustomerservicemsg", "exmail_tool",
    "mphelper", "downloaderapp", "weibo", "lbsapp", "shakeapp", "readerapp", "voipapp",
    "userexperience_alarm", "qqfriend", "wxitil", "weixinguanhaozhushou",
})
API_SESSION_TYPES = {"single": "single", "private": "single", "friend": "single", "group": "group",
                     "chatroom": "group", "official": "official", "system": "system",
                     "other": "other", "channel": "other"}

TOKEN_RE = re.compile(r"(?<!\w)((?:access_)?token)\s*=\s*([\"']?)[^&\"'\s<>]*", re.IGNORECASE)
AESKEY_ATTR_RE = re.compile(r"\s+[\w:.-]*aeskey[\w:.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*')", re.IGNORECASE)
AESKEY_ELEM_RE = re.compile(r"<([\w:.-]*aeskey[\w:.-]*)>[^<]*</\1>", re.IGNORECASE)
TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[(.*?)\]\]>|(.*?))\s*</title>", re.DOTALL)
APPTYPE_RE = re.compile(r"<type>\s*(\d+)\s*</type>")
CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
SURROGATE_RE = re.compile("[\ud800-\udfff]")
SHA_NAME_RE = re.compile(r"^([0-9a-f]{64})\.([a-z0-9]+)$")
SNIFFABLE_EXTS = frozenset({"jpg", "png", "gif", "webp", "wav", "mp4", "pdf", "zip", "silk", "bmp"})

class ExportError(Exception):
    """Fatal problem that stops the whole run."""


class ApiError(Exception):
    def __init__(self, message, status=None, retryable=True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Pure helpers (ids, kinds, text, sanitising, magic bytes)
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def to_int(value, default=0):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def scrub(text):
    """Remove token values from text that may end up in logs or errors.jsonl."""
    return TOKEN_RE.sub(r"\1=\2", str(text))


def conv_key(talker):
    return hashlib.sha1(talker.encode("utf-8")).hexdigest()[:16]


def account_id_for(my_wxid):
    return "a" + hashlib.sha1(my_wxid.encode("utf-8")).hexdigest()[:12]


def wxid_hash(my_wxid):
    return "sha256:" + hashlib.sha256(my_wxid.encode("utf-8")).hexdigest()


def msg_uid_for(talker, server_id, create_time, is_send, local_type, content, sender):
    sid = "" if server_id is None else str(server_id).strip()
    if sid and sid != "0":
        return "s" + sid
    basis = f"{talker}|{create_time}|{is_send}|{local_type}|{content}|{sender}"
    return "h" + hashlib.sha1(scrub(basis).encode("utf-8")).hexdigest()[:20]


def kind_for(local_type):
    kind = KIND_BY_TYPE.get(local_type)
    if kind:
        return kind
    if local_type > 0xFFFFFFFF and (local_type & 0xFFFFFFFF) == 49:
        return "app"
    return "other"


def classify_session(talker, api_type=None):
    if talker.endswith("@chatroom"):
        return "group"
    if talker.startswith("gh_"):
        return "official"
    if "@openim" in talker:
        return "other"
    if talker in SYSTEM_TALKERS or talker.startswith("@"):
        return "system"
    if isinstance(api_type, str) and api_type.lower() in API_SESSION_TYPES:
        return API_SESSION_TYPES[api_type.lower()]
    return "single"


def sanitize_raw(raw):
    """Drop AES keys and tokens from raw XML/text; cut at 256KB. Returns (text, truncated)."""
    cleaned = AESKEY_ELEM_RE.sub("", raw)
    cleaned = AESKEY_ATTR_RE.sub("", cleaned)
    cleaned = TOKEN_RE.sub(r"\1=\2", cleaned)
    cleaned = LEAK_RE.sub("", cleaned)
    data = cleaned.encode("utf-8", "replace")
    if len(data) <= MAX_RAW_BYTES:
        return cleaned, False
    return data[:MAX_RAW_BYTES].decode("utf-8", "ignore"), True


def text_for(kind, content, raw):
    if kind == "app":
        match = TITLE_RE.search(raw or "")
        if match:
            title = html.unescape((match.group(1) if match.group(1) is not None else match.group(2)).strip())
            if title:
                return title
        app_type = APPTYPE_RE.search(raw or "")
        return "[文件]" if app_type and app_type.group(1) == "6" else "[链接]"
    if kind == "system" and content.lstrip().startswith("<"):
        plain = TAG_RE.sub("", CDATA_RE.sub(r"\1", content))
        return html.unescape(plain).strip()
    return content


def normalize_message(talker, account_id, m):
    local_type = to_int(m.get("localType"))
    create_time = to_int(m.get("createTime"))
    if create_time > 10 ** 11:
        create_time //= 1000
    is_send = 1 if to_int(m.get("isSend")) else 0
    sender = str(m.get("senderUsername") or "")
    content = m.get("content")
    content = "" if content is None else str(content)
    raw_source = m.get("rawContent")
    raw_source = content if raw_source in (None, "") else str(raw_source)
    bad_utf16 = any(SURROGATE_RE.search(s) for s in (sender, content, raw_source))
    sender, content, raw_source = scrub(sender), scrub(content), scrub(raw_source)
    server_id = m.get("serverId")
    kind = kind_for(local_type)
    raw, truncated = sanitize_raw(raw_source)
    local_id = to_int(m.get("localId"), None)
    sid = "" if server_id is None else str(server_id).strip()
    return {
        "schema_version": SCHEMA_VERSION,
        "conv_id": f"{account_id}:{talker}",
        "talker": talker,
        "msg_uid": msg_uid_for(talker, server_id, create_time, is_send, local_type, content, sender),
        "server_id": sid if sid and sid != "0" else None,
        "local_id": local_id,
        "local_seq": local_id,
        "sort_seq": to_int(m.get("sortSeq")) or create_time * 1000,
        "create_time": create_time,
        "is_send": is_send,
        "sender": sender,
        "local_type": local_type,
        "kind": kind,
        "text": sanitize_raw(text_for(kind, content, raw))[0],
        "raw": raw,
        "media_ref": None,
        "media_sha256": None,
        "media_ext": None,
        "media_size": None,
        "media_mime": None,
        "flags": (["truncated_raw"] if truncated else []) + (["bad_utf16"] if bad_utf16 else []),
    }


def scrub(value):
    """Replace lone UTF-16 surrogates (not encodable as UTF-8) with U+FFFD, recursively."""
    if isinstance(value, str):
        return SURROGATE_RE.sub("\ufffd", value) if SURROGATE_RE.search(value) else value
    if isinstance(value, dict):
        return {scrub(k): scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def dumps(obj):
    """One JSONL line; always valid UTF-8 (lone surrogates become U+FFFD)."""
    return json.dumps(scrub(obj), ensure_ascii=False, separators=(",", ":"))


def sniff(head):
    """Return (ext, mime) when the bytes look like a known plaintext media type."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if head[:4] == b"\x89PNG":
        return "png", "image/png"
    if head[:3] == b"GIF":
        return "gif", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav", "audio/wav"
    if head[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    if head[:4] == b"%PDF":
        return "pdf", "application/pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip", "application/zip"
    if head[:9] == b"#!SILK_V3" or head[1:10] == b"#!SILK_V3":
        return "silk", "audio/silk"
    if (head[:2] == b"BM" and head[6:10] == b"\0\0\0\0"
            and int.from_bytes(head[14:18], "little") in (12, 40, 52, 56, 64, 108, 124)):
        return "bmp", "image/bmp"
    return None


def iter_jsonl(path):
    with open(path, "r", encoding="utf-8", newline="\n") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line:
                yield line


def atomic_write_text(path, text):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def sort_jsonl_file(path):
    rows = []
    for line in iter_jsonl(path):
        rec = json.loads(line)
        rows.append((to_int(rec.get("sort_seq")), str(rec.get("msg_uid")), line))
    rows.sort(key=lambda row: (row[0], row[1]))
    atomic_write_text(path, "".join(row[2] + "\n" for row in rows))


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@dataclass
class MediaOutcome:
    status: str  # ok | empty | encrypted | unverified | too_large
    sha256: str = None
    ext: str = None
    size: int = None
    mime: str = None
    probe_skipped: bool = False


def probe_video(ffprobe, path):
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration,format_name", "-of", "json", str(path)],
            capture_output=True, timeout=120, check=False, creationflags=NO_WINDOW)
        if result.returncode != 0:
            return False
        fmt = json.loads(result.stdout.decode("utf-8", "replace")).get("format") or {}
        return bool(fmt.get("format_name"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def ingest_media(src, media_dir, kind, ffprobe, tag, fallback_ext=None):
    """Copy a plaintext media file into media/<sha256>.<ext>; never stores unverified or encrypted bytes.

    fallback_ext is for user attachments (documents, archives) whose type cannot be sniffed.
    """
    size = src.stat().st_size
    if size == 0:
        return MediaOutcome("empty", size=0)
    if size > MAX_MEDIA_BYTES:
        return MediaOutcome("too_large", size=size)
    tmp = media_dir / f"{tag}.tmp"
    try:
        with open(src, "rb") as source:
            first = source.read(COPY_CHUNK)
            sig = sniff(first[:32])
            if sig is None and fallback_ext:
                ext = fallback_ext
                mime = mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream"
            elif sig is None:
                return MediaOutcome("encrypted", size=size)
            elif fallback_ext and sig[0] == "zip" and fallback_ext != "zip":
                ext, mime = fallback_ext, mimetypes.guess_type(f"x.{fallback_ext}")[0] or sig[1]
            else:
                ext, mime = sig
            digest = hashlib.sha256()
            total = 0
            with open(tmp, "wb") as target:
                chunk = first
                while chunk:
                    digest.update(chunk)
                    target.write(chunk)
                    total += len(chunk)
                    chunk = source.read(COPY_CHUNK)
        probe_skipped = False
        if kind == "video" or ext == "mp4":
            if ffprobe:
                if not probe_video(ffprobe, tmp):
                    return MediaOutcome("unverified", size=total)
            else:
                probe_skipped = True
        sha = digest.hexdigest()
        final = media_dir / f"{sha}.{ext}"
        if final.exists():
            tmp.unlink()
        else:
            os.replace(tmp, final)
        return MediaOutcome("ok", sha, ext, total, mime, probe_skipped)
    finally:
        if tmp.exists():
            tmp.unlink()


def resolve_media_path(raw_path, allowed_root):
    """Return (path, problem). Only files under WeFlow's api-media export root are read."""
    try:
        path = Path(raw_path).resolve()
    except (OSError, ValueError):
        return None, "invalid"
    if allowed_root is not None:
        try:
            path.relative_to(allowed_root)
        except ValueError:
            return None, "outside"
    elif "api-media" not in {part.lower() for part in path.parts}:
        return None, "outside"
    return (path, None) if path.is_file() else (None, "missing")


# ---------------------------------------------------------------------------
# WeFlow API client
# ---------------------------------------------------------------------------

class WeFlowClient:
    def __init__(self, base, token=None):
        self.base = base.rstrip("/")
        self.token = token or None
        self.token_in_query = False
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get_json(self, path, params=None, timeout=API_TIMEOUT):
        last = None
        for attempt in range(len(RETRY_BACKOFF) + 1):
            try:
                return self._get_once(path, params, timeout)
            except ApiError as err:
                last = err
                if not err.retryable or attempt == len(RETRY_BACKOFF):
                    raise
                SLEEP(RETRY_BACKOFF[attempt])
        raise last

    def _get_once(self, path, params, timeout):
        query = dict(params or {})
        headers = {"Accept": "application/json"}
        if self.token:
            if self.token_in_query:
                query["access_token"] = self.token
            else:
                headers["Authorization"] = f"Bearer {self.token}"
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            with self._opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as err:
            err.close()
            if err.code in (401, 403) and self.token and not self.token_in_query:
                self.token_in_query = True
                return self._get_once(path, params, timeout)
            retryable = err.code in (408, 425, 429) or err.code >= 500
            raise ApiError(f"HTTP {err.code}", err.code, retryable) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as err:
            reason = getattr(err, "reason", err)
            raise ApiError(scrub(f"{type(err).__name__}: {reason}"), None, True) from None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("invalid JSON response", None, True) from None

    def messages(self, talker, offset, limit, media, start=None):
        params = {"talker": talker, "limit": limit, "offset": offset}
        if media:
            params["media"] = 1
        if start:
            params["start"] = start
        data = self.get_json("/api/v1/messages", params, MEDIA_PAGE_TIMEOUT if media else API_TIMEOUT)
        if not isinstance(data, dict):
            raise ApiError("unexpected response shape", None, True)
        if data.get("error"):
            raise ApiError(scrub(data["error"]), None, True)
        return data


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------

@dataclass
class Session:
    talker: str
    name: str
    session_type: str
    last_ts: int


def normalize_session(item):
    talker = str(item.get("username") or item.get("talker") or item.get("id") or "").strip()
    if not talker:
        return None
    last_ts = to_int(item.get("lastTimestamp") or item.get("last_ts"))
    if last_ts > 10 ** 11:
        last_ts //= 1000
    api_type = item.get("sessionType", item.get("type"))
    return Session(talker, str(item.get("displayName") or item.get("name") or talker),
                   classify_session(talker, api_type), last_ts)


class Conv:
    def __init__(self, talker):
        self.talker = talker
        self.errors = 0


class Exporter:
    def __init__(self, opts, client=None):
        self.opts = opts
        self.out = Path(opts.out)
        self.messages_dir = self.out / "messages"
        self.media_dir = self.out / "media"
        self.client = client or WeFlowClient(opts.api_base, os.environ.get("WEFLOW_ACCESS_TOKEN"))
        self.lock = threading.Lock()
        self.stats = collections.Counter()
        self.state = {"version": 1, "conversations": {}}
        self.account_id = None
        self.my_wxid_hash = None
        self.media_root = None
        self.stop = threading.Event()
        self.error_count = 0
        self.ffprobe = None
        if opts.ffprobe and opts.ffprobe.lower() != "none":
            self.ffprobe = shutil.which(opts.ffprobe)

    # -- small utilities ----------------------------------------------------

    def log(self, message):
        with self.lock:
            print(message, flush=True)

    def inc(self, name, amount=1):
        with self.lock:
            self.stats[name] += amount

    def record_error(self, conv, stage, offset, error, retryable):
        row = {"ts": now_iso(), "talker": conv.talker, "stage": stage, "offset": offset,
               "error": scrub(error), "retryable": retryable}
        with self.lock:
            conv.errors += 1
            self.error_count += 1
            with open(self.out / "errors.jsonl", "a", encoding="utf-8", newline="\n") as handle:
                handle.write(dumps(row) + "\n")

    def save_state(self):
        atomic_write_text(self.out / "state.json", json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def tmp_tag(self):
        return f"{os.getpid()}-{threading.get_ident()}"

    # -- preparation --------------------------------------------------------

    def prepare(self):
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(self.out).free / 1024 ** 3
        if free_gb < self.opts.min_free_gb:
            raise ExportError(f"only {free_gb:.1f} GB free on the output drive (need {self.opts.min_free_gb} GB)")
        for folder in (self.messages_dir, self.media_dir, self.out):
            for stale in folder.glob("*.tmp"):
                stale.unlink()
        state_path = self.out / "state.json"
        if state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                self.state["conversations"] = dict(loaded.get("conversations") or {})
            except (ValueError, OSError) as err:
                raise ExportError(f"state.json is unreadable ({err}); move it away to start over")
        (self.out / "errors.jsonl").write_text("", encoding="utf-8")
        manifest = self.out / "manifest.json"
        if manifest.exists():
            manifest.unlink()

    def preflight(self):
        try:
            self.client.get_json("/health", None, API_TIMEOUT)
        except ApiError as err:
            raise ExportError(
                f"WeFlow API is not reachable ({err}). Open WeFlow > 设置 > API 服务 and switch on "
                f"'HTTP API 服务' (port 5031).")

    def list_sessions(self):
        data = self.client.get_json("/api/v1/sessions", {"limit": 5000})
        items = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ExportError("unexpected /api/v1/sessions response")
        seen, unique = set(), []
        for item in items:
            sess = normalize_session(item) if isinstance(item, dict) else None
            if sess and sess.talker not in seen:
                seen.add(sess.talker)
                unique.append(sess)
        return unique

    def select(self, sessions):
        if self.opts.conv:
            known = {s.talker for s in sessions}
            missing = [t for t in self.opts.conv if t not in known]
            if missing:
                raise ExportError(f"{len(missing)} --conv talker(s) not found in WeFlow sessions")
            wanted = set(self.opts.conv)
            sessions = [s for s in sessions if s.talker in wanted]
        if self.opts.limit_convs:
            sessions = sessions[: self.opts.limit_convs]
        return sessions

    def infer_my_wxid(self, sessions):
        """Find the account's own wxid from isSend=1 senders on the first page of a few conversations."""
        votes = collections.Counter()
        ordered = [s for s in sessions if s.session_type == "single"] + \
                  [s for s in sessions if s.session_type != "single"]
        for sess in ordered[:40]:
            try:
                data = self.client.messages(sess.talker, 0, DEFAULT_PAGE_SIZE, False)
            except ApiError:
                continue
            mine = {str(m.get("senderUsername") or "") for m in data.get("messages") or []
                    if to_int(m.get("isSend")) and m.get("senderUsername")}
            mine.discard(sess.talker)
            for wxid in mine:
                votes[wxid] += 1
            if votes and votes.most_common(1)[0][1] >= 2:
                break
        return votes.most_common(1)[0][0] if votes else None

    def resolve_account(self, sessions):
        expected = self.opts.account_id
        wxid = os.environ.get("WECHAT_MY_WXID") or self.infer_my_wxid(sessions)
        if wxid:
            inferred = account_id_for(wxid)
            if expected and expected != inferred:
                raise ExportError(f"account mismatch: expected {expected}, this WeChat account is {inferred}")
            self.account_id = inferred
            self.my_wxid_hash = wxid_hash(wxid)
        elif expected:
            self.account_id = expected
        else:
            raise ExportError("cannot tell which WeChat account this is: no sent message with a sender was found. "
                              "Pass --account-id <a...> (from the Mac side) or set WECHAT_MY_WXID.")

    # -- paging -------------------------------------------------------------

    def get_page(self, talker, offset, size, media, start, expect_rows):
        sizes = [size] + ([FALLBACK_PAGE_SIZE] if size > FALLBACK_PAGE_SIZE else [])
        last = None
        for used in sizes:
            attempts = 0
            while True:
                try:
                    data = self.client.messages(talker, offset, used, media, start)
                except ApiError as err:
                    last = err
                    break
                msgs = data.get("messages")
                msgs = msgs if isinstance(msgs, list) else []
                if not msgs and expect_rows and attempts < len(RETRY_BACKOFF):
                    SLEEP(RETRY_BACKOFF[attempts])
                    attempts += 1
                    continue
                self.note_media_root(data)
                return msgs, bool(data.get("hasMore")), used
        raise last

    def note_media_root(self, data):
        info = data.get("media")
        if self.media_root is None and isinstance(info, dict) and info.get("exportPath"):
            self.media_root = Path(info["exportPath"]).resolve()

    def probe_total(self, talker, expect_rows):
        """Count a conversation's messages with exponential + binary search on offset (no media)."""
        def rows_at(offset):
            return self.client.messages(talker, offset, PROBE_PAGE_SIZE, False).get("messages") or []

        first = rows_at(0)
        for delay in RETRY_BACKOFF:
            if first or not expect_rows:
                break
            SLEEP(delay)
            first = rows_at(0)
        if len(first) < PROBE_PAGE_SIZE:
            return len(first)
        low, high = 0, PROBE_PAGE_SIZE
        while rows_at(high):
            low, high = high, high * 2
        while high - low > PROBE_PAGE_SIZE:
            mid = (low + high) // 2
            if rows_at(mid):
                low = mid
            else:
                high = mid
        return low + len(rows_at(low))

    # -- media --------------------------------------------------------------

    def media_ref_for(self, m, raw_path):
        if raw_path and self.media_root is not None:
            try:
                return Path(raw_path).resolve().relative_to(self.media_root).as_posix()
            except (ValueError, OSError):
                pass
        name = m.get("mediaFileName")
        return str(name) if name else None

    def wants_media(self, rec, m):
        return rec["kind"] in MEDIA_KINDS

    def resolve_media(self, raw_path):
        return resolve_media_path(raw_path, self.media_root)

    def release_media(self, path):
        """Called after a media file has been ingested (or failed); subclasses may delete temp files."""

    def attach_media(self, conv, rec, m):
        flags = rec["flags"]
        raw_path = m.get("mediaPath") or m.get("mediaLocalPath")
        rec["media_ref"] = self.media_ref_for(m, raw_path)
        if not raw_path:
            flags.append("media_missing")
            self.inc("media_missing")
            return
        path, problem = self.resolve_media(str(raw_path))
        if path is None:
            flags.append("media_missing")
            self.inc("media_missing")
            if problem == "outside":
                self.record_error(conv, "media", None, "media path is outside WeFlow's export folder", False)
            return
        try:
            outcome = ingest_media(path, self.media_dir, rec["kind"], self.ffprobe, self.tmp_tag(),
                                   m.get("mediaFallbackExt"))
        except OSError as err:
            flags.append("media_missing")
            self.inc("media_missing")
            self.record_error(conv, "media", None, f"{type(err).__name__} errno={err.errno}", True)
            return
        finally:
            self.release_media(path)
        if outcome.status in ("empty", "too_large"):
            flags.append("media_missing" if outcome.status == "empty" else "media_too_large")
            self.inc("media_missing" if outcome.status == "empty" else "media_too_large")
        elif outcome.status == "encrypted":
            flags.append("media_encrypted")
            self.inc("media_encrypted")
        elif outcome.status == "unverified":
            flags.append("media_unverified")
            self.inc("media_unverified")
        else:
            rec.update(media_sha256=outcome.sha256, media_ext=outcome.ext, media_size=outcome.size,
                       media_mime=outcome.mime)
            if outcome.probe_skipped:
                flags.append("media_unverified")
                self.inc("media_unverified")
            else:
                self.inc("media_verified")

    # -- one conversation -----------------------------------------------------

    def pending_media(self, path):
        count = 0
        for line in iter_jsonl(path):
            rec = json.loads(line)
            if rec.get("kind") in MEDIA_KINDS and not rec.get("media_sha256"):
                count += 1
        return count

    def needs_export(self, sess):
        opts = self.opts
        final = self.messages_dir / f"{conv_key(sess.talker)}.jsonl"
        entry = self.state["conversations"].get(sess.talker) or {}
        if opts.media_only:
            return final.exists() and self.pending_media(final) > 0
        if opts.full or opts.limit or opts.since:
            return True
        return not (entry.get("done") and final.exists()) or opts.incremental

    def export_conversation(self, sess, position, total):
        opts = self.opts
        talker = sess.talker
        key = conv_key(talker)
        final = self.messages_dir / f"{key}.jsonl"
        tmp = self.messages_dir / f"{key}.jsonl.tmp"
        conv = Conv(talker)
        prev = dict(self.state["conversations"].get(talker) or {})
        limited = opts.limit > 0
        prev_ct = to_int(prev.get("last_create_time"))
        merge = bool(opts.incremental and not limited and prev.get("done") and final.exists() and prev_ct > 0)
        # --since writes a package holding only recent messages; the importer skips what the cloud already has.
        threshold = opts.since or (max(0, prev_ct - LOOKBACK_SEC) if merge else 0)
        start_param = threshold or None
        want_media = not opts.no_media
        seen = set()
        counts = collections.Counter()
        last_sort = 0
        last_ct = 0
        prev_key = None
        unsorted = False
        ok = True

        def emit(out, rec, line=None):
            nonlocal last_sort, last_ct, prev_key, unsorted
            order = (rec["sort_seq"], rec["msg_uid"])
            if prev_key is not None and order < prev_key:
                unsorted = True
            prev_key = order if prev_key is None or order > prev_key else prev_key
            last_sort = max(last_sort, rec["sort_seq"])
            last_ct = max(last_ct, rec["create_time"])
            counts["messages"] += 1
            if rec.get("media_sha256"):
                counts["media"] += 1
            out.write((line if line is not None else dumps(rec)) + "\n")

        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as out:
                if merge:
                    for line in iter_jsonl(final):
                        rec = json.loads(line)
                        if to_int(rec.get("create_time")) >= threshold:
                            continue
                        seen.add(rec["msg_uid"])
                        emit(out, rec, line)
                offset = 0
                remaining = None
                if limited:
                    rows = self.probe_total(talker, sess.last_ts > 0)
                    if rows == 0 and sess.last_ts > 0:
                        self.record_error(conv, "messages", 0, "empty_page", True)
                        ok = False
                    remaining = min(opts.limit, rows)
                    offset = rows - remaining
                page_size = opts.page_size
                first_page = True
                while ok and not self.stop.is_set():
                    want = page_size if remaining is None else min(page_size, remaining)
                    if want <= 0:
                        break
                    expect_rows = first_page and sess.last_ts > 0 and not merge and not opts.since
                    msgs, has_more, page_size = self.get_page(talker, offset, want, want_media, start_param,
                                                              expect_rows)
                    if not msgs and expect_rows:
                        self.record_error(conv, "messages", offset, "empty_page", True)
                        ok = False
                        break
                    first_page = False
                    for m in msgs:
                        rec = normalize_message(talker, self.account_id, m)
                        if rec["create_time"] < threshold or rec["msg_uid"] in seen:
                            continue
                        seen.add(rec["msg_uid"])
                        if want_media and self.wants_media(rec, m):
                            self.attach_media(conv, rec, m)
                        emit(out, rec)
                    offset += len(msgs)
                    if remaining is not None:
                        remaining -= len(msgs)
                    if not msgs or not has_more:
                        break
                if self.stop.is_set():
                    ok = False
        except ApiError as err:
            self.record_error(conv, "messages", None, str(err), err.retryable)
            ok = False
        except Exception as err:
            where = err.__traceback__
            while where.tb_next:
                where = where.tb_next
            self.record_error(conv, "internal", None, f"{type(err).__name__} at line {where.tb_lineno}", False)
            ok = False
        finally:
            if not ok and tmp.exists():
                tmp.unlink()

        entry = dict(prev)
        if ok and opts.since and not counts["messages"]:
            tmp.unlink(missing_ok=True)
            entry.update(done=False, messages=0, media=0, errors=conv.errors)
        elif ok:
            if unsorted:
                sort_jsonl_file(tmp)
            os.replace(tmp, final)
            entry.update(done=not limited, last_sort_seq=last_sort, last_create_time=last_ct,
                         messages=counts["messages"], media=counts["media"], errors=conv.errors)
        else:
            entry.setdefault("messages", 0)
            entry.setdefault("media", 0)
            entry.setdefault("last_sort_seq", 0)
            entry.update(done=False, errors=conv.errors)
        entry["updated_at"] = now_iso()
        with self.lock:
            self.state["conversations"][talker] = entry
            self.save_state()
            self.stats["conversations_ok" if ok else "conversations_failed"] += 1
        if opts.since and ok and not counts["messages"]:
            return
        self.log(f"[{position}/{total}] {key} {sess.session_type:8s} {'ok' if ok else 'FAILED'} "
                 f"messages={counts['messages']} media={counts['media']} errors={conv.errors}")

    # -- outputs ----------------------------------------------------------------

    def write_sessions(self, all_sessions):
        rows = []
        for sess in all_sessions:
            if not (self.messages_dir / f"{conv_key(sess.talker)}.jsonl").exists():
                continue
            entry = self.state["conversations"].get(sess.talker) or {}
            rows.append({"talker": sess.talker, "name": sess.name,
                         "is_group": sess.talker.endswith("@chatroom") or sess.session_type == "group",
                         "session_type": sess.session_type, "last_ts": sess.last_ts,
                         "msg_count": to_int(entry.get("messages"))})
        atomic_write_text(self.out / "sessions.jsonl", "".join(dumps(r) + "\n" for r in rows))
        return rows

    def write_contacts(self):
        rows = []
        try:
            data = self.client.get_json("/api/v1/contacts", {"limit": 10000})
            for item in (data.get("contacts") if isinstance(data, dict) else None) or []:
                if not isinstance(item, dict):
                    continue
                wxid = str(item.get("username") or item.get("wxid") or "")
                if not wxid:
                    continue
                is_friend = bool(item["isFriend"]) if "isFriend" in item else item.get("type") in ("friend", 1, "1")
                rows.append({"wxid": wxid, "alias": str(item.get("alias") or ""),
                             "nickname": str(item.get("nickname") or ""),
                             "remark": str(item.get("remark") or ""), "is_friend": is_friend})
        except ApiError as err:
            self.log(f"contacts skipped: {err}")
        atomic_write_text(self.out / "contacts.jsonl", "".join(dumps(r) + "\n" for r in rows))
        return len(rows)

    def media_totals(self):
        files = size = 0
        for entry in self.media_dir.iterdir():
            if entry.is_file() and SHA_NAME_RE.match(entry.name):
                files += 1
                size += entry.stat().st_size
        return files, size

    def write_manifest(self, run_id, session_rows):
        opts = self.opts
        media_files, media_bytes = self.media_totals()
        keys = {conv_key(r["talker"]) for r in session_rows}
        messages = sum(to_int(e.get("messages")) for t, e in self.state["conversations"].items()
                       if conv_key(t) in keys)
        notes = getattr(self, "notes_base", "WeFlow HTTP API; media copied from WeFlow's local api-media export folder")
        if opts.limit:
            notes += f"; smoke run: latest {opts.limit} messages per conversation"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "source": getattr(opts, "source", None) or "win",
            "run_id": run_id,
            "account_id": self.account_id,
            "my_wxid_hash": self.my_wxid_hash,
            "weflow_version": opts.weflow_version,
            "exported_at": now_iso(),
            "full": bool(opts.full and not (opts.conv or opts.limit_convs or opts.limit or opts.no_media)),
            **({"since": opts.since} if opts.since else {}),
            "counts": {"sessions": len(session_rows), "messages": messages, "media_files": media_files,
                       "media_bytes": media_bytes, "errors": self.error_count},
            "media_prefix": "media/sha256/",
            "staging_prefix": f"staging/win/{run_id}/",
            "notes": notes,
        }
        atomic_write_text(self.out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return manifest

    def write_report(self, manifest, session_rows, started):
        kinds = collections.Counter(r["session_type"] for r in session_rows)
        counts = manifest["counts"]
        stats = self.stats
        elapsed = int(time.time() - started)
        lines = [
            "source: win",
            f"run_id: {manifest['run_id']}",
            f"account_id: {manifest['account_id']}",
            f"weflow_version: {manifest['weflow_version']}",
            f"sessions: {counts['sessions']} (" + " / ".join(
                f"{name} {kinds.get(name, 0)}" for name in ("single", "group", "official", "system", "other")) + ")",
            f"messages: {counts['messages']}",
            f"media: {counts['media_files']} files / {counts['media_bytes'] / 1024 ** 3:.2f} GB",
            f"media_verified: {stats['media_verified']} / unverified {stats['media_unverified']} / "
            f"missing {stats['media_missing']} / encrypted {stats['media_encrypted']}",
            f"errors: {counts['errors']}",
            f"staging: local {self.out}",
            f"duration: {elapsed // 3600}h{elapsed % 3600 // 60:02d}m{elapsed % 60:02d}s",
            f"notes: {manifest['notes']}",
        ]
        text = "\n".join(lines) + "\n"
        atomic_write_text(self.out / "report.md", "```text\n" + text + "```\n")
        return text

    # -- driver -------------------------------------------------------------

    def run(self):
        started = time.time()
        self.prepare()
        self.preflight()
        all_sessions = self.list_sessions()
        self.log(f"WeFlow sessions: {len(all_sessions)}")
        selected = self.select(all_sessions)
        self.resolve_account(all_sessions)
        self.log(f"account_id: {self.account_id}")
        run_id = self.opts.run_id or (datetime.now(CST).strftime("%Y%m%dT%H%M") + "-"
                                      + (getattr(self.opts, "source", None) or "win"))
        todo = [s for s in selected if self.needs_export(s)]
        self.log(f"conversations to export: {len(todo)} (skipped {len(selected) - len(todo)})")
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.opts.workers) as pool:
            futures = [pool.submit(self.export_conversation, s, i + 1, len(todo)) for i, s in enumerate(todo)]
            try:
                for future in futures:
                    future.result()
            except KeyboardInterrupt:
                self.stop.set()
                for future in futures:
                    future.cancel()
                self.log("interrupted; finished conversations are saved in state.json")
                raise
        rows = self.write_sessions(all_sessions)
        contacts = self.write_contacts()
        manifest = self.write_manifest(run_id, rows)
        report = self.write_report(manifest, rows, started)
        self.log(f"contacts: {contacts}")
        self.log(report.rstrip())
        return 0 if self.error_count == 0 and not self.stats["conversations_failed"] else 3


# ---------------------------------------------------------------------------
# Package verification (acceptance checks from HANDOFF-WINDOWS.md section 8)
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("schema_version", "conv_id", "talker", "msg_uid", "server_id", "local_id", "local_seq",
                   "sort_seq", "create_time", "is_send", "sender", "local_type", "kind", "text", "raw",
                   "media_ref", "media_sha256", "media_ext", "media_size", "media_mime", "flags")
LEAK_RE = re.compile(r"(?<![\w:.-])[\w:.-]*aeskey[\w:.-]*\s*=\s*[\"'][^\"']+|(?<!\w)(?:access_)?token\s*=\s*[\"']?[^&\"'\s<>]+",
                     re.IGNORECASE)


def verify_package(out, ffprobe=None):
    out = Path(out)
    problems = []
    summary = collections.Counter()

    def bad(message):
        if len(problems) < 50:
            problems.append(message)

    manifest_path = out / "manifest.json"
    manifest = {}
    if not manifest_path.exists():
        bad("manifest.json is missing")
    else:
        raw = manifest_path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            bad("manifest.json has a BOM")
        manifest = json.loads(raw.decode("utf-8-sig"))
        for field in ("schema_version", "source", "run_id", "account_id", "my_wxid_hash", "weflow_version",
                      "exported_at", "full", "counts", "media_prefix", "staging_prefix"):
            if field not in manifest:
                bad(f"manifest.json lacks {field}")
        if manifest.get("my_wxid_hash") and not str(manifest["my_wxid_hash"]).startswith("sha256:"):
            bad("my_wxid_hash is not sha256:<hex>")
    account = manifest.get("account_id") or ""

    media_names = set()
    media_dir = out / "media"
    for entry in sorted(media_dir.iterdir()) if media_dir.exists() else []:
        match = SHA_NAME_RE.match(entry.name)
        if not match:
            bad(f"unexpected file in media/: {entry.name}")
            continue
        digest = hashlib.sha256()
        with open(entry, "rb") as handle:
            head = handle.read(COPY_CHUNK)
            digest.update(head)
            for chunk in iter(lambda: handle.read(COPY_CHUNK), b""):
                digest.update(chunk)
        if digest.hexdigest() != match.group(1):
            bad(f"media hash mismatch: {entry.name}")
        sig = sniff(head[:32])
        ext = match.group(2)
        sniffable = ext in SNIFFABLE_EXTS
        if sniffable and (sig is None or sig[0] != ext):
            bad(f"media content does not look like .{ext}: {entry.name}")
        if match.group(2) == "mp4" and ffprobe:
            summary["videos_checked"] += 1
            if probe_video(ffprobe, entry):
                summary["videos_ok"] += 1
            else:
                bad(f"ffprobe failed: {entry.name}")
        media_names.add(entry.name)
        summary["media_files"] += 1

    session_counts = {}
    sessions_path = out / "sessions.jsonl"
    if not sessions_path.exists():
        bad("sessions.jsonl is missing")
    else:
        for line in iter_jsonl(sessions_path):
            row = json.loads(line)
            session_counts[conv_key(row["talker"])] = to_int(row.get("msg_count"))
    summary["sessions"] = len(session_counts)

    for path in sorted((out / "messages").glob("*.jsonl")):
        key = path.stem
        if key not in session_counts:
            bad(f"{path.name} has no row in sessions.jsonl")
        with open(path, "rb") as handle:
            if handle.read(3) == b"\xef\xbb\xbf":
                bad(f"{path.name} has a BOM")
        seen = set()
        prev = None
        rows = 0
        for line in iter_jsonl(path):
            rec = json.loads(line)
            rows += 1
            missing = [f for f in REQUIRED_FIELDS if f not in rec]
            if missing:
                bad(f"{path.name}: fields missing {missing}")
                break
            if conv_key(rec["talker"]) != key:
                bad(f"{path.name}: talker does not match file name")
            if account and not rec["conv_id"].startswith(account + ":"):
                bad(f"{path.name}: conv_id does not start with account_id")
            if rec["msg_uid"] in seen:
                bad(f"{path.name}: duplicate msg_uid")
            seen.add(rec["msg_uid"])
            order = (rec["sort_seq"], rec["msg_uid"])
            if prev is not None and order < prev:
                bad(f"{path.name}: not sorted by (sort_seq, msg_uid)")
            prev = order
            if rec["server_id"] is not None and not isinstance(rec["server_id"], str):
                bad(f"{path.name}: server_id is not a string")
            if LEAK_RE.search(rec["raw"] or ""):
                bad(f"{path.name}: key or token text left in raw")
            if rec["media_sha256"]:
                if f"{rec['media_sha256']}.{rec['media_ext']}" not in media_names:
                    bad(f"{path.name}: media file missing for a message")
                summary["media_refs"] += 1
        summary["messages"] += rows
        if key in session_counts and session_counts[key] != rows:
            bad(f"{path.name}: sessions.jsonl says {session_counts[key]} messages, file has {rows}")

    counts = manifest.get("counts") or {}
    for field, actual in (("sessions", summary["sessions"]), ("messages", summary["messages"]),
                          ("media_files", summary["media_files"])):
        if counts and counts.get(field) != actual:
            bad(f"manifest counts.{field}={counts.get(field)} but package has {actual}")
    return problems, summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def detect_weflow_version(exe):
    if os.name != "nt" or not exe or not Path(exe).is_file():
        return "unknown"
    try:
        import ctypes
        from ctypes import wintypes
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(exe), None)
        buffer = ctypes.create_string_buffer(size)
        version.GetFileVersionInfoW(str(exe), 0, size, buffer)
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length))
        info = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        return f"{info[2] >> 16}.{info[2] & 0xFFFF}.{info[3] >> 16}"
    except Exception:
        return "unknown"


def build_parser():
    default_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WeFlow" / "WeFlow.exe"
    parser = argparse.ArgumentParser(description="Export WeChat chats from WeFlow into a staging package.")
    parser.add_argument("--out", required=True, help="staging directory, e.g. W:\\wechat-export-win")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="WeFlow API, loopback only")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="ignore state.json and export every conversation")
    mode.add_argument("--incremental", action="store_true",
                      help="refresh finished conversations from one day before their last message")
    mode.add_argument("--since", type=int, default=0, metavar="EPOCH",
                      help="export only messages created at or after this Unix time (an incremental package)")
    parser.add_argument("--conv", action="append", default=[], help="export only this talker (repeatable)")
    parser.add_argument("--limit", type=int, default=0, help="smoke test: latest N messages per conversation")
    parser.add_argument("--limit-convs", type=int, default=0, help="only the first N conversations")
    media = parser.add_mutually_exclusive_group()
    media.add_argument("--no-media", action="store_true", help="export text only")
    media.add_argument("--media-only", action="store_true",
                       help="re-export conversations whose JSONL still has media without a file")
    parser.add_argument("--workers", type=int, default=2, help="conversations exported in parallel (1-4)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--account-id", help="expected account_id from the Mac side")
    parser.add_argument("--run-id")
    parser.add_argument("--weflow-version", help="default: read from WeFlow.exe")
    parser.add_argument("--weflow-exe", default=str(default_exe))
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable, or 'none'")
    parser.add_argument("--min-free-gb", type=float, default=2.0)
    parser.add_argument("--verify", action="store_true", help="check an existing package instead of exporting")
    return parser


def check_loopback(api_base):
    host = urllib.parse.urlparse(api_base).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ExportError("--api-base must be a loopback address; WeFlow's API must not leave this machine")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    opts = build_parser().parse_args(argv)
    try:
        if opts.verify:
            ffprobe = None if opts.ffprobe.lower() == "none" else shutil.which(opts.ffprobe)
            problems, summary = verify_package(opts.out, ffprobe)
            print(json.dumps(dict(summary), ensure_ascii=False))
            for problem in problems:
                print("PROBLEM:", problem)
            print("OK" if not problems else f"{len(problems)} problem(s)")
            return 0 if not problems else 4
        check_loopback(opts.api_base)
        opts.workers = max(1, min(4, opts.workers))
        opts.page_size = max(1, opts.page_size)
        if not opts.weflow_version:
            opts.weflow_version = detect_weflow_version(opts.weflow_exe)
        return Exporter(opts).run()
    except ExportError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

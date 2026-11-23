"""Message/conversation normalization and identity rules.

Pure functions only; no cloud SDK imports here so tests run without credentials.
"""

from __future__ import annotations

import hashlib
import html
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

RAW_LIMIT = 256 * 1024

# localType -> normalized kind. Composite values (>= 2^32) are app messages.
KIND_BY_TYPE = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    48: "location",
    42: "card",
    10000: "system",
    49: "app",
}

_KIND_BY_EXT = {
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "gif": "image",
    "webp": "image",
    "bmp": "image",
    "heic": "image",
    "mp4": "video",
    "mov": "video",
    "m4v": "video",
    "avi": "video",
    "mkv": "video",
    "silk": "voice",
    "amr": "voice",
    "mp3": "voice",
    "wav": "voice",
    "pdf": "file",
    "doc": "file",
    "docx": "file",
    "xls": "file",
    "xlsx": "file",
    "ppt": "file",
    "pptx": "file",
    "zip": "file",
    "rar": "file",
    "7z": "file",
}


def as_int(value: Any, default: int = 0) -> int:
    """Parse ints coming from JSON/JS-ish sources, tolerating '123.0'/'123'."""
    if value is None or value == "":
        return default
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return default
            return int(float(text)) if "." in text else int(text)
        return int(value)
    except (TypeError, ValueError):
        return default


def kind_for(local_type: int) -> str:
    if local_type in KIND_BY_TYPE:
        return KIND_BY_TYPE[local_type]
    if local_type >= 0x100000000:
        return "app"
    return "other"


def kind_for_ext(ext: str | None) -> str:
    return _KIND_BY_EXT.get((ext or "").lower().lstrip("."), "file")


def conv_key(talker: str) -> str:
    """Filesystem-safe key used by the Windows staging package."""
    return hashlib.sha1(talker.encode("utf-8")).hexdigest()[:16]


def msg_uid(
    *,
    server_id: str | int,
    talker: str,
    create_time: int,
    is_send: bool,
    local_type: int,
    content: str,
    sender: str,
) -> str:
    """Stable message identity across Mac/Windows.

    serverId is the server-side message id; localId is per-device and MUST NOT
    be used. For messages without serverId, fall back to a content fingerprint.
    """
    sid = str(server_id or "0").split(".")[0].strip()
    if sid and sid not in {"0", "None", "null"}:
        return f"s{sid}"
    fingerprint = "|".join(
        [
            talker,
            str(create_time),
            "1" if is_send else "0",
            str(local_type),
            content or "",
            sender or "",
        ]
    )
    return "h" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:20]


def sort_seq_for(create_time: int, source_sort_seq: Any) -> int:
    value = as_int(source_sort_seq, 0)
    if value > 0:
        return value
    return int(create_time) * 1000


_SECRET_ATTR = re.compile(
    r"(?i)\b(aeskey|cdnthumbaeskey|cdnbigimgaeskey|sessionid|access_token|token)"
    r"\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)"
)
_SECRET_QS = re.compile(r"(?i)\b(access_token|token)=[^&\s\"'<>]+")
_TITLE = re.compile(r"<title>(?:\s*<!\[CDATA\[)?(.*?)(?:\]\]>\s*)?</title>", re.DOTALL)


def sanitize_raw(raw: str) -> tuple[str, bool]:
    """Remove WeChat media keys/token-ish attributes; cap huge app XML."""
    text = _SECRET_ATTR.sub(lambda m: f'{m.group(1)}=""', raw or "")
    text = _SECRET_QS.sub(lambda m: f"{m.group(1)}=", text)
    truncated = len(text.encode("utf-8")) > RAW_LIMIT
    if truncated:
        # Cut on a byte boundary; callers only need a searchable prefix.
        text = text.encode("utf-8")[:RAW_LIMIT].decode("utf-8", errors="ignore")
    return text, truncated


def extract_app_text(raw: str) -> str:
    match = _TITLE.search(raw or "")
    if not match:
        return ""
    title = re.sub(r"<[^>]+>", "", match.group(1))
    return html.unescape(title).strip()


@dataclass(slots=True)
class Message:
    conv_id: str
    talker: str
    msg_uid: str
    server_id: str
    local_id: int
    local_seq: int
    sort_seq: int
    create_time: int
    is_send: bool
    sender: str
    local_type: int
    kind: str
    text: str
    raw: str
    media_ref: str | None = None
    media_sha256: str | None = None
    media_key: str | None = None
    media_ext: str | None = None
    media_size: int | None = None
    media_mime: str | None = None
    media_state: str | None = None
    flags: list[str] = field(default_factory=list)
    src: str = ""
    content_hash: str = ""
    ingested_at: int = 0
    # Transient: never written to JSONL / Tablestore.
    media_url: str | None = None

    @property
    def primary_key(self) -> list[tuple[str, Any]]:
        return [("conv_id", self.conv_id), ("sort_seq", self.sort_seq), ("msg_uid", self.msg_uid)]

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "conv_id": self.conv_id,
            "talker": self.talker,
            "msg_uid": self.msg_uid,
            "server_id": self.server_id,
            "local_id": self.local_id,
            "local_seq": self.local_seq,
            "sort_seq": self.sort_seq,
            "create_time": self.create_time,
            "is_send": self.is_send,
            "sender": self.sender,
            "local_type": self.local_type,
            "kind": self.kind,
            "text": self.text,
            "raw": self.raw,
            "media_ref": self.media_ref,
            "media_sha256": self.media_sha256,
            "media_key": self.media_key,
            "media_ext": self.media_ext,
            "media_size": self.media_size,
            "media_mime": self.media_mime,
            "media_state": self.media_state,
            "flags": list(self.flags),
            "src": self.src,
            "content_hash": self.content_hash,
            "ingested_at": self.ingested_at,
        }

    def to_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "account_id": self.conv_id.split(":", 1)[0],
            "talker": self.talker,
            "sender": self.sender,
            "is_send": bool(self.is_send),
            "kind": self.kind,
            "local_type": self.local_type,
            "text": self.text,
            "raw": self.raw,
            "local_seq": self.local_seq,
            "create_time": self.create_time,
            "src": self.src,
            "content_hash": self.content_hash,
            "ingested_at": self.ingested_at,
            "flags": ",".join(self.flags),
        }
        for name in ("media_ref", "media_sha256", "media_key", "media_ext", "media_size", "media_mime", "media_state"):
            value = getattr(self, name)
            if value is not None:
                attrs[name] = value
        return attrs

    @classmethod
    def from_record(cls, record: Mapping[str, Any], *, default_src: str = "") -> Message:
        talker = str(record.get("talker") or "")
        create_time = as_int(record.get("create_time") or record.get("createTime"), 0)
        local_type = as_int(record.get("local_type") or record.get("localType"), 0)
        is_send = bool(as_int(record.get("is_send") if "is_send" in record else record.get("isSend"), 0))
        content = str(record.get("text") if record.get("text") is not None else record.get("content") or "")
        sender = str(record.get("sender") or record.get("senderUsername") or "")
        server_id = str(record.get("server_id") or record.get("serverId") or "0").split(".")[0]
        uid = str(record.get("msg_uid") or "") or msg_uid(
            server_id=server_id,
            talker=talker,
            create_time=create_time,
            is_send=is_send,
            local_type=local_type,
            content=content,
            sender=sender,
        )
        conv_id = str(record.get("conv_id") or "")
        if not conv_id:
            conv_id = f"{record.get('account_id') or ''}:{talker}"
        raw = str(record.get("raw") if record.get("raw") is not None else record.get("rawContent") or content)
        kind = str(record.get("kind") or kind_for(local_type))
        flags = record.get("flags") or []
        if isinstance(flags, str):
            flags = [part for part in flags.split(",") if part]
        content_hash = str(record.get("content_hash") or "")
        if not content_hash:
            content_hash = hashlib.sha1(
                f"{kind}|{content}|{sender}|{create_time}".encode()
            ).hexdigest()
        return cls(
            conv_id=conv_id,
            talker=talker,
            msg_uid=uid,
            server_id=server_id,
            local_id=as_int(record.get("local_id") or record.get("localId"), 0),
            local_seq=as_int(record.get("local_seq") or record.get("localId"), 0),
            sort_seq=sort_seq_for(create_time, record.get("sort_seq") or record.get("sortSeq")),
            create_time=create_time,
            is_send=is_send,
            sender=sender,
            local_type=local_type,
            kind=kind,
            text=content,
            raw=raw,
            media_ref=record.get("media_ref") or None,
            media_sha256=record.get("media_sha256") or None,
            media_key=record.get("media_key") or None,
            media_ext=record.get("media_ext") or None,
            media_size=as_int(record.get("media_size"), 0) or None,
            media_mime=record.get("media_mime") or None,
            media_state=record.get("media_state") or None,
            flags=list(flags),
            src=str(record.get("src") or default_src),
            content_hash=content_hash,
            ingested_at=as_int(record.get("ingested_at"), 0),
        )


def normalize_message(
    raw: Mapping[str, Any],
    *,
    talker: str,
    account_id: str,
    src: str,
    ingested_at: int | None = None,
) -> Message:
    create_time = as_int(raw.get("createTime"), 0)
    local_type = as_int(raw.get("localType"), 0)
    is_send = bool(as_int(raw.get("isSend"), 0))
    sender = str(raw.get("senderUsername") or "")
    content = str(raw.get("content") or "")
    raw_content = str(raw.get("rawContent") or content)
    kind = kind_for(local_type)

    if kind == "text":
        text = content
    elif kind == "app":
        text = extract_app_text(raw_content) or content or "[链接]"
    else:
        text = content or raw.get("parsedContent") or ""

    sanitized, truncated = sanitize_raw(raw_content)
    flags = ["truncated_raw"] if truncated else []

    server_id = str(raw.get("serverId") or "0").split(".")[0]
    uid = msg_uid(
        server_id=server_id,
        talker=talker,
        create_time=create_time,
        is_send=is_send,
        local_type=local_type,
        content=content,
        sender=sender,
    )
    media_ref = str(raw.get("mediaFileName") or raw.get("fileName") or "")
    media_url = raw.get("mediaUrl")
    if not media_ref and isinstance(media_url, str):
        path = urlsplit(media_url).path
        marker = "/api/v1/media/"
        if marker in path:
            media_ref = path.split(marker, 1)[1]
    content_hash = hashlib.sha1(
        f"{kind}|{text}|{sender}|{create_time}".encode()
    ).hexdigest()
    now = int(time.time()) if ingested_at is None else int(ingested_at)
    return Message(
        conv_id=f"{account_id}:{talker}",
        talker=talker,
        msg_uid=uid,
        server_id=server_id,
        local_id=as_int(raw.get("localId"), 0),
        local_seq=as_int(raw.get("localId"), 0),
        sort_seq=sort_seq_for(create_time, raw.get("sortSeq")),
        create_time=create_time,
        is_send=is_send,
        sender=sender,
        local_type=local_type,
        kind=kind,
        text=text,
        raw=sanitized,
        media_ref=media_ref or None,
        media_url=media_url if isinstance(media_url, str) else None,
        flags=flags,
        src=src,
        content_hash=content_hash,
        ingested_at=now,
    )


@dataclass(slots=True)
class Conversation:
    conv_id: str
    account_id: str
    talker: str
    name: str
    remark: str
    is_group: bool
    member_count: int
    first_ts: int
    last_ts: int
    updated_at: int

    def to_attrs(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "talker": self.talker,
            "name": self.name,
            "remark": self.remark,
            "is_group": bool(self.is_group),
            "member_count": self.member_count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "updated_at": self.updated_at,
        }


def normalize_session(session: Mapping[str, Any], *, account_id: str) -> Conversation:
    talker = ""
    for key in ("username", "userName", "talker", "id"):
        if session.get(key):
            talker = str(session[key])
            break
    name = ""
    for key in ("displayName", "remark", "nickName", "nickname", "name"):
        if session.get(key):
            name = str(session[key])
            break
    is_group = talker.endswith("@chatroom") or session.get("sessionType") == "group"
    last_ts = as_int(session.get("lastTimestamp") or session.get("last_ts"), 0)
    now = int(time.time())
    return Conversation(
        conv_id=f"{account_id}:{talker}",
        account_id=account_id,
        talker=talker,
        name=name or talker,
        remark=str(session.get("remark") or ""),
        is_group=is_group,
        member_count=as_int(session.get("memberCount"), 0),
        first_ts=last_ts,
        last_ts=last_ts,
        updated_at=now,
    )
"""Media validation, hashing and OSS ingestion."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
import subprocess
from pathlib import Path

from .model import Message

_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"RIFF": "webp",  # refined below
    b"%PDF": "pdf",
    b"PK\x03\x04": "zip",
    b"#!SILK_V3": "silk",
    b"#!AMR": "amr",
    b"ID3": "mp3",
    b"OggS": "ogg",
    b"\x1aE\xdf\xa3": "mkv",
}

_VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "webm", "3gp"}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "tif", "tiff"}
_MIME_OVERRIDE = {
    "silk": "audio/silk",
    "webp": "image/webp",
    "heic": "image/heic",
    "m4v": "video/x-m4v",
}


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sniff_ext(path: str | Path) -> str | None:
    path = Path(path)
    with path.open("rb") as fh:
        head = fh.read(32)
    for magic, ext in _MAGIC.items():
        if head.startswith(magic):
            if magic == b"RIFF" and head[8:12] == b"WEBP":
                return "webp"
            if magic == b"RIFF":
                return "wav" if head[8:12] == b"WAVE" else "avi"
            return ext
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"heic", b"heix", b"mif1"}:
            return "heic"
        if brand in {b"avif"}:
            return "avif"
        if brand == b"qt  ":
            return "mov"
        return "mp4"
    return None


def guess_ext(name: str | None) -> str | None:
    if not name:
        return None
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix if re.fullmatch(r"[a-z0-9]{1,8}", suffix) else None


def mime_for(ext: str | None) -> str:
    ext = (ext or "").lower().lstrip(".")
    if ext in _MIME_OVERRIDE:
        return _MIME_OVERRIDE[ext]
    guessed, _ = mimetypes.guess_type(f"x.{ext}")
    return guessed or "application/octet-stream"


def ffprobe_ok(path: str | Path) -> bool | None:
    """True=playable, False=probe failed, None=ffprobe unavailable."""
    binary = shutil.which("ffprobe")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def validate(path: str | Path, kind: str, name: str | None = None) -> tuple[bool, str | None, list[str]]:
    """Return (is_plaintext, ext, flags)."""
    path = Path(path)
    flags: list[str] = []
    magic = sniff_ext(path)
    # Documents are often zip/ole containers (docx, xlsx...); the file name says more than the magic does.
    ext = (guess_ext(name) or magic) if kind == "app" else (magic or guess_ext(name))
    if kind == "image" and magic not in _IMAGE_EXTS:
        return False, ext, ["media_encrypted"]
    elif kind == "video":
        if magic not in _VIDEO_EXTS:
            return False, ext, ["media_encrypted"]
        probe = ffprobe_ok(path)
        if probe is False:
            return False, ext, ["media_unverified"]
        if probe is None:
            flags.append("media_unverified")
    elif kind == "voice" and magic not in {"silk", "amr", "mp3", "wav", "m4a", "ogg"}:
        return False, ext, ["media_encrypted"]
    if not ext:
        return False, None, ["media_unverified"]
    return True, ext, flags


def ingest_file(
    *,
    store,
    oss,
    message: Message,
    path: str | Path,
    upload: bool = True,
) -> dict[str, str | int | None]:
    """Hash/validate a local media file, optionally upload, return message fields."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return {"media_state": "missing", "flags": "media_missing"}
    ok, ext, flags = validate(path, message.kind, message.media_ref)
    if not ok:
        state = "encrypted" if "media_encrypted" in flags else "failed"
        return {"media_state": state, "flags": ",".join(flags)}
    sha = sha256_file(path)
    size = path.stat().st_size
    key = oss.key(sha, ext)
    if upload and not oss.exists(key):
        oss.put_file(path, key, mime_for(ext), sha)
    fields: dict[str, str | int | None] = {
        "media_sha256": sha,
        "media_key": key,
        "media_ext": ext,
        "media_size": size,
        "media_mime": mime_for(ext),
        "media_state": "done",
    }
    if flags:
        fields["flags"] = ",".join(flags)
    store.update_message_media(message, fields)
    return fields
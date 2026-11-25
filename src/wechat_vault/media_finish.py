"""Convert decoded image containers, archive voice blobs, and link media to messages."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config
from .local_media import decode_image, image_ext, infer_xor
from .model import sanitize_raw
from .store.oss import ObjectStore
from .store.tablestore import TableStore


def convert_wxgf(data: bytes) -> bytes:
    import av

    if not data.startswith(b"wxgf"):
        raise ValueError("not a decoded image container")
    starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data[4:]))
    units = []
    for i, match in enumerate(starts):
        begin = 4 + match.end()
        end = 4 + starts[i + 1].start() if i + 1 < len(starts) else len(data)
        unit = data[begin:end]
        if len(unit) >= 2 and not unit[0] & 128:
            units.append(unit)
    groups, current = [], []
    for unit in units:
        if (unit[0] >> 1) & 63 == 32 and current:
            groups.append(current)
            current = []
        current.append(unit)
    if current:
        groups.append(current)
    groups.sort(key=lambda group: sum(map(len, group)), reverse=True)
    for group in groups:
        stream = b"".join(b"\x00\x00\x00\x01" + u for u in group)
        try:
            codec = av.CodecContext.create("hevc", "r")
            codec.thread_count = 1
            frames = []
            for packet in codec.parse(stream):
                frames.extend(codec.decode(packet))
            for packet in codec.parse(b""):
                frames.extend(codec.decode(packet))
            frames.extend(codec.decode(None))
            if frames:
                frame = max(frames, key=lambda f: f.width * f.height)
                result = io.BytesIO()
                frame.to_image().save(result, format="JPEG", quality=95)
                return result.getvalue()
        except (ValueError, av.error.FFmpegError):
            continue
    raise ValueError("No decodable HEVC image frame")


def put_bytes(oss, data, ext, mime):
    digest = hashlib.sha256(data).hexdigest()
    key = oss.key(digest, ext)
    existing = getattr(oss, "_existing_media_keys", None)
    if existing is None:
        existing = oss._existing_media_keys = set(oss.list_keys("media/sha256/"))
    if key not in existing:
        for attempt in range(3):
            try:
                result = oss.bucket.put_object(
                    key, data, headers={"Content-Type": mime, "x-oss-meta-sha256": digest}
                )
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        if result.status != 200 or result.crc is None:
            raise ValueError("Object CRC verification unavailable")
        existing.add(key)
    return {"key": key, "sha256": digest, "size": len(data), "ext": ext, "state": "done"}


def convert_index(oss, root, account, device, workers=8):
    prefix = f"manifests/{account}/{device}"
    summary = json.loads(oss.get_bytes(prefix + "/media-latest.json"))
    if summary.get("partial"):
        raise ValueError("Full media archive is still incomplete")
    rows = [json.loads(line) for line in oss.get_bytes(summary["index_key"]).splitlines()]
    oss._existing_media_keys = set(oss.list_keys("media/sha256/"))
    key = os.environ["WECHAT_IMAGE_AES_KEY"].encode()
    xor = infer_xor([root / row["path"] for row in rows], key)
    counts = Counter()

    def one(row):
        try:
            if row.get("state") == "failed":
                path = root / row["path"]
                if path.suffix.lower() == ".dat":
                    recovered, ext = decode_image(path.read_bytes(), key, xor)
                else:
                    recovered = path.read_bytes()
                    ext = path.suffix.lower().lstrip(".") or "bin"
                    if ext == "mp4" and recovered[4:8] != b"ftyp":
                        ext = image_ext(recovered)
                        if not ext:
                            raise ValueError("Unknown video signature")
                mime = "text/plain; charset=utf-8" if ext == "html" else "application/octet-stream"
                row = {**row, **put_bytes(oss, recovered, ext, mime)}
                if ext == "html":
                    row["state"] = "non_image_payload"
                row.pop("error", None)
            if row.get("ext") != "wxgf":
                return row
            # Local files avoid a second download of the decoded container.
            source = root / row["path"]
            if source.exists():
                decoded, _ = decode_image(source.read_bytes(), key, xor)
            else:
                decoded = oss.get_bytes(row["key"])
                if hashlib.sha256(decoded).hexdigest() != row["sha256"]:
                    raise ValueError("Archived image hash mismatch")
            result = put_bytes(oss, convert_wxgf(decoded), "jpg", "image/jpeg")
            return {**row, **result, "original_key": row["key"]}
        except Exception as exc:
            return {**row, "conversion_error": type(exc).__name__}

    with tempfile.TemporaryDirectory(prefix="wechat-media-final-") as directory:
        index = Path(directory) / "index.jsonl"
        with index.open("w") as f, ThreadPoolExecutor(max_workers=workers) as pool:
            for i, row in enumerate(pool.map(one, rows), 1):
                counts["conversion_failed" if row.get("conversion_error") else row["state"]] += 1
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                if i % 1000 == 0:
                    print(f"[convert] {i}/{len(rows)} {dict(counts)}", flush=True)
        digest = hashlib.sha256(index.read_bytes()).hexdigest()
        obj = prefix + f"/media-viewable-{digest}.jsonl"
        oss.put_file(index, obj, "application/x-ndjson", digest)
        summary.update({"index_key": obj, "counts": dict(counts)})
        oss.bucket.put_object(
            prefix + "/media-viewable.json",
            json.dumps(summary).encode(),
            headers={"Content-Type": "application/json"},
        )
    return summary


def voice_index(oss, root):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
    import wcdb_snapshot as ws

    key = bytes.fromhex(os.environ["WECHAT_DB_KEY"])
    result = {}
    oss._existing_media_keys = set(oss.list_keys("media/sha256/"))
    tasks = []
    for p in sorted((root / "db_storage/message").glob("media_*.db")):
        con, _ = ws.open_memory(p, key)
        names = dict(con.execute("select rowid,user_name from Name2Id"))
        for chat, server, data in con.execute(
            "select chat_name_id,svr_id,voice_data from VoiceInfo"
        ):
            if data and server and chat in names:
                tasks.append((names[chat], str(server), bytes(data)))
        con.close()

    def upload(task):
        talker, server, data = task
        if data.startswith((b"\x02#!SILK_V3", b"#!SILK_V3")):
            ext, mime = "silk", "audio/silk"
        elif data.startswith(b"#!AMR"):
            ext, mime = "amr", "audio/amr"
        else:
            ext, mime = "bin", "application/octet-stream"
        row = put_bytes(oss, data, ext, mime)
        if ext == "bin":
            row["state"] = "unsupported_voice_container"
        return (talker, "s" + server), row

    with ThreadPoolExecutor(max_workers=48) as pool:
        for i, (identity, row) in enumerate(pool.map(upload, tasks), 1):
            result[identity] = row
            if i % 1000 == 0:
                print(f"[voice] {i}/{len(tasks)}", flush=True)
    print(f"[voice] archived={len(result)}", flush=True)
    return result


def link(oss, store, root, account, summary, voices):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
    import wcdb_snapshot as ws

    files = defaultdict(list)
    for line in oss.get_bytes(summary["index_key"]).splitlines():
        row = json.loads(line)
        if row.get("key"):
            files[Path(row["path"]).name].append(row)
    by_md5 = defaultdict(list)
    con, _ = ws.open_memory(
        root / "db_storage/hardlink/hardlink.db", bytes.fromhex(os.environ["WECHAT_DB_KEY"])
    )
    directory_names = dict(con.execute("select rowid,username from dir2id"))
    for table in ("image_hardlink_info_v4", "video_hardlink_info_v4", "file_hardlink_info_v4"):
        for md5, filename, dir1, dir2 in con.execute(
            f"select md5,file_name,dir1,dir2 from {table}"
        ):
            names = {filename}
            path = Path(filename)
            if table == "image_hardlink_info_v4":
                for suffix in ("h", "t", "b", "c", "w", "l"):
                    names.add(path.stem + "_" + suffix + path.suffix)
                    names.add(path.stem + "." + suffix + path.suffix)
            elif table == "video_hardlink_info_v4":
                names.update({path.stem + "_thumb.jpg", path.stem + ".jpg"})
            matches = [entry for name in names for entry in files.get(name, [])]
            required = {directory_names[i] for i in (dir1, dir2) if i in directory_names}
            scoped = [row for row in matches if required.issubset(set(Path(row["path"]).parts))]
            if scoped:
                by_md5[md5].extend(scoped)
            elif not required and len({row["sha256"] for row in matches}) == 1:
                by_md5[md5].extend(matches)
    con.close()
    counts = Counter()

    def changes_for(row):
        changes = {}
        for column in ("text", "raw"):
            cleaned, _ = sanitize_raw(str(row.get(column) or ""))
            if cleaned != row.get(column, ""):
                changes[column] = cleaned
        if row.get("kind") in ("image", "video", "voice", "file", "app", "emoji"):
            candidates = []
            voice = voices.get((row.get("talker"), row["msg_uid"]))
            if voice:
                candidates.append(voice)
            for md5 in set(re.findall(r"\b[0-9a-fA-F]{32}\b", str(row.get("raw") or ""))):
                candidates.extend(by_md5.get(md5.lower(), []))
            if candidates:

                def media_rank(candidate):
                    ext = candidate.get("ext")
                    video_mismatch = row.get("kind") == "video" and ext not in {
                        "mp4",
                        "mov",
                        "m4v",
                        "avi",
                        "mkv",
                    }
                    image_mismatch = row.get("kind") in {"image", "emoji"} and ext not in {
                        "jpg",
                        "jpeg",
                        "png",
                        "gif",
                        "webp",
                        "bmp",
                        "heic",
                        "wxgf",
                    }
                    thumbnail = Path(candidate.get("path", "")).stem.endswith(
                        ("_t", ".t", "_thumb")
                    )
                    return (
                        video_mismatch,
                        image_mismatch,
                        thumbnail,
                        ext == "wxgf",
                        -candidate.get("size", 0),
                    )

                candidates.sort(key=media_rank)
                media = candidates[0]
                thumbnail = Path(media.get("path", "")).stem.endswith(("_t", ".t", "_thumb"))
                media_state = media["state"]
                if thumbnail or (
                    row.get("kind") == "video"
                    and media.get("ext") not in {"mp4", "mov", "m4v", "avi", "mkv"}
                ):
                    media_state = "thumbnail_only"
                changes.update(
                    {
                        "media_key": media["key"],
                        "media_sha256": media["sha256"],
                        "media_size": media["size"],
                        "media_ext": media["ext"],
                        "media_state": media_state,
                    }
                )
            elif not row.get("media_key"):
                expects_media = row.get("kind") != "app" or bool(
                    re.search(r"<type>\s*6\s*</type>", str(row.get("raw") or ""))
                    or (int(row.get("local_type") or 0) >> 32) == 6
                )
                if expects_media:
                    changes["media_state"] = "missing"
        return changes

    pending = deque()
    batch = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        for row in store.scan("wx_messages", limit=5000):
            if str(row.get("conv_id") or "").split(":", 1)[0] != account:
                continue
            counts["source_" + str(row.get("src") or "unknown")] += 1
            changes = changes_for(row)
            counts["scanned"] += 1
            counts["linked"] += bool(changes.get("media_key"))
            counts["updated"] += bool(changes)
            if changes:
                pk = [(key, row[key]) for key in ("conv_id", "sort_seq", "msg_uid")]
                batch.append((pk, changes))
                if len(batch) >= 200:
                    pending.append(pool.submit(store.update_message_fields_batch, batch))
                    batch = []
                if len(pending) >= 48:
                    pending.popleft().result()
            if counts["scanned"] % 25000 == 0:
                print(f"[link] {dict(counts)}", flush=True)
        if batch:
            pending.append(pool.submit(store.update_message_fields_batch, batch))
        for future in pending:
            future.result()

    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--skip-conversion", action="store_true")
    parser.add_argument("--skip-voice", action="store_true")
    parser.add_argument("--only-voice", action="store_true")
    args = parser.parse_args()
    oss, store = ObjectStore(Config.from_env()), TableStore(Config.from_env())
    prefix = f"manifests/{args.account}/{args.device}"
    if args.only_voice:
        voices = voice_index(oss, args.account_root)
        payload = [
            {"talker": identity[0], "msg_uid": identity[1], **row}
            for identity, row in voices.items()
        ]
        oss.bucket.put_object(
            prefix + "/voice-index.json",
            json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        print("VOICE_ARCHIVE_COMPLETE", len(voices), flush=True)
        return
    summary = (
        json.loads(oss.get_bytes(prefix + "/media-viewable.json"))
        if args.skip_conversion
        else convert_index(oss, args.account_root, args.account, args.device)
    )
    voices = (
        {
            (row["talker"], row["msg_uid"]): row
            for row in json.loads(oss.get_bytes(prefix + "/voice-index.json"))
        }
        if args.skip_voice
        else voice_index(oss, args.account_root)
    )
    result = link(
        oss,
        store,
        args.account_root,
        "a" + hashlib.sha1(args.account.encode()).hexdigest()[:12],
        summary,
        voices,
    )
    result.update({"voices": len(voices), "media": summary["counts"]})
    oss.bucket.put_object(
        prefix + "/linked-latest.json",
        json.dumps(result).encode(),
        headers={"Content-Type": "application/json"},
    )
    print("MEDIA_FINISH_COMPLETE", json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

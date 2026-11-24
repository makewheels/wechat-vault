"""Import a Windows staging package into Tablestore (messages) + OSS (media)."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .media import mime_for, sha256_file, validate
from .model import Conversation, Message
from .sources.staging import StagingPackage
from .store.oss import ObjectStore
from .store.tablestore import TableStore


def import_staging(
    package: StagingPackage,
    store: TableStore | None,
    oss: ObjectStore | None,
    *,
    config: Config,
    limit: int | None = None,
    no_media: bool = False,
    dry_run: bool = False,
    workers: int = 1,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    manifest = package.manifest
    source = str(manifest.get("source") or "win")
    account_id = package.account_id

    if not dry_run:
        assert store is not None
        store.put_account(account_id, my_wxid="", name="", source=source)

    sessions: dict[str, Conversation] = {}
    for conversation in package.sessions():
        sessions[conversation.conv_id] = conversation
        if not dry_run:
            assert store is not None
            store.upsert_conversation(conversation)

    first_ts: dict[str, int] = {}
    last_ts: dict[str, int] = {}
    max_sort: dict[str, int] = {}
    counts: dict[str, int] = defaultdict(int)
    stats = {
        "sessions": len(sessions),
        "messages": 0,
        "written": 0,
        "duplicates": 0,
        "media_uploaded": 0,
        "media_present": 0,
        "media_missing": 0,
        "media_invalid": 0,
        "errors": 0,
    }

    workers = max(workers, 1)
    pool = ThreadPoolExecutor(max_workers=workers) if not dry_run else None
    pending: deque = deque()

    def drain(keep: int) -> None:
        while len(pending) > max(keep, 0):
            done_message, future = pending.popleft()
            try:
                inserted = future.result()
            except Exception as exc:  # keep going; one bad row must not abort a 1M-row import
                stats["errors"] += 1
                log(f"[import] message failed {done_message.conv_id} {done_message.msg_uid}: {exc}")
                continue
            stats["written" if inserted else "duplicates"] += 1

    for message in package.messages(limit=limit):
        conv_id = message.conv_id
        first_ts[conv_id] = min(first_ts.get(conv_id, message.create_time), message.create_time)
        last_ts[conv_id] = max(last_ts.get(conv_id, message.create_time), message.create_time)
        max_sort[conv_id] = max(max_sort.get(conv_id, 0), message.sort_seq)
        counts[conv_id] += 1
        stats["messages"] += 1

        if dry_run:
            continue
        assert store is not None
        if not no_media:
            assert oss is not None
        try:
            _prepare_media(message, package, oss, stats, no_media=no_media, log=log)
        except Exception as exc:
            stats["errors"] += 1
            _add_flag(message, "media_failed")
            log(f"[media] failed {message.conv_id} {message.msg_uid}: {exc}")
        if message.media_ref and not message.media_sha256 and not no_media:
            store.put_media_task(message, message.media_ref)
        pending.append((message, pool.submit(store.put_message, message)))
        if len(pending) >= workers * 8:
            drain(workers * 4)
        if stats["messages"] % 5000 == 0:
            log(f"[import] {stats['messages']} messages, {stats['written']} written")

    drain(0)
    if pool is not None:
        pool.shutdown()

    if not dry_run:
        assert store is not None
        for conv_id, conversation in sessions.items():
            conversation.first_ts = first_ts.get(conv_id, conversation.first_ts)
            conversation.last_ts = last_ts.get(conv_id, conversation.last_ts)
            store.upsert_conversation(conversation)
            store.put_sync_state(
                source_id=source,
                conv_id=conv_id,
                last_sort_seq=max_sort.get(conv_id, 0),
                last_create_time=last_ts.get(conv_id, 0),
                msgs_written=counts.get(conv_id, 0),
            )
    return stats


def _prepare_media(
    message: Message,
    package: StagingPackage,
    oss: ObjectStore,
    stats: dict[str, int],
    *,
    no_media: bool,
    log: Callable[[str], None],
) -> None:
    if not message.media_sha256:
        return
    sha = message.media_sha256
    ext = message.media_ext
    local = package.media_path(sha, ext)
    if not ext and local is not None:
        ext = local.suffix.lstrip(".") or None
        message.media_ext = ext
    if no_media:
        message.media_state = "pending"
        return
    if local is not None:
        actual = sha256_file(local)
        if actual != sha:
            message.media_state = "failed"
            _add_flag(message, "media_hash_mismatch")
            stats["media_invalid"] += 1
            log(f"[media] hash mismatch {message.conv_id} {message.msg_uid}")
            return
        ok, detected_ext, flags = validate(local, message.kind, message.media_ref)
        if not ok:
            message.media_state = "encrypted" if "media_encrypted" in flags else "failed"
            for flag in flags:
                _add_flag(message, flag)
            stats["media_invalid"] += 1
            log(f"[media] invalid {message.conv_id} {message.msg_uid}: {flags}")
            return
        ext = detected_ext or ext
        if not ext:
            message.media_state = "missing"
            _add_flag(message, "media_missing")
            stats["media_missing"] += 1
            return
        message.media_ext = ext
    if not ext:
        message.media_state = "missing"
        _add_flag(message, "media_missing")
        stats["media_missing"] += 1
        return

    key = oss.key(sha, ext)
    message.media_key = key
    message.media_mime = message.media_mime or mime_for(ext)
    if oss.exists(key):
        message.media_state = "done"
        size = oss.size(key)
        if size:
            message.media_size = size
        stats["media_present"] += 1
        return
    if local is None:
        message.media_state = "missing"
        _add_flag(message, "media_missing")
        stats["media_missing"] += 1
        return
    oss.put_file(local, key, mime_for(ext), sha)
    message.media_sha256 = sha
    message.media_key = key
    message.media_size = local.stat().st_size
    message.media_state = "done"
    for flag in flags:
        _add_flag(message, flag)
    stats["media_uploaded"] += 1


def _add_flag(message: Message, flag: str) -> None:
    if flag not in message.flags:
        message.flags.append(flag)
"""Mac-side sync from WeFlow and media task processing."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .media import ingest_file
from .model import Message, normalize_message, normalize_session
from .sources.weflow import WeFlowClient
from .store.oss import ObjectStore
from .store.tablestore import TableStore


def infer_account(client: WeFlowClient, sessions: list[dict]) -> tuple[str, str] | None:
    """Return (my_wxid, account_id) by looking for an outgoing message."""
    for session in sessions[:200]:
        talker = str(session.get("username") or "")
        if not talker:
            continue
        try:
            page, _ = client.messages(talker, limit=20, media=False)
        except Exception as exc:
            # One unreadable session must not abort account inference.
            if "exc" in repr(exc):
                continue
            continue
        for raw in page:
            if int(raw.get("isSend") or 0) == 1 and raw.get("senderUsername"):
                my_wxid = str(raw["senderUsername"])
                return my_wxid, "a" + hashlib.sha1(my_wxid.encode("utf-8")).hexdigest()[:12]
    return None


def sync_mac(
    config: Config,
    store: TableStore,
    oss: ObjectStore,
    *,
    full: bool = False,
    include_media: bool = False,
    limit_convs: int | None = None,
    talker: str | None = None,
    my_wxid: str | None = None,
    page_size: int = 200,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    config.require_weflow()
    client = WeFlowClient(config.weflow_api, config.weflow_token)
    if not client.health():
        raise RuntimeError(f"WeFlow 不可用: {config.weflow_api}（先打开 WeFlow）")

    sessions = client.sessions(limit=5000)
    if talker:
        sessions = [s for s in sessions if str(s.get("username") or "") == talker]
    if limit_convs:
        sessions = sessions[:limit_convs]
    if not sessions:
        return {"sessions": 0, "messages": 0, "written": 0, "duplicates": 0, "media": 0, "errors": 0}

    if my_wxid:
        account = (my_wxid, "a" + hashlib.sha1(my_wxid.encode("utf-8")).hexdigest()[:12])
    else:
        account = infer_account(client, sessions)
    if not account:
        raise RuntimeError("无法从消息推断 my_wxid；用 --my-wxid 指定，或确认 WeFlow 能返回 isSend=1 的消息")
    my_wxid, account_id = account
    store.put_account(account_id, my_wxid=my_wxid, name="", source=config.source)
    log(f"[sync] account={account_id} sessions={len(sessions)} source={config.source}")

    stats = {"sessions": 0, "messages": 0, "written": 0, "duplicates": 0, "media": 0, "errors": 0}
    for session in sessions:
        conversation = normalize_session(session, account_id=account_id)
        if not conversation.talker:
            continue
        state = store.get_sync_state(config.source, conversation.conv_id)
        watermark = int((state or {}).get("last_sort_seq") or 0)
        if not full and state and watermark and int(session.get("lastTimestamp") or 0) <= int(
            state.get("last_create_time") or 0
        ):
            log(f"[sync] skip unchanged {conversation.name}")
            continue

        store.upsert_conversation(conversation)
        first_ts = conversation.last_ts
        last_ts = conversation.last_ts
        max_sort = watermark
        written = 0
        seen = 0
        try:
            for page in client.iter_messages(conversation.talker, page_size=page_size, media=include_media):
                page_max = 0
                for raw in page:
                    message = normalize_message(
                        raw,
                        talker=conversation.talker,
                        account_id=account_id,
                        src=config.source,
                    )
                    seen += 1
                    max_sort = max(max_sort, message.sort_seq)
                    first_ts = min(first_ts, message.create_time) if first_ts else message.create_time
                    last_ts = max(last_ts, message.create_time)
                    if not full and watermark and message.sort_seq <= watermark:
                        continue
                    if store.put_message(message):
                        written += 1
                    else:
                        stats["duplicates"] += 1
                    if include_media and (message.media_url or message.media_ref):
                        if _sync_one_media(client, store, oss, message, stats, log):
                            stats["media"] += 1
                    elif message.media_ref:
                        store.put_media_task(message, message.media_ref)
                page_max = max((int(m.get("sortSeq") or 0) for m in page), default=0)
                if not full and watermark and page_max and page_max <= watermark:
                    break
        except Exception as exc:
            stats["errors"] += 1
            log(f"[sync] conversation failed {conversation.name}: {exc}")
            store.put_sync_state(
                source_id=config.source,
                conv_id=conversation.conv_id,
                last_sort_seq=max_sort,
                last_create_time=last_ts,
                msgs_written=written,
                status="error",
                last_error=str(exc),
            )
            continue

        conversation.first_ts = first_ts or conversation.first_ts
        conversation.last_ts = last_ts or conversation.last_ts
        store.upsert_conversation(conversation)
        store.put_sync_state(
            source_id=config.source,
            conv_id=conversation.conv_id,
            last_sort_seq=max_sort,
            last_create_time=last_ts,
            msgs_written=written,
        )
        stats["sessions"] += 1
        stats["messages"] += seen
        stats["written"] += written
        log(f"[sync] {conversation.name}: {seen} seen, {written} new")
    return stats


def _sync_one_media(
    client: WeFlowClient,
    store: TableStore,
    oss: ObjectStore,
    message: Message,
    stats: dict[str, int],
    log: Callable[[str], None],
) -> bool:
    with tempfile.TemporaryDirectory(prefix="wechat-media-") as tmp:
        dest = Path(tmp) / "media.bin"
        ok = client.download_media(
            {"mediaUrl": message.media_url, "mediaFileName": message.media_ref}, dest
        )
        if not ok:
            _flag(message, "media_missing")
            message.media_state = "missing"
            store.update_message_media(message, {"media_state": "missing", "flags": "media_missing"})
            stats["errors"] += 1
            log(f"[media] download failed {message.conv_id} {message.msg_uid}")
            return False
        fields = ingest_file(store=store, oss=oss, message=message, path=dest)
        if fields.get("media_state") != "done":
            stats["errors"] += 1
            log(f"[media] invalid {message.conv_id} {message.msg_uid}: {fields}")
            return False
        return True


def process_media_tasks(
    config: Config,
    store: TableStore,
    oss: ObjectStore,
    *,
    limit: int = 100,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    config.require_weflow()
    client = WeFlowClient(config.weflow_api, config.weflow_token)
    stats = {"done": 0, "failed": 0, "skipped": 0}
    processed = 0
    for task in store.scan("wx_media_tasks", columns=None, limit=200):
        if str(task.get("state")) not in {"pending", "failed"}:
            continue
        if int(task.get("attempts") or 0) >= 5:
            continue
        if processed >= limit:
            break
        processed += 1
        message = store.get_message(
            str(task.get("conv_id") or ""),
            int(task.get("sort_seq") or 0),
            str(task.get("msg_uid") or ""),
        )
        if not message:
            store.update_media_task(str(task["msg_uid"]), state="skipped", attempts=99, last_error="message missing")
            stats["skipped"] += 1
            continue
        with tempfile.TemporaryDirectory(prefix="wechat-media-") as tmp:
            dest = Path(tmp) / "media.bin"
            ok = client.download_media({"mediaFileName": task.get("media_ref")}, dest)
            if not ok:
                store.update_media_task(
                    str(task["msg_uid"]),
                    state="failed",
                    attempts=int(task.get("attempts") or 0) + 1,
                    last_error="download failed",
                )
                stats["failed"] += 1
                continue
            fields = ingest_file(store=store, oss=oss, message=Message.from_record(message), path=dest)
            if fields.get("media_state") == "done":
                store.update_media_task(str(task["msg_uid"]), state="done", attempts=0, last_error="")
                stats["done"] += 1
            else:
                store.update_media_task(
                    str(task["msg_uid"]),
                    state="failed",
                    attempts=int(task.get("attempts") or 0) + 1,
                    last_error=str(fields.get("flags") or fields.get("media_state")),
                )
                stats["failed"] += 1
    log(f"[media] {stats}")
    return stats


def _flag(message: Message, flag: str) -> None:
    if flag not in message.flags:
        message.flags.append(flag)
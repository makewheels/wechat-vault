"""Finish an active Mac archive after both message and media imports commit their summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config
from .dedupe import maintenance_client, repair
from .media_finish import convert_index, link, put_bytes
from .model import sanitize_raw
from .store.oss import ObjectStore
from .store.tablestore import TableStore, _retry_request
from .verify import counts, dup_msg_uid, media_stats


def reconcile_tasks(store, oss):
    from tablestore import Condition, Row, RowExistenceExpectation

    tasks = list(store.scan("wx_media_tasks", limit=5000))

    def one(task):
        message = store.get_message(
            str(task.get("conv_id") or ""), int(task.get("sort_seq") or 0), str(task["msg_uid"])
        )
        state, error = "orphaned", "Source message is not present in the archive"
        if message:
            if message.get("media_key") and oss.exists(message["media_key"]):
                state, error = "done", ""
            else:
                state, error = "missing", "No locally available media in this snapshot"
        row = Row(
            [("msg_uid", task["msg_uid"])],
            {"PUT": [("state", state), ("last_error", error), ("updated_at", int(time.time()))]},
        )
        _retry_request(
            lambda: store.client.update_row(
                "wx_media_tasks", row, Condition(RowExistenceExpectation.EXPECT_EXIST)
            )
        )
        return state

    with ThreadPoolExecutor(max_workers=16) as pool:
        states = Counter(pool.map(one, tasks))
    print("[tasks] reconciled", dict(states), flush=True)
    return dict(states)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--maintenance-profile")
    args = parser.parse_args()
    config = Config.from_env()
    oss, store = ObjectStore(config), TableStore(config)
    prefix = f"manifests/{args.account}/{args.device}"
    message_key = f"manifests/{args.account_root.name}/messages-latest.json"
    required = [prefix + "/media-latest.json", prefix + "/voice-index.json"]
    # Do not finalize against a partial smoke-test manifest.
    while True:
        ready = all(oss.exists(key) for key in required)
        if ready:
            manifest = json.loads(oss.get_bytes(required[0]))
            ready = not manifest.get("partial", True)
        if ready:
            break
        print("[complete] waiting for full media and voice summaries", flush=True)
        time.sleep(30)
    summary = convert_index(oss, args.account_root, args.account, args.device, workers=32)
    voices = {
        (row["talker"], row["msg_uid"]): row
        for row in json.loads(oss.get_bytes(prefix + "/voice-index.json"))
    }
    for identity, voice in list(voices.items()):
        if voice.get("ext") == "amr":
            data = oss.get_bytes(voice["key"])
            if data.startswith(b"#!AMR\n\x02#!SILK_V3"):
                normalized = put_bytes(oss, data[6:], "silk", "audio/silk")
                voices[identity] = {**voice, **normalized, "original_key": voice["key"]}
    while not oss.exists(message_key):
        print("[complete] media conversion finished; waiting for message import", flush=True)
        time.sleep(30)
    account_key = "a" + hashlib.sha1(args.account.encode()).hexdigest()[:12]
    store.put_account(account_key, my_wxid=args.account, name="", source="mac")
    linked = link(
        oss,
        store,
        args.account_root,
        "a" + hashlib.sha1(args.account.encode()).hexdigest()[:12],
        summary,
        voices,
    )
    if linked.get("source_win"):
        store.put_account(account_key, my_wxid=args.account, name="", source="win")
    task_states = reconcile_tasks(store, oss)
    duplicate_result = repair(
        store,
        oss,
        apply=True,
        delete_client=(
            maintenance_client(config, args.maintenance_profile)
            if args.maintenance_profile
            else None
        ),
    )
    # Historical conflict snippets were written before the body sanitizer was fixed.
    from tablestore import Condition, Row, RowExistenceExpectation

    sanitized_conflicts = 0
    for row in store.scan("wx_conflicts", columns=["existing_text", "incoming_text"], limit=5000):
        changes = {
            key: sanitize_raw(str(row.get(key) or ""))[0]
            for key in ("existing_text", "incoming_text")
        }
        if any(changes[key] != row.get(key, "") for key in changes):
            store.client.update_row(
                "wx_conflicts",
                Row(
                    [("msg_uid", row["msg_uid"]), ("src", row["src"])],
                    {"PUT": list(changes.items())},
                ),
                Condition(RowExistenceExpectation.EXPECT_EXIST),
            )
            sanitized_conflicts += 1
    # Independent read-back scans, after all writes finish.
    with ThreadPoolExecutor(max_workers=3) as pool:
        count_future = pool.submit(counts, store)
        duplicate_future = pool.submit(dup_msg_uid, store)
        media_future = pool.submit(media_stats, store)
        verification = {
            "counts": count_future.result(),
            "duplicate_identities": duplicate_future.result(),
            "media": media_future.result(),
        }
    verification["content_variants"] = sum(
        1 for _ in store.scan("wx_conflicts", columns=[], limit=5000)
    )
    incomplete = (
        summary["counts"].get("failed", 0)
        + summary["counts"].get("conversion_failed", 0)
        + verification["duplicate_identities"]
    )
    result = {
        "status": "partial" if incomplete else "complete",
        "account": args.account,
        "device": args.device,
        "linked": linked,
        "dedupe": duplicate_result,
        "verification": verification,
        "local_media": summary["counts"],
        "voice_records": len(voices),
        "media_tasks": task_states,
        "sanitized_conflicts": sanitized_conflicts,
        "completed_at": int(time.time()),
    }
    oss.bucket.put_object(
        prefix + "/archive-latest.json",
        json.dumps(result).encode(),
        headers={"Content-Type": "application/json"},
    )
    print("ARCHIVE_COMPLETE", json.dumps(result), flush=True)
    return int(bool(incomplete))


if __name__ == "__main__":
    raise SystemExit(main())

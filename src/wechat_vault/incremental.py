"""Incremental sync: export recent messages straight from the local databases and merge them into the cloud.

Run it on a schedule. The start point comes from the cloud watermark (`wx_sync_state`) for this source,
minus a look-back window so late-downloaded media and late messages are still picked up. The merge is
idempotent: rows already stored are skipped, media is matched by sha256, and a row that already links
media (possibly from the other device) is never overwritten.

    python -m wechat_vault.incremental --account-root <...>/xwechat_files/<wxid> --source win
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

LOOKBACK_DAYS = 7


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance(name: str) -> Iterator[None]:
    handle = open(Path(tempfile.gettempdir()) / f"{name}.lock", "a+")  # noqa: SIM115
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(name) from exc
    try:
        yield
    finally:
        handle.close()


def cloud_watermark(store, source: str) -> int:
    """Newest message time this source has imported, or 0 when it never has."""
    latest = 0
    for row in store.scan("wx_sync_state", columns=None, limit=5000):
        if row.get("source_id") == source:
            latest = max(latest, int(row.get("last_create_time") or 0))
    return latest


def choose_since(*, full: bool, since: int, watermark: int, lookback_days: int, now: int) -> int:
    """Start time for the export; 0 means everything."""
    if full:
        return 0
    if since:
        return since
    if not watermark:
        raise ValueError("this source has no sync state in the cloud yet; run once with --full or --since")
    # A clock-skewed message must not push the window past the present.
    return max(min(watermark, now) - lookback_days * 86400, 1)


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def run(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
    import export_snapshot as es

    from .config import Config
    from .import_staging import import_staging
    from .sources.staging import StagingPackage
    from .store.oss import ObjectStore
    from .store.tablestore import TableStore

    config = Config.from_env()
    config.require_cloud()
    store, oss = TableStore(config), ObjectStore(config)
    now = int(time.time())
    since = choose_since(
        full=args.full,
        since=args.since,
        watermark=0 if args.full or args.since else cloud_watermark(store, args.source),
        lookback_days=args.lookback_days,
        now=now,
    )
    log(f"[incremental] source={args.source} since={since or 'full'}")

    with tempfile.TemporaryDirectory(prefix="wechat-incremental-") as directory:
        out = Path(directory) / "staging"
        export = [
            "--db-storage", str(args.account_root / "db_storage"),
            "--out", str(out),
            "--source", args.source,
            "--workers", "4",
            "--ffprobe", "none",
            "--full" if not since else "--since",
        ]
        if since:
            export.append(str(since))
        if args.no_media:
            export.append("--no-media")
        if es.main(export):
            raise RuntimeError("export failed; nothing was imported")
        if es.main(["--out", str(out), "--verify", "--ffprobe", "none"]):
            raise RuntimeError("export verification failed; nothing was imported")
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        log(f"[incremental] exported {manifest['counts']}")

        contacts_path = out / "contacts.jsonl"
        if contacts_path.exists() and not args.dry_run:
            contacts = [json.loads(line) for line in contacts_path.read_text(encoding="utf-8").splitlines()]
            chunks = [contacts[i : i + 100] for i in range(0, len(contacts), 100)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda chunk: store.put_contacts(manifest["account_id"], chunk), chunks))

        result = import_staging(
            StagingPackage(out),
            None if args.dry_run else store,
            None if args.dry_run else oss,
            config=config,
            no_media=args.no_media,
            dry_run=args.dry_run,
            workers=args.workers,
            identities_since=since or None,
            log=log,
        )
    log(f"[incremental] import result {result}")
    if result["errors"]:
        raise RuntimeError(f"{result['errors']} messages failed to import; the watermark was not advanced")
    summary = {
        "source": args.source,
        "since": since,
        "export": manifest["counts"],
        "import": result,
        "completed_at": int(time.time()),
    }
    if not args.dry_run:
        oss.bucket.put_object(
            f"manifests/{manifest['account_id']}/{args.source}/incremental-latest.json",
            json.dumps(summary).encode(),
            headers={"Content-Type": "application/json"},
        )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-root", type=Path, required=True, help="the account folder that holds db_storage")
    parser.add_argument("--source", required=True, choices=["win", "mac"], help="this machine's source label")
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--full", action="store_true", help="export everything (the cloud still dedupes)")
    when.add_argument("--since", type=int, default=0, metavar="EPOCH", help="override the start time")
    parser.add_argument("--no-media", action="store_true", help="messages only")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--dry-run", action="store_true", help="export and verify, write nothing to the cloud")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with single_instance(f"wechat-vault-incremental-{args.source}"):
            run(args)
    except AlreadyRunning:
        log("[incremental] another run is in progress; skipped")
        return 0
    except Exception as exc:
        # SDK exception bodies can carry signed request details; only our own errors are printed in full.
        detail = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else ""
        log(f"[incremental] FAILED: {type(exc).__name__} {detail}".rstrip())
        return 1
    log("[incremental] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

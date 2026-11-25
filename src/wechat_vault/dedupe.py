"""Cross-device duplicate repair with a private OSS backup before deletion."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .config import Config
from .model import sanitize_raw
from .store.oss import ObjectStore
from .store.tablestore import TableStore


def equivalent(left: dict, right: dict) -> bool:
    return all(
        left.get(k) == right.get(k)
        for k in ("conv_id", "msg_uid", "create_time", "sender", "kind", "text", "is_send")
    )


def repair(
    store: TableStore, oss: ObjectStore, *, apply=False, delete_client=None, log=print
) -> dict:
    from tablestore import Condition, RowExistenceExpectation

    counts = {"scanned": 0, "duplicates": 0, "removed": 0, "conflicts": 0}
    groups = defaultdict(list)
    current = None

    def flush():
        for rows in groups.values():
            if len(rows) < 2:
                continue
            counts["duplicates"] += len(rows) - 1
            rows = [store.get_message(r["conv_id"], r["sort_seq"], r["msg_uid"]) for r in rows]
            if any(row is None for row in rows):
                raise RuntimeError("Duplicate rows changed during audit; rerun the audit")
            # Preserve the row with the most useful media data, then stable order.
            rows.sort(key=lambda r: (not bool(r.get("media_key")), int(r["sort_seq"])))
            keep = rows[0]
            for extra in rows[1:]:
                if not equivalent(keep, extra):
                    counts["conflicts"] += 1
                    continue
                if not apply:
                    continue
                backup = {"retained": dict(keep), "removed": dict(extra)}
                for record in backup.values():
                    for column in ("text", "raw"):
                        if column in record:
                            record[column] = sanitize_raw(str(record[column]))[0]
                payload = json.dumps(backup, ensure_ascii=False, sort_keys=True).encode()
                digest = hashlib.sha256(payload).hexdigest()
                key = f"maintenance/dedupe/{digest}.json"
                oss.bucket.put_object(
                    key,
                    payload,
                    headers={"Content-Type": "application/json", "x-oss-meta-sha256": digest},
                )
                # Read back the backup before any destructive operation.
                if hashlib.sha256(oss.get_bytes(key)).hexdigest() != digest:
                    raise RuntimeError("Duplicate backup verification failed")
                pk = [(k, extra[k]) for k in ("conv_id", "sort_seq", "msg_uid")]
                (delete_client or store.client).delete_row(
                    "wx_messages", pk, Condition(RowExistenceExpectation.EXPECT_EXIST)
                )
                counts["removed"] += 1
        groups.clear()

    for row in store.scan("wx_messages", columns=[], limit=5000):
        conv = row["conv_id"]
        if current is not None and conv != current:
            flush()
        current = conv
        groups[str(row["msg_uid"])].append(row)
        counts["scanned"] += 1
        if counts["scanned"] % 50000 == 0:
            log(f"[dedupe] {counts}", flush=True)
    flush()
    log(f"[dedupe] {counts}")
    return counts


def maintenance_client(config: Config, profile_name: str):
    """Use the existing local CLI profile only for this explicit duplicate deletion task."""
    from dataclasses import replace
    from pathlib import Path

    data = json.loads((Path.home() / ".aliyun/config.json").read_text())
    profile = next(p for p in data["profiles"] if p["name"] == profile_name)
    if profile.get("mode") != "AK":
        raise ValueError("Maintenance requires an existing AK CLI profile")
    scoped = replace(config, ak_id=profile["access_key_id"], ak_secret=profile["access_key_secret"])
    return TableStore(scoped).client


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maintenance-profile")
    args = parser.parse_args()
    config = Config.from_env()
    config.require_cloud()
    # Application credentials intentionally retain their no-delete policy.
    client = (
        maintenance_client(config, args.maintenance_profile) if args.maintenance_profile else None
    )
    result = repair(TableStore(config), ObjectStore(config), apply=args.apply, delete_client=client)
    return int(result["conflicts"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())

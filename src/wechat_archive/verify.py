"""Read-only checks for imports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from .store.tablestore import TableStore


def dup_msg_uid(store: TableStore, *, limit: int = 50, log: Callable[[str], None] = print) -> int:
    seen: Counter[str] = Counter()
    examples: dict[str, list[tuple[str, int]]] = {}
    for row in store.scan("wx_messages"):
        uid = str(row.get("msg_uid") or "")
        if not uid:
            continue
        seen[uid] += 1
        if seen[uid] > 1:
            examples.setdefault(uid, []).append((str(row.get("conv_id")), int(row.get("sort_seq") or 0)))
    dupes = {uid: count for uid, count in seen.items() if count > 1}
    log(f"[verify] duplicate msg_uid: {len(dupes)}")
    for uid, count in list(dupes.items())[:limit]:
        log(f"  {uid} x{count} {examples.get(uid, [])[:4]}")
    return len(dupes)


def counts(store: TableStore, *, top: int = 20, log: Callable[[str], None] = print) -> dict[str, int]:
    per_conv: Counter[str] = Counter()
    total = 0
    for row in store.scan("wx_messages"):
        per_conv[str(row.get("conv_id") or "")] += 1
        total += 1
    log(f"[verify] messages={total} conversations={len(per_conv)}")
    for conv_id, count in per_conv.most_common(top):
        log(f"  {count:>7} {conv_id}")
    return {"messages": total, "conversations": len(per_conv)}


def media_stats(store: TableStore, *, log: Callable[[str], None] = print) -> dict[str, int]:
    states: Counter[str] = Counter()
    with_sha = 0
    for row in store.scan("wx_messages"):
        if row.get("media_sha256"):
            with_sha += 1
        states[str(row.get("media_state") or "none")] += 1
    log(f"[verify] media done={with_sha} states={dict(states)}")
    result = dict(states)
    result["with_sha256"] = with_sha
    return result


def table_stats(store: TableStore, *, log: Callable[[str], None] = print) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in ("wx_messages", "wx_conversations", "wx_media_tasks", "wx_conflicts"):
        count = sum(1 for _ in store.scan(table))
        result[table] = count
        log(f"[stats] {table}={count}")
    return result
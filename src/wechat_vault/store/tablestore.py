"""Tablestore access: table init, idempotent message writes, scans."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from ..config import Config
from ..model import Conversation, Message, sanitize_raw

TABLE_SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "wx_accounts": [("account_id", "STRING")],
    "wx_conversations": [("conv_id", "STRING")],
    "wx_messages": [("conv_id", "STRING"), ("sort_seq", "INTEGER"), ("msg_uid", "STRING")],
    "wx_contacts": [("account_id", "STRING"), ("wxid", "STRING")],
    "wx_sync_state": [("source_id", "STRING"), ("conv_id", "STRING")],
    "wx_media_tasks": [("msg_uid", "STRING")],
    "wx_conflicts": [("msg_uid", "STRING"), ("src", "STRING")],
}


def _use_env_proxy(connection: Any, endpoint: str) -> None:
    """The Tablestore SDK talks to urllib3 directly and ignores HTTPS_PROXY/NO_PROXY; honour them."""
    from urllib.parse import urlsplit
    from urllib.request import getproxies, proxy_bypass_environment

    from urllib3 import ProxyManager

    host = urlsplit(endpoint).hostname or ""
    proxy = getproxies().get("https")
    if not proxy or proxy_bypass_environment(host):
        return
    kwargs = dict(connection.pool.connection_pool_kw)
    kwargs["maxsize"] = max(
        int(kwargs.get("maxsize") or 1), 32
    )  # concurrent imports share this pool
    connection.pool = ProxyManager(proxy, num_pools=connection.NUM_POOLS, **kwargs)


def _retry_request(operation):
    import requests
    import urllib3

    transient_codes = {
        "OTSServerBusy",
        "OTSQuotaExhausted",
        "OTSTimeout",
        "OTSOperationRateLimited",
        "OTSInternalServerError",
    }
    for attempt in range(4):
        try:
            return operation()
        except Exception as exc:
            code = exc.get_error_code() if hasattr(exc, "get_error_code") else ""
            transient = (
                isinstance(
                    exc,
                    (
                        requests.RequestException,
                        urllib3.exceptions.HTTPError,
                        TimeoutError,
                        ConnectionError,
                        OSError,
                    ),
                )
                or code in transient_codes
            )
            if not transient or attempt == 3:
                raise
            time.sleep(2**attempt)


def _columns_to_get(columns: list[str] | None, table: str) -> list[str] | None:
    # Tablestore skips rows holding none of the requested columns, so a made-up column hides every row.
    # For "keys only" read a column every row has (messages always carry content_hash), else all columns.
    if columns != []:
        return columns
    return ["content_hash"] if table == "wx_messages" else []


class TableStore:
    def __init__(self, config: Config):
        from tablestore import OTSClient

        self.config = config
        self.client = OTSClient(
            config.endpoint,
            config.ak_id,
            config.ak_secret,
            config.instance,
        )
        _use_env_proxy(self.client.connection, config.endpoint)

    # ---- schema ----

    def init_tables(self) -> list[str]:
        from tablestore import (
            CapacityUnit,
            OTSServiceError,
            ReservedThroughput,
            TableMeta,
            TableOptions,
        )

        created: list[str] = []
        for table, columns in TABLE_SCHEMAS.items():
            meta = TableMeta(table, columns)
            options = TableOptions(time_to_live=-1, max_version=1)
            try:
                self.client.create_table(meta, options, ReservedThroughput(CapacityUnit(0, 0)))
                created.append(table)
            except OTSServiceError as exc:
                if exc.get_error_code() != "OTSTableAlreadyExist":
                    raise
        return created

    # ---- writes ----

    def put_message(self, message: Message, *, record_conflict: bool = True) -> bool:
        from tablestore import Condition, OTSServiceError, Row, RowExistenceExpectation

        row = Row(message.primary_key, list(message.to_attrs().items()))
        try:
            self.client.put_row(
                "wx_messages", row, Condition(RowExistenceExpectation.EXPECT_NOT_EXIST)
            )
            return True
        except OTSServiceError as exc:
            if exc.get_error_code() != "OTSConditionCheckFail":
                raise
            if record_conflict:
                existing = self.get_message(message.conv_id, message.sort_seq, message.msg_uid)
                if existing and existing.get("content_hash") != message.content_hash:
                    self.put_conflict(message, existing)
            return False

    def message_identities(self, account_id: str) -> dict:
        """Fetch the account's identity map in one paginated scan, not one query per chat."""
        result = {}
        count = 0
        for row in self.scan("wx_messages", columns=["content_hash"], limit=5000):
            conv_id = str(row["conv_id"])
            if not conv_id.startswith(account_id + ":"):
                continue
            result.setdefault(conv_id, {})[str(row["msg_uid"])] = (
                int(row["sort_seq"]),
                str(row.get("content_hash") or ""),
            )
            count += 1
            if count % 50000 == 0:
                print(f"[import] existing identities loaded={count}", flush=True)
        print(f"[import] existing identities loaded={count} complete", flush=True)
        return result

    def put_messages(self, messages: list[Message]) -> list[bool]:
        """Batch writes retain conditional insert semantics; retry failed items individually."""
        from tablestore import (
            BatchWriteRowRequest,
            Condition,
            PutRowItem,
            Row,
            RowExistenceExpectation,
            TableInBatchWriteRowItem,
        )

        if (
            len(messages) > 1
            and sum(len(m.text.encode()) + len(m.raw.encode()) for m in messages) > 3 * 1024 * 1024
        ):
            middle = len(messages) // 2
            return self.put_messages(messages[:middle]) + self.put_messages(messages[middle:])
        request = BatchWriteRowRequest()
        items = [
            PutRowItem(
                Row(m.primary_key, list(m.to_attrs().items())),
                Condition(RowExistenceExpectation.EXPECT_NOT_EXIST),
            )
            for m in messages
        ]
        request.add(TableInBatchWriteRowItem("wx_messages", items))
        response = _retry_request(lambda request=request: self.client.batch_write_row(request))
        outcomes = response.get_put_by_table("wx_messages")
        if len(outcomes) != len(messages):
            raise RuntimeError("Incomplete batch response")
        results, variants = [], []
        for message, outcome in zip(messages, outcomes):
            if outcome.is_ok:
                results.append(True)
            elif outcome.error_code == "OTSConditionCheckFail":
                # An ambiguous timeout may already have committed the insert.
                # Retain the incoming version without one read per message.
                results.append(False)
                variants.append((message, ""))
            else:
                results.append(self.put_message(message))
        if variants:
            self.put_message_variants(variants)
        return results

    def put_message_variants(self, variants: list[tuple[Message, str]]) -> list[bool]:
        from tablestore import Row

        rows = []
        size = 0
        for message, existing_hash in variants:
            attrs = [
                ("conv_id", message.conv_id),
                ("existing_sort_seq", message.sort_seq),
                ("existing_hash", existing_hash),
                ("incoming_hash", message.content_hash),
                ("incoming_text", sanitize_raw(message.text)[0][:4000]),
                ("incoming_raw", sanitize_raw(message.raw)[0]),
                ("created_at", int(time.time())),
            ]
            payload_size = sum(len(str(v).encode()) for _, v in attrs)
            if rows and size + payload_size > 2 * 1024 * 1024:
                self._put_rows("wx_conflicts", rows)
                rows, size = [], 0
            rows.append(
                Row([("msg_uid", message.msg_uid), ("src", message.src or "unknown")], attrs)
            )
            size += payload_size
        if rows:
            self._put_rows("wx_conflicts", rows)
        # The canonical message already exists and remains unchanged.
        return [False] * len(variants)

    def put_conflict(self, message: Message, existing: dict[str, Any]) -> None:
        from tablestore import Row

        row = Row(
            [("msg_uid", message.msg_uid), ("src", message.src or "unknown")],
            [
                ("conv_id", message.conv_id),
                ("existing_hash", existing.get("content_hash", "")),
                ("incoming_hash", message.content_hash),
                ("existing_text", sanitize_raw(str(existing.get("text", "")))[0][:4000]),
                ("incoming_text", sanitize_raw(message.text)[0][:4000]),
                ("created_at", message.ingested_at),
            ],
        )
        self.client.put_row("wx_conflicts", row)

    def upsert_conversation(self, conversation: Conversation) -> None:
        from tablestore import Row

        existing = self.get_row("wx_conversations", [("conv_id", conversation.conv_id)])
        if existing:
            first_ts = min(
                int(existing.get("first_ts") or conversation.first_ts),
                conversation.first_ts,
            )
            last_ts = max(
                int(existing.get("last_ts") or conversation.last_ts),
                conversation.last_ts,
            )
            conversation.first_ts = first_ts or conversation.first_ts
            conversation.last_ts = last_ts or conversation.last_ts
        self.client.put_row(
            "wx_conversations",
            Row([("conv_id", conversation.conv_id)], list(conversation.to_attrs().items())),
        )

    def _put_rows(self, table: str, rows: list) -> None:
        from tablestore import (
            BatchWriteRowRequest,
            Condition,
            PutRowItem,
            RowExistenceExpectation,
            TableInBatchWriteRowItem,
        )

        for offset in range(0, len(rows), 100):
            request = BatchWriteRowRequest()
            request.add(
                TableInBatchWriteRowItem(
                    table,
                    [
                        PutRowItem(row, Condition(RowExistenceExpectation.IGNORE))
                        for row in rows[offset : offset + 100]
                    ],
                )
            )
            response = _retry_request(lambda request=request: self.client.batch_write_row(request))
            if any(not row.is_ok for row in response.get_put_by_table(table)):
                raise RuntimeError(f"Batch write failed for {table}")

    def upsert_conversations(self, conversations: list[Conversation]) -> None:
        from tablestore import Row

        existing = {
            r["conv_id"]: r
            for r in self.scan("wx_conversations", columns=["first_ts", "last_ts"], limit=5000)
        }
        rows = []
        for conv in conversations:
            old = existing.get(conv.conv_id, {})
            starts = [t for t in (conv.first_ts, int(old.get("first_ts") or 0)) if t]
            conv.first_ts = min(starts) if starts else 0
            conv.last_ts = max(conv.last_ts, int(old.get("last_ts") or 0))
            rows.append(Row([("conv_id", conv.conv_id)], list(conv.to_attrs().items())))
        self._put_rows("wx_conversations", rows)

    def put_sync_states(self, source_id: str, records: list[dict]) -> None:
        from tablestore import Row

        rows = []
        for record in records:
            attrs = {k: v for k, v in record.items() if k != "conv_id"}
            attrs.update(last_run=int(time.time()), status="done", last_error="")
            rows.append(
                Row([("source_id", source_id), ("conv_id", record["conv_id"])], list(attrs.items()))
            )
        self._put_rows("wx_sync_state", rows)

    def put_sync_state(
        self,
        *,
        source_id: str,
        conv_id: str,
        last_sort_seq: int,
        last_create_time: int,
        msgs_written: int,
        status: str = "done",
        last_error: str = "",
    ) -> None:
        from tablestore import Row

        row = Row(
            [("source_id", source_id), ("conv_id", conv_id)],
            [
                ("last_sort_seq", last_sort_seq),
                ("last_create_time", last_create_time),
                ("last_run", int(time.time())),
                ("status", status),
                ("last_error", last_error[:1000]),
                ("msgs_written", msgs_written),
            ],
        )
        self.client.put_row("wx_sync_state", row)

    def put_account(self, account_id: str, my_wxid: str, name: str, source: str) -> None:
        from tablestore import Row

        existing = self.get_row("wx_accounts", [("account_id", account_id)]) or {}
        sources = set(str(existing.get("sources") or "").split(",")) | {source}
        sources.discard("")
        my_wxid = my_wxid or str(existing.get("my_wxid") or "")
        name = name or str(existing.get("name") or "")
        row = Row(
            [("account_id", account_id)],
            [
                ("my_wxid", my_wxid),
                ("name", name),
                ("last_sync_at", int(time.time())),
                ("sources", ",".join(sorted(sources))),
            ],
        )
        self.client.put_row("wx_accounts", row)

    def put_contact(self, account_id: str, contact: dict[str, Any]) -> None:
        from tablestore import Row

        wxid = str(contact.get("username") or contact.get("wxid") or "")
        if not wxid:
            return
        row = Row(
            [("account_id", account_id), ("wxid", wxid)],
            [
                ("alias", str(contact.get("alias") or "")),
                ("nickname", str(contact.get("nickName") or contact.get("nickname") or "")),
                ("remark", str(contact.get("remark") or "")),
                ("is_friend", bool(contact.get("isFriend", True))),
                ("updated_at", int(time.time())),
            ],
        )
        self.client.put_row("wx_contacts", row)

    def put_contacts(self, account_id: str, contacts: list[dict[str, Any]]) -> None:
        from tablestore import (
            BatchWriteRowRequest,
            Condition,
            PutRowItem,
            Row,
            RowExistenceExpectation,
            TableInBatchWriteRowItem,
        )

        request = BatchWriteRowRequest()
        items = []
        for contact in contacts:
            wxid = str(contact.get("username") or contact.get("wxid") or "")
            if not wxid:
                continue
            attrs = [
                ("alias", str(contact.get("alias") or "")),
                ("nickname", str(contact.get("nickname") or contact.get("nickName") or "")),
                ("remark", str(contact.get("remark") or "")),
                ("is_friend", bool(contact.get("is_friend", contact.get("isFriend", True)))),
                ("updated_at", int(time.time())),
            ]
            items.append(
                PutRowItem(
                    Row([("account_id", account_id), ("wxid", wxid)], attrs),
                    Condition(RowExistenceExpectation.IGNORE),
                )
            )
        if not items:
            return
        request.add(TableInBatchWriteRowItem("wx_contacts", items))
        response = _retry_request(lambda request=request: self.client.batch_write_row(request))
        if any(not row.is_ok for row in response.get_put_by_table("wx_contacts")):
            raise RuntimeError("Some contacts failed to write")

    def put_media_task(self, message: Message, media_ref: str) -> None:
        from tablestore import Row

        row = Row(
            [("msg_uid", message.msg_uid)],
            [
                ("conv_id", message.conv_id),
                ("sort_seq", message.sort_seq),
                ("media_ref", media_ref),
                ("kind", message.kind),
                ("state", "pending"),
                ("attempts", 0),
                ("last_error", ""),
                ("updated_at", message.ingested_at),
            ],
        )
        self.client.put_row("wx_media_tasks", row)

    def update_media_task(
        self, msg_uid: str, *, state: str, attempts: int, last_error: str = ""
    ) -> None:
        from tablestore import Condition, Row, RowExistenceExpectation

        row = Row(
            [("msg_uid", msg_uid)],
            {
                "PUT": [
                    ("state", state),
                    ("attempts", attempts),
                    ("last_error", last_error[:1000]),
                    ("updated_at", int(time.time())),
                ]
            },
        )
        try:
            self.client.update_row(
                "wx_media_tasks", row, Condition(RowExistenceExpectation.EXPECT_EXIST)
            )
        except Exception:
            # Task row may have been cleaned up; nothing to update.
            return

    def update_message_media(self, message: Message, fields: dict[str, Any]) -> None:
        from tablestore import Condition, Row, RowExistenceExpectation

        payload = {key: value for key, value in fields.items() if value is not None}
        if not payload:
            return
        row = Row(message.primary_key, {"PUT": list(payload.items())})
        self.client.update_row("wx_messages", row, Condition(RowExistenceExpectation.EXPECT_EXIST))

    def update_message_fields_batch(self, records: list[tuple[list, dict]]) -> None:
        from tablestore import (
            BatchWriteRowRequest,
            Condition,
            Row,
            RowExistenceExpectation,
            TableInBatchWriteRowItem,
            UpdateRowItem,
        )

        request = BatchWriteRowRequest()
        request.add(
            TableInBatchWriteRowItem(
                "wx_messages",
                [
                    UpdateRowItem(
                        Row(pk, {"PUT": list(fields.items())}),
                        Condition(RowExistenceExpectation.EXPECT_EXIST),
                    )
                    for pk, fields in records
                ],
            )
        )
        response = _retry_request(lambda request=request: self.client.batch_write_row(request))
        if any(not row.is_ok for row in response.get_update_by_table("wx_messages")):
            raise RuntimeError("Some message media updates failed")

    # ---- reads ----

    def get_row(self, table: str, primary_key: list[tuple[str, Any]]) -> dict[str, Any] | None:
        _, row, _ = _retry_request(lambda: self.client.get_row(table, primary_key, max_version=1))
        if not row:
            return None
        result = dict(row.primary_key)
        result.update({name: value for name, value, _ in row.attribute_columns})
        return result

    def get_message(self, conv_id: str, sort_seq: int, msg_uid: str) -> dict[str, Any] | None:
        return self.get_row(
            "wx_messages",
            [("conv_id", conv_id), ("sort_seq", sort_seq), ("msg_uid", msg_uid)],
        )

    def get_sync_state(self, source_id: str, conv_id: str) -> dict[str, Any] | None:
        return self.get_row("wx_sync_state", [("source_id", source_id), ("conv_id", conv_id)])

    def iter_messages(
        self,
        conv_id: str,
        *,
        columns: list[str] | None = None,
        limit: int = 5000,
        min_sort_seq: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        from tablestore import INF_MAX, INF_MIN, Direction

        start: list[tuple[str, Any]] | None = [
            ("conv_id", conv_id),
            ("sort_seq", INF_MIN if min_sort_seq is None else min_sort_seq),
            ("msg_uid", INF_MIN),
        ]
        end = [("conv_id", conv_id), ("sort_seq", INF_MAX), ("msg_uid", INF_MAX)]
        while start:
            _, start, rows, _ = _retry_request(
                lambda start=start: self.client.get_range(
                    "wx_messages",
                    Direction.FORWARD,
                    start,
                    end,
                    columns_to_get=_columns_to_get(columns, "wx_messages"),
                    limit=limit,
                )
            )
            for row in rows:
                item = dict(row.primary_key)
                item.update({name: value for name, value, _ in row.attribute_columns})
                yield item

    def scan(
        self, table: str, *, columns: list[str] | None = None, limit: int = 500
    ) -> Iterator[dict[str, Any]]:
        from tablestore import INF_MAX, INF_MIN, Direction

        names = [name for name, _ in TABLE_SCHEMAS[table]]
        start: list[tuple[str, Any]] | None = [(name, INF_MIN) for name in names]
        end = [(name, INF_MAX) for name in names]
        while start:
            _, start, rows, _ = _retry_request(
                lambda start=start: self.client.get_range(
                    table,
                    Direction.FORWARD,
                    start,
                    end,
                    columns_to_get=_columns_to_get(columns, table),
                    limit=limit,
                )
            )
            for row in rows:
                item = dict(row.primary_key)
                item.update({name: value for name, value, _ in row.attribute_columns})
                yield item

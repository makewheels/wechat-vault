from wechat_vault.dedupe import repair


class Store:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []
        self.client = self

    def scan(self, *args, **kwargs):
        yield from self.rows

    def get_message(self, conv_id, sort_seq, msg_uid):
        return next(
            r
            for r in self.rows
            if r["conv_id"] == conv_id and r["sort_seq"] == sort_seq and r["msg_uid"] == msg_uid
        )

    def delete_row(self, table, pk, condition):
        self.deleted.append(dict(pk))


class OSS:
    def __init__(self):
        self.bucket = self
        self.objects = {}

    def put_object(self, key, payload, headers=None):
        self.objects[key] = payload

    def get_bytes(self, key):
        return self.objects[key]


def row(conv, seq, text="hello"):
    return {
        "conv_id": conv,
        "sort_seq": seq,
        "msg_uid": "s123",
        "create_time": 100,
        "sender": "sender",
        "kind": "text",
        "text": text,
        "is_send": False,
    }


def test_dedupe_is_scoped_to_conversation_and_preserves_conflicts():
    store = Store([row("a", 1), row("a", 2), row("a", 3, "different"), row("b", 1)])
    oss = OSS()
    result = repair(store, oss, apply=True, log=lambda *a, **k: None)
    assert result["removed"] == 1
    assert result["conflicts"] == 1
    assert store.deleted == [{"conv_id": "a", "sort_seq": 2, "msg_uid": "s123"}]
    assert len(oss.objects) == 1


def test_dedupe_dry_run_never_deletes_or_writes_backup():
    store, oss = Store([row("a", 1), row("a", 2)]), OSS()
    result = repair(store, oss, log=lambda *a, **k: None)
    assert result["duplicates"] == 1
    assert not store.deleted
    assert not oss.objects

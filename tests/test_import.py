import hashlib
import json
from pathlib import Path

from wechat_vault.config import Config
from wechat_vault.import_staging import import_staging
from wechat_vault.model import conv_key
from wechat_vault.sources.staging import StagingPackage


class FakeStore:
    def __init__(self):
        self.accounts = []
        self.conversations = {}
        self.messages = []
        self.tasks = []
        self.sync_states = []
        self.media_updates = []

    def iter_messages(self, conv_id, columns=None):
        return (m.to_record() for m in self.messages if m.conv_id == conv_id)

    def put_account(self, *args, **kwargs):
        self.accounts.append((args, kwargs))

    def upsert_conversation(self, conversation):
        self.conversations[conversation.conv_id] = conversation

    def put_message(self, message):
        self.messages.append(message)
        return True

    def put_media_task(self, message, ref):
        self.tasks.append((message.msg_uid, ref))

    def put_sync_state(self, **kwargs):
        self.sync_states.append(kwargs)

    def update_message_media(self, message, fields):
        self.media_updates.append((message.msg_uid, fields))


class FakeOSS:
    def __init__(self):
        self.files = {}

    @staticmethod
    def key(sha, ext):
        return f"media/sha256/{sha}.{ext}"

    def exists(self, key):
        return key in self.files

    def size(self, key):
        return len(self.files[key])

    def put_file(self, path, key, mime, sha=""):
        self.files[key] = Path(path).read_bytes()


CONFIG = Config(
    endpoint="https://example.ots.aliyuncs.com",
    instance="wechat-vault",
    bucket="wechat-vault",
    oss_endpoint="https://oss-cn-beijing.aliyuncs.com",
    region="cn-beijing",
    ak_id="x",
    ak_secret="y",
    weflow_api="http://127.0.0.1:5031",
    weflow_token="t",
    source="win",
)


def build_package(root: Path, *, media_bytes: bytes = b""):
    (root / "messages").mkdir()
    (root / "media").mkdir()
    sha = hashlib.sha256(media_bytes).hexdigest()
    (root / "media" / f"{sha}.jpg").write_bytes(media_bytes)
    (root / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "source": "win", "account_id": "a123"}),
        encoding="utf-8",
    )
    session = {"talker": "wxid_peer", "name": "朋友", "last_ts": 1720177182, "is_group": False}
    (root / "sessions.jsonl").write_text(json.dumps(session) + "\n", encoding="utf-8")
    records = [
        {
            "conv_id": "a123:wxid_peer",
            "talker": "wxid_peer",
            "msg_uid": "s1",
            "server_id": "1",
            "sort_seq": 1000,
            "create_time": 1,
            "is_send": 0,
            "sender": "wxid_peer",
            "local_type": 1,
            "kind": "text",
            "text": "hi",
            "raw": "hi",
        },
        {
            "conv_id": "a123:wxid_peer",
            "talker": "wxid_peer",
            "msg_uid": "s2",
            "server_id": "2",
            "sort_seq": 2000,
            "create_time": 2,
            "is_send": 1,
            "sender": "wxid_me",
            "local_type": 3,
            "kind": "image",
            "text": "[图片]",
            "raw": "[图片]",
            "media_sha256": sha,
            "media_ext": "jpg",
        },
    ]
    (root / "messages" / f"{conv_key('wxid_peer')}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return sha


def test_import_writes_messages_and_media(tmp_path):
    sha = build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)
    store, oss = FakeStore(), FakeOSS()
    stats = import_staging(StagingPackage(tmp_path), store, oss, config=CONFIG)
    assert stats["messages"] == 2
    assert stats["written"] == 2
    assert stats["media_uploaded"] == 1
    assert len(store.messages) == 2
    assert store.messages[1].media_state == "done"
    assert store.messages[1].media_key == f"media/sha256/{sha}.jpg"
    assert store.conversations["a123:wxid_peer"].last_ts == 2
    assert len(store.sync_states) == 1


def test_import_no_media(tmp_path):
    build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)
    store, oss = FakeStore(), FakeOSS()
    stats = import_staging(StagingPackage(tmp_path), store, oss, config=CONFIG, no_media=True)
    assert stats["media_uploaded"] == 0
    assert store.messages[1].media_state == "pending"
    assert not oss.files


def test_import_existing_row_gets_media_fields(tmp_path):
    sha = build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)
    store, oss = FakeStore(), FakeOSS()
    store.put_message = lambda message: (store.messages.append(message), False)[1]
    stats = import_staging(StagingPackage(tmp_path), store, oss, config=CONFIG)
    assert stats["duplicates"] == 2
    assert [uid for uid, _ in store.media_updates] == ["s2"]
    fields = store.media_updates[0][1]
    assert fields["media_state"] == "done"
    assert fields["media_key"] == f"media/sha256/{sha}.jpg"


def test_import_dry_run(tmp_path):
    build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)
    store, oss = FakeStore(), FakeOSS()
    stats = import_staging(StagingPackage(tmp_path), store, oss, config=CONFIG, dry_run=True)
    assert stats["messages"] == 2
    assert store.messages == []
    assert store.accounts == []
    assert not oss.files


def test_cross_device_message_reuses_existing_order(tmp_path):
    from wechat_vault.model import Message

    build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)
    package = StagingPackage(tmp_path)
    original = next(package.messages())
    existing = Message.from_record(original.to_record())
    existing.sort_seq += 123456
    existing.content_hash = "different-source-normalization"
    store = FakeStore()
    store.messages.append(existing)
    import_staging(package, store, None, config=CONFIG, no_media=True)
    matched = [m for m in store.messages[1:] if m.msg_uid == existing.msg_uid]
    assert len(matched) == 1
    assert matched[0].sort_seq == existing.sort_seq


def test_prefetched_identity_map_skips_cross_device_duplicates(tmp_path):
    from wechat_vault.model import Message

    build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)
    package = StagingPackage(tmp_path)
    original = next(package.messages())
    existing = Message.from_record(original.to_record())
    existing.sort_seq += 999

    class BatchStore(FakeStore):
        def message_identities(self, account_id):
            return {
                existing.conv_id: {existing.msg_uid: (existing.sort_seq, existing.content_hash)}
            }

        def iter_messages(self, *args, **kwargs):
            raise AssertionError("A prefetched import must not query each conversation")

        def put_messages(self, messages):
            self.messages.extend(messages)
            return [True] * len(messages)

    store = BatchStore()
    stats = import_staging(package, store, None, config=CONFIG, no_media=True)
    assert stats["duplicates"] == 1
    assert stats["written"] == 1
    assert all(m.msg_uid != existing.msg_uid for m in store.messages)


def test_failed_batch_does_not_advance_sync_watermark(tmp_path):
    build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)

    class FailingBatchStore(FakeStore):
        def put_messages(self, messages):
            raise RuntimeError("simulated cloud outage")

    store = FailingBatchStore()
    stats = import_staging(StagingPackage(tmp_path), store, None, config=CONFIG, no_media=True)
    assert stats["errors"] == 2
    assert not store.sync_states


def test_content_variant_is_saved_without_rewriting_canonical_message(tmp_path):
    build_package(tmp_path, media_bytes=b"\xff\xd8\xff\xe0" + b"0" * 100)
    package = StagingPackage(tmp_path)
    original = next(package.messages())

    class VariantStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.variants = []

        def message_identities(self, account_id):
            return {original.conv_id: {original.msg_uid: (123456, "older-normalization")}}

        def put_message_variants(self, variants):
            self.variants.extend(variants)
            return [False] * len(variants)

        def put_messages(self, messages):
            self.messages.extend(messages)
            return [True] * len(messages)

    store = VariantStore()
    stats = import_staging(package, store, None, config=CONFIG, no_media=True)
    assert stats["duplicates"] == 1
    assert stats["written"] == 1
    assert len(store.variants) == 1
    assert store.variants[0][0].sort_seq == 123456
    assert store.variants[0][1] == "older-normalization"
    assert all(m.msg_uid != original.msg_uid for m in store.messages)

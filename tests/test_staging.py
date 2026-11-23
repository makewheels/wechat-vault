import json
from pathlib import Path

import pytest

from wechat_vault.model import conv_key
from wechat_vault.sources.staging import StagingError, StagingPackage


def write_package(root: Path, *, missing_sessions: bool = False):
    (root / "messages").mkdir(parents=True)
    (root / "media").mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "win",
                "account_id": "a123",
                "counts": {"sessions": 1, "messages": 2},
            }
        ),
        encoding="utf-8",
    )
    if not missing_sessions:
        (root / "sessions.jsonl").write_text(
            json.dumps(
                {"talker": "wxid_peer", "name": "朋友", "last_ts": 1720177182, "is_group": False}
            )
            + "\n",
            encoding="utf-8",
        )
    records = [
        {
            "schema_version": 1,
            "conv_id": "a123:wxid_peer",
            "talker": "wxid_peer",
            "msg_uid": "s1",
            "server_id": "1",
            "local_id": 1,
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
            "schema_version": 1,
            "conv_id": "a123:wxid_peer",
            "talker": "wxid_peer",
            "msg_uid": "s2",
            "server_id": "2",
            "local_id": 2,
            "sort_seq": 2000,
            "create_time": 2,
            "is_send": 1,
            "sender": "wxid_me",
            "local_type": 1,
            "kind": "text",
            "text": "yo",
            "raw": "yo",
            "media_sha256": "a" * 64,
            "media_ext": "jpg",
        },
    ]
    (root / "messages" / f"{conv_key('wxid_peer')}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    (root / "media" / f"{'a' * 64}.jpg").write_bytes(b"jpg")


def test_staging_reads_sessions_and_messages(tmp_path):
    write_package(tmp_path)
    package = StagingPackage(tmp_path)
    assert package.account_id == "a123"
    assert package.source == "win"
    sessions = list(package.sessions())
    assert len(sessions) == 1
    assert sessions[0].conv_id == "a123:wxid_peer"
    messages = list(package.messages())
    assert [m.msg_uid for m in messages] == ["s1", "s2"]
    assert messages[1].media_sha256 == "a" * 64
    assert messages[1].media_ext == "jpg"
    assert package.media_path("a" * 64, "jpg").exists()
    assert package.counts() == {"sessions": 1, "messages": 2}


def test_staging_limit(tmp_path):
    write_package(tmp_path)
    package = StagingPackage(tmp_path)
    assert len(list(package.messages(limit=1))) == 1


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(StagingError):
        _ = StagingPackage(tmp_path).manifest


def test_missing_sessions_is_empty(tmp_path):
    write_package(tmp_path, missing_sessions=True)
    package = StagingPackage(tmp_path)
    assert list(package.sessions()) == []
    assert list(package.messages()) == []


class FakeOSS:
    def __init__(self, files):
        self.files = files

    def exists(self, key):
        return key in self.files

    def get_to_file(self, key, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.files[key])

    def list_keys(self, prefix):
        return [key for key in self.files if key.startswith(prefix)]


def test_staging_from_oss(tmp_path):
    local = tmp_path / "local"
    write_package(local)
    files = {}
    for path in local.rglob("*"):
        if path.is_file() and "media" not in path.parts:
            files["staging/win/run1/" + path.relative_to(local).as_posix()] = path.read_bytes()
    package = StagingPackage.from_oss(FakeOSS(files), "staging/win/run1", tmp_path / "fetch")
    assert package.account_id == "a123"
    assert [m.msg_uid for m in package.messages()] == ["s1", "s2"]

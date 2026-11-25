import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from wechat_vault.media_finish import link


@pytest.mark.parametrize(
    "has_original,wrong_directory,expected_state",
    [(True, False, "done"), (False, False, "thumbnail_only"), (True, True, "missing")],
)
def test_links_available_variants_without_crossing_directories(
    monkeypatch, tmp_path, has_original, wrong_directory, expected_state
):
    con = sqlite3.connect(":memory:")
    con.execute("create table dir2id(username text)")
    con.executemany("insert into dir2id values (?)", [("2026-11",), ("chat-dir",)])
    for kind in ("image", "video", "file"):
        con.execute(
            f"create table {kind}_hardlink_info_v4(md5 text,file_name text,dir1 int,dir2 int)"
        )
    digest = "a" * 32
    con.execute("insert into image_hardlink_info_v4 values (?,?,1,2)", (digest, "asset.dat"))
    monkeypatch.setitem(
        sys.modules, "wcdb_snapshot", SimpleNamespace(open_memory=lambda *a: (con, {}))
    )
    monkeypatch.setenv("WECHAT_DB_KEY", "0" * 64)
    folder = "other-chat" if wrong_directory else "chat-dir"
    rows = [
        {
            "path": f"msg/attach/{folder}/2026-11/Img/asset_t.dat",
            "key": "media/thumb.jpg",
            "sha256": "thumb",
            "ext": "jpg",
            "size": 100,
            "state": "done",
        }
    ]
    if has_original:
        rows.append(
            {
                "path": f"msg/attach/{folder}/2026-11/Img/asset_h.dat",
                "key": "media/original.jpg",
                "sha256": "original",
                "ext": "jpg",
                "size": 1000,
                "state": "done",
            }
        )
    oss = SimpleNamespace(get_bytes=lambda _: "\n".join(json.dumps(r) for r in rows).encode())

    class Store:
        def __init__(self):
            self.updates = []

        def scan(self, *args, **kwargs):
            yield {
                "conv_id": "acct:chat",
                "msg_uid": "s1",
                "sort_seq": 1,
                "kind": "image",
                "text": "",
                "raw": f'<img md5="{digest}"/>',
                "src": "mac",
            }

        def update_message_fields_batch(self, records):
            self.updates.extend(records)

    store = Store()
    result = link(oss, store, tmp_path, "acct", {"index_key": "index"}, {})
    fields = store.updates[0][1]
    assert fields["media_state"] == expected_state
    assert result["linked"] == (not wrong_directory)
    if not wrong_directory:
        assert fields["media_key"] == ("media/original.jpg" if has_original else "media/thumb.jpg")

import sqlite3
import unittest

import export_snapshot as es
import export_weflow as ew


def make_client(my_wxid="wxid_me"):
    client = object.__new__(es.SnapshotClient)
    client.my_wxid = my_wxid
    client.decode_failures = 0
    return client


class DecodeTests(unittest.TestCase):
    def test_text_bytes_and_none(self):
        self.assertEqual(es.decode_content("hi"), "hi")
        self.assertEqual(es.decode_content("你好".encode()), "你好")
        self.assertEqual(es.decode_content(None), "")

    def test_zstd_blob(self):
        raw = "压缩消息".encode()
        try:
            from compression import zstd
            blob = zstd.compress(raw)
        except ImportError:
            import zstandard
            blob = zstandard.ZstdCompressor().compress(raw)
        self.assertEqual(es.decode_content(blob), "压缩消息")


class ConvertTests(unittest.TestCase):
    names = {2: "wxid_me", 7: "wxid_bob"}

    def convert(self, talker, sender_id, content, client=None):
        client = client or make_client()
        return client.convert(talker, self.names, 2, (1, 99, 1, 5000, sender_id, 1700000000, content, None))

    def test_group_prefix_stripped_for_others_only(self):
        other = self.convert("1@chatroom", 7, "wxid_bob:\nhello")
        self.assertEqual((other["content"], other["rawContent"], other["senderUsername"], other["isSend"]),
                         ("hello", "wxid_bob:\nhello", "wxid_bob", 0))
        mine = self.convert("1@chatroom", 2, "wxid_bob:\nquoted")
        self.assertEqual((mine["content"], mine["senderUsername"], mine["isSend"]),
                         ("wxid_bob:\nquoted", "wxid_me", 1))

    def test_prefix_kept_when_it_is_not_the_sender(self):
        msg = self.convert("1@chatroom", 7, "note:\nbody")
        self.assertEqual(msg["content"], "note:\nbody")

    def test_single_chat_sender_is_the_talker(self):
        msg = self.convert("wxid_bob", 0, "x")
        self.assertEqual(msg["senderUsername"], "wxid_bob")

    def test_undecodable_content_is_counted(self):
        client = make_client()
        msg = self.convert("wxid_bob", 7, es.ZSTD_MAGIC + b"broken", client)
        self.assertEqual((msg["content"], client.decode_failures), ("", 1))


class SchemaTests(unittest.TestCase):
    def test_missing_column_fails_loudly(self):
        con = sqlite3.connect(":memory:")
        con.execute("create table Name2Id (id integer)")
        with self.assertRaises(ew.ExportError):
            es.require_columns("t", con, "Name2Id", {"user_name"})

    def test_missing_table_fails_loudly(self):
        with self.assertRaises(ew.ExportError):
            es.require_columns("t", sqlite3.connect(":memory:"), "contact", {"username"})

    def test_complete_table_passes(self):
        con = sqlite3.connect(":memory:")
        con.execute("create table Name2Id (user_name text)")
        es.require_columns("t", con, "Name2Id", {"user_name"})


if __name__ == "__main__":
    unittest.main()

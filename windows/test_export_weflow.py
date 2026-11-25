"""Tests for export_weflow against an in-process fake of WeFlow 2.1.4's HTTP API.

Run from this directory:  python -m unittest -v
"""
import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import export_weflow as ew

BASE = 1_700_000_000
OLD = BASE - 3 * 86400
ME, FRIEND, GROUP, OFFICIAL = "wxid_me", "wxid_friend", "123456@chatroom", "gh_daily"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" * 50
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\0" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 40
ENCRYPTED = bytes(range(1, 200))
APP_XML = ("<msg><appmsg><title>报告.pdf</title><type>6</type><appattach>"
           "<aeskey>00112233445566778899aabbccddeeff</aeskey>"
           '<cdnattachurl token="SECRETTOKEN"></cdnattachurl></appattach></appmsg></msg>')


def msg(talker, i, ts=None, **over):
    ts = BASE + i * 60 if ts is None else ts
    data = {"localId": i + 1, "serverId": 7_000_000_000_000_000_000 + i, "localType": 1, "createTime": ts,
            "sortSeq": ts * 1000 + i, "isSend": i % 2, "senderUsername": ME if i % 2 else talker,
            "content": f"msg {i}", "rawContent": f"msg {i}"}
    data.update(over)
    return data


class FakeWeFlow:
    """Ascending messages with offset/limit paging, a `start` filter and media paths only when media=1."""

    def __init__(self, root):
        self.media_root = Path(root) / "cache" / "api-media"
        self.sessions, self.messages, self.requests = [], {}, []
        self.fail = lambda path, params: None
        self.lock = threading.Lock()
        self.closed = False
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                with fake.lock:
                    fake.requests.append((url.path, params))
                status = fake.fail(url.path, params)
                if status:
                    return self.send_json(status, {"success": False, "error": "injected"})
                if url.path in ("/health", "/api/v1/health"):
                    return self.send_json(200, {"status": "ok"})
                if url.path == "/api/v1/sessions":
                    return self.send_json(200, {"success": True, "count": len(fake.sessions),
                                                "sessions": fake.sessions})
                if url.path == "/api/v1/contacts":
                    return self.send_json(200, {"success": True, "count": 1, "contacts": [
                        {"username": FRIEND, "nickname": "朋友", "remark": "", "alias": ""}]})
                if url.path == "/api/v1/messages":
                    return self.send_json(200, fake.page(params))
                self.send_json(404, {"success": False, "error": "not found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def page(self, params):
        talker, limit, offset = params["talker"], int(params["limit"]), int(params.get("offset", 0))
        rows = self.messages.get(talker, [])
        if "start" in params:
            rows = [m for m in rows if m["createTime"] >= int(params["start"])]
        with_media = params.get("media") == "1"
        out = []
        for row in rows[offset:offset + limit]:
            row = dict(row)
            path = row.pop("_media", None)
            if with_media and path:
                row["mediaPath"] = path
            out.append(row)
        return {"success": True, "talker": talker, "count": len(out), "hasMore": offset + limit < len(rows),
                "media": {"enabled": with_media, "exportPath": str(self.media_root), "count": 0},
                "messages": out}

    def write_media(self, talker, folder, name, data):
        path = self.media_root / talker / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def close(self):
        if not self.closed:
            self.closed = True
            self.httpd.shutdown()
            self.httpd.server_close()


def build_world(root):
    fake = FakeWeFlow(root)
    friend = [msg(FRIEND, i, ts=OLD + i * 60 if i < 3 else None) for i in range(6)]
    friend[2]["serverId"] = 0
    friend[4], friend[5] = friend[5], friend[4]  # out of order on the wire; the package must still be sorted
    friend.insert(2, dict(friend[1]))  # WeFlow's cursor paging can deliver a row twice
    friend += [
        msg(FRIEND, 6, localType=3, _media=fake.write_media(FRIEND, "images", "a.jpg", JPEG)),
        msg(FRIEND, 7, localType=3, _media=fake.write_media(FRIEND, "images", "b.jpg", ENCRYPTED)),
        msg(FRIEND, 8, localType=3),
        msg(FRIEND, 9, localType=43, _media=fake.write_media(FRIEND, "videos", "c.mp4", MP4)),
        msg(FRIEND, 10, localType=34, _media=fake.write_media(FRIEND, "voices", "d.wav", WAV)),
        msg(FRIEND, 11, localType=10000, content="<sysmsg><![CDATA[你撤回了一条消息]]></sysmsg>"),
        msg(FRIEND, 12, localType=(6 << 32) | 49, content="[文件]", rawContent=APP_XML),
        msg(FRIEND, 13, content="bad \ud83d end", rawContent="bad \ud83d end"),
    ]
    fake.messages[FRIEND] = friend
    fake.messages[GROUP] = [msg(GROUP, i, senderUsername=ME if i % 2 else f"wxid_member{i % 3}")
                            for i in range(120)]
    fake.messages[OFFICIAL] = [msg(OFFICIAL, i, localType=49, isSend=0, senderUsername=OFFICIAL,
                                   rawContent=f"<msg><appmsg><title>标题 {i}</title><type>5</type></appmsg></msg>")
                               for i in range(3)]
    for talker, name in ((FRIEND, "朋友"), (GROUP, "群聊"), (OFFICIAL, "公众号")):
        fake.sessions.append({"username": talker, "displayName": name, "type": 1, "unreadCount": 0,
                              "lastTimestamp": max(m["createTime"] for m in fake.messages[talker])})
    return fake


class HelperTests(unittest.TestCase):
    def test_identifiers_match_the_contract(self):
        self.assertEqual(ew.conv_key("wxid_a"), "79a283cbbfc778de")
        self.assertEqual(ew.account_id_for("wxid_a"), "a79a283cbbfc7")
        self.assertEqual(ew.wxid_hash("wxid_a"),
                         "sha256:ad80f74115b9ee649910da78572ad97fbacdea9aaf4245e84fa41633f3cb077e")
        self.assertEqual(ew.msg_uid_for("wxid_a", "123", 1, 1, 1, "hello", "wxid_a"), "s123")
        for empty in (None, "", "0", 0):
            self.assertEqual(ew.msg_uid_for("wxid_a", empty, 1700000000, 1, 1, "hello", "wxid_a"),
                             "h57cb3147a48781210980")

    def test_kind_and_session_type(self):
        self.assertEqual([ew.kind_for(t) for t in (1, 3, 34, 43, 47, 48, 42, 49, (6 << 32) | 49, 10000, 99)],
                         ["text", "image", "voice", "video", "emoji", "location", "card", "app", "app",
                          "system", "other"])
        self.assertEqual([ew.classify_session(t) for t in
                          ("1@chatroom", "gh_x", "filehelper", "wxid_x", "x@openim")],
                         ["group", "official", "system", "single", "other"])

    def test_sanitize_raw_drops_keys_and_tokens(self):
        raw = '<emoji aeskey="AAAA" md5="m"/><aeskey>BBBB</aeskey><u href="http://x/y?token=CCCC&a=1"/>'
        cleaned, truncated = ew.sanitize_raw(raw)
        for secret in ("AAAA", "BBBB", "CCCC"):
            self.assertNotIn(secret, cleaned)
        self.assertIn('md5="m"', cleaned)
        self.assertFalse(truncated)
        big, truncated = ew.sanitize_raw("x" * (ew.MAX_RAW_BYTES + 10))
        self.assertTrue(truncated)
        self.assertEqual(len(big), ew.MAX_RAW_BYTES)

    def test_sniff_and_dumps(self):
        for data, ext in ((JPEG, "jpg"), (b"\x89PNG\r\n\x1a\n" + b"0" * 24, "png"), (MP4, "mp4"), (WAV, "wav"),
                          (b"%PDF-1.7" + b"0" * 24, "pdf")):
            self.assertEqual(ew.sniff(data[:32])[0], ext)
        self.assertIsNone(ew.sniff(ENCRYPTED[:32]))
        line = ew.dumps({"t": "a\ud83db", "k": ["\ude00"]})
        line.encode("utf-8")
        self.assertEqual(json.loads(line), {"t": "a\ufffdb", "k": ["\ufffd"]})

    def test_unverified_video_is_not_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, media_dir = Path(tmp) / "v.mp4", Path(tmp) / "media"
            media_dir.mkdir()
            src.write_bytes(MP4)
            with mock.patch.object(ew, "probe_video", return_value=False):
                outcome = ew.ingest_media(src, media_dir, "video", "ffprobe", "t")
            self.assertEqual(outcome.status, "unverified")
            self.assertEqual(list(media_dir.iterdir()), [])
            with mock.patch.object(ew, "probe_video", return_value=True):
                outcome = ew.ingest_media(src, media_dir, "video", "ffprobe", "t")
            self.assertEqual((outcome.status, outcome.ext), ("ok", "mp4"))
            self.assertEqual([p.name for p in media_dir.iterdir()], [hashlib.sha256(MP4).hexdigest() + ".mp4"])

    def test_probe_total_counts_exactly(self):
        class Rows:
            def __init__(self, total):
                self.total = total

            def messages(self, talker, offset, limit, media, start=None):
                return {"messages": [{}] * max(0, min(limit, self.total - offset))}

        opts = ew.build_parser().parse_args(["--out", "unused"])
        for total in (0, 1, 499, 500, 501, 999, 1000, 1001, 1300, 4999, 5000):
            with self.subTest(total=total), mock.patch.object(ew, "SLEEP", lambda seconds: None):
                self.assertEqual(ew.Exporter(opts, client=Rows(total)).probe_total("t", False), total)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ew-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = build_world(self.tmp)
        self.addCleanup(self.fake.close)
        self.out = self.tmp / "out"
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WECHAT_MY_WXID", None)
        os.environ.pop("WEFLOW_ACCESS_TOKEN", None)
        sleep = mock.patch.object(ew, "SLEEP", lambda seconds: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_export(self, *extra, api=None):
        argv = ["--out", str(self.out), "--api-base", api or self.fake.url, "--ffprobe", "none",
                "--weflow-version", "2.1.4", "--min-free-gb", "0", "--run-id", "t1", *extra]
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = ew.main(argv)
        self.output = captured.getvalue()
        return code

    def records(self, talker):
        path = self.out / "messages" / f"{ew.conv_key(talker)}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def json_file(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def snapshot(self):
        skip = {"manifest.json", "report.md", "state.json"}
        return {p.relative_to(self.out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.out.rglob("*") if p.is_file() and p.name not in skip}

    def test_full_export_matches_contract(self):
        self.assertEqual(self.run_export("--full"), 0, self.output)
        manifest_text = (self.out / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        self.assertEqual((manifest["source"], manifest["full"], manifest["counts"]["sessions"]), ("win", True, 3))
        self.assertEqual(manifest["account_id"], ew.account_id_for(ME))
        self.assertEqual(manifest["my_wxid_hash"], ew.wxid_hash(ME))
        self.assertNotIn(ME, manifest_text)
        self.assertEqual(ew.verify_package(self.out)[0], [])
        self.assertEqual(list(self.out.rglob("*.tmp")), [])
        self.assertTrue(all(e["done"] for e in self.json_file("state.json")["conversations"].values()))

        friend = self.records(FRIEND)
        self.assertEqual(len(friend), len(self.fake.messages[FRIEND]) - 1)  # the duplicate row is dropped
        keys = [(r["sort_seq"], r["msg_uid"]) for r in friend]
        self.assertEqual(keys, sorted(keys))
        by_i = {r["local_id"] - 1: r for r in friend}
        self.assertTrue(by_i[2]["msg_uid"].startswith("h"))
        self.assertIsNone(by_i[2]["server_id"])
        self.assertEqual(by_i[3]["msg_uid"], "s" + by_i[3]["server_id"])

        jpeg = by_i[6]
        self.assertEqual((jpeg["media_sha256"], jpeg["media_ext"], jpeg["media_mime"], jpeg["media_size"]),
                         (hashlib.sha256(JPEG).hexdigest(), "jpg", "image/jpeg", len(JPEG)))
        self.assertEqual(jpeg["flags"], [])
        self.assertEqual((by_i[7]["media_sha256"], by_i[7]["flags"]), (None, ["media_encrypted"]))
        self.assertEqual((by_i[8]["media_sha256"], by_i[8]["flags"]), (None, ["media_missing"]))
        self.assertEqual((by_i[9]["media_ext"], by_i[9]["flags"]), ("mp4", ["media_unverified"]))
        self.assertEqual((by_i[10]["media_ext"], by_i[10]["flags"]), ("wav", []))
        self.assertEqual((by_i[11]["kind"], by_i[11]["text"]), ("system", "你撤回了一条消息"))
        app = by_i[12]
        self.assertEqual((app["kind"], app["text"]), ("app", "报告.pdf"))
        self.assertIn("<title>报告.pdf</title>", app["raw"])
        self.assertNotIn("00112233", app["raw"])
        self.assertNotIn("SECRETTOKEN", app["raw"])
        self.assertEqual(by_i[13]["text"], "bad \ufffd end")
        self.assertIn("bad_utf16", by_i[13]["flags"])
        self.assertNotIn("bad_utf16", by_i[1]["flags"])

        self.assertEqual(len(self.records(GROUP)), 120)
        types = {r["talker"]: r["session_type"] for r in map(json.loads, (self.out / "sessions.jsonl")
                                                              .read_text(encoding="utf-8").splitlines())}
        self.assertEqual(types, {FRIEND: "single", GROUP: "group", OFFICIAL: "official"})

    def test_rerun_is_idempotent_and_resume_skips_finished(self):
        self.assertEqual(self.run_export("--full"), 0, self.output)
        first = self.snapshot()
        self.assertEqual(self.run_export("--full"), 0, self.output)
        self.assertEqual(self.snapshot(), first)
        self.fake.requests.clear()
        self.assertEqual(self.run_export(), 0, self.output)
        self.assertEqual(self.snapshot(), first)
        self.assertEqual([p for path, p in self.fake.requests if p.get("media") == "1"], [])

    def test_small_pages_after_failure_and_failed_conversation_is_kept(self):
        self.fake.fail = lambda path, params: (
            500 if path == "/api/v1/messages" and params.get("media") == "1" and params["limit"] == "200" else None)
        self.assertEqual(self.run_export("--full"), 0, self.output)
        self.assertEqual(len(self.records(GROUP)), 120)
        group_file = self.out / "messages" / f"{ew.conv_key(GROUP)}.jsonl"
        before = group_file.read_bytes()

        self.fake.fail = lambda path, params: (
            500 if params.get("talker") == GROUP and params.get("media") == "1" else None)
        self.assertEqual(self.run_export("--full"), 3, self.output)
        self.assertEqual(group_file.read_bytes(), before)
        conversations = self.json_file("state.json")["conversations"]
        self.assertEqual((conversations[GROUP]["done"], conversations[FRIEND]["done"]), (False, True))
        errors = [json.loads(line) for line in (self.out / "errors.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({e["talker"] for e in errors}, {GROUP})

        self.fake.fail = lambda path, params: None
        self.fake.requests.clear()
        self.assertEqual(self.run_export(), 0, self.output)
        self.assertEqual({p["talker"] for path, p in self.fake.requests if p.get("media") == "1"}, {GROUP})

    def test_incremental_replaces_only_the_lookback_window(self):
        self.assertEqual(self.run_export("--full"), 0, self.output)
        before = self.records(FRIEND)
        last = self.fake.messages[FRIEND][-1]
        last["content"] = last["rawContent"] = "edited"
        self.fake.messages[FRIEND] += [msg(FRIEND, 20), msg(FRIEND, 21)]
        self.fake.requests.clear()
        self.assertEqual(self.run_export("--incremental"), 0, self.output)
        after = self.records(FRIEND)
        self.assertEqual(len(after), len(before) + 2)
        self.assertEqual(len({r["msg_uid"] for r in after}), len(after))
        self.assertEqual([r["text"] for r in after[:3]], [r["text"] for r in before[:3]])
        self.assertIn("edited", {r["text"] for r in after})
        self.assertNotIn("bad \ufffd end", {r["text"] for r in after})
        starts = {p["start"] for path, p in self.fake.requests if p.get("talker") == FRIEND and "start" in p}
        self.assertEqual(starts, {str(max(r["create_time"] for r in before) - 86400)})
        self.assertFalse(self.json_file("manifest.json")["full"])

    def test_since_exports_only_recent_messages_and_skips_empty_conversations(self):
        since = BASE + 100 * 60
        group_rows = len(self.fake.messages[GROUP])
        self.assertGreater(group_rows, 100)
        self.assertEqual(self.run_export("--since", str(since), "--conv", GROUP, "--conv", FRIEND), 0, self.output)
        recent = self.records(GROUP)
        self.assertEqual(len(recent), group_rows - 100)
        self.assertTrue(all(r["create_time"] >= since for r in recent))
        self.assertFalse((self.out / "messages" / f"{ew.conv_key(FRIEND)}.jsonl").exists())
        manifest = self.json_file("manifest.json")
        self.assertEqual((manifest["full"], manifest["since"], manifest["counts"]["sessions"]), (False, since, 1))
        self.assertEqual(ew.verify_package(self.out)[0], [])

    def test_limit_keeps_the_latest_messages_only(self):
        self.assertEqual(self.run_export("--conv", GROUP, "--limit", "5"), 0, self.output)
        self.assertEqual([r["text"] for r in self.records(GROUP)], [f"msg {i}" for i in range(115, 120)])
        self.assertFalse(self.json_file("state.json")["conversations"][GROUP]["done"])
        manifest = self.json_file("manifest.json")
        self.assertEqual((manifest["full"], manifest["counts"]["sessions"]), (False, 1))

    def test_media_outside_weflow_folder_is_never_read(self):
        secret = self.tmp / "elsewhere" / "secret.jpg"
        secret.parent.mkdir()
        secret.write_bytes(JPEG + b"secret")
        self.fake.messages[FRIEND].append(msg(FRIEND, 30, localType=3, _media=str(secret)))
        self.assertEqual(self.run_export("--full"), 3, self.output)
        rec = next(r for r in self.records(FRIEND) if r["local_id"] == 31)
        self.assertEqual((rec["media_sha256"], rec["flags"]), (None, ["media_missing"]))
        self.assertNotIn(hashlib.sha256(JPEG + b"secret").hexdigest() + ".jpg",
                         {p.name for p in (self.out / "media").iterdir()})

    def test_startup_guards(self):
        self.assertEqual(self.run_export(api="http://10.1.2.3:5031"), 1)
        self.assertIn("loopback", self.output)
        self.assertEqual(self.run_export("--account-id", "a000000000000"), 1)
        self.assertIn("account mismatch", self.output)
        self.assertEqual(list((self.out / "messages").glob("*.jsonl")), [])
        dead = self.fake.url
        self.fake.close()
        self.assertEqual(self.run_export(api=dead), 1)
        self.assertIn("HTTP API", self.output)

    def test_verify_flags_tampered_media(self):
        self.assertEqual(self.run_export("--full"), 0, self.output)
        self.assertEqual(self.run_export("--verify"), 0, self.output)
        media = next(p for p in (self.out / "media").iterdir() if p.suffix == ".jpg")
        media.write_bytes(media.read_bytes() + b"x")
        self.assertEqual(self.run_export("--verify"), 4, self.output)
        self.assertIn("media hash mismatch", self.output)


if __name__ == "__main__":
    unittest.main()


from wechat_vault.model import (
    Message,
    conv_key,
    extract_app_text,
    kind_for,
    msg_uid,
    normalize_message,
    sanitize_raw,
    sort_seq_for,
)


def test_normalize_text_message():
    raw = {
        "localId": 1,
        "serverId": 5352695609301753000,
        "localType": 1,
        "createTime": 1720177182,
        "sortSeq": 1720177182000,
        "isSend": 1,
        "senderUsername": "wxid_me",
        "content": "我以前也是",
        "rawContent": "我以前也是",
    }
    message = normalize_message(raw, talker="wxid_peer", account_id="a123", src="win")
    assert message.msg_uid == "s5352695609301753000"
    assert message.conv_id == "a123:wxid_peer"
    assert message.sort_seq == 1720177182000
    assert message.kind == "text"
    assert message.text == "我以前也是"
    assert message.is_send is True
    assert message.content_hash


def test_fallback_uid_is_stable_without_server_id():
    kwargs = {
        "server_id": 0,
        "talker": "wxid_peer",
        "create_time": 1720177182,
        "is_send": False,
        "local_type": 1,
        "content": "你好",
        "sender": "wxid_peer",
    }
    assert msg_uid(**kwargs) == msg_uid(**kwargs)
    changed = dict(kwargs, content="你好呀")
    assert msg_uid(**kwargs) != msg_uid(**changed)
    assert msg_uid(**kwargs).startswith("h")


def test_kind_mapping():
    assert kind_for(1) == "text"
    assert kind_for(3) == "image"
    assert kind_for(43) == "video"
    assert kind_for(49) == "app"
    assert kind_for(244813135921) == "app"
    assert kind_for(10000) == "system"
    assert kind_for(999) == "other"


def test_sanitize_removes_keys_and_tokens():
    raw = '<msg aeskey="deadbeef" cdnthumbaeskey="cafe"><url>https://x?access_token=secret&amp;y=1</url></msg>'
    cleaned, truncated = sanitize_raw(raw)
    assert "deadbeef" not in cleaned
    assert "cafe" not in cleaned
    assert "secret" not in cleaned
    assert "aeskey=\"\"" in cleaned
    assert truncated is False


def test_sanitize_truncates_huge_raw():
    cleaned, truncated = sanitize_raw("x" * (300 * 1024))
    assert truncated is True
    assert len(cleaned.encode("utf-8")) <= 256 * 1024


def test_app_title_extraction():
    raw = "<msg><appmsg><title><![CDATA[标题 & 内容]]></title></appmsg></msg>"
    assert extract_app_text(raw) == "标题 & 内容"


def test_sort_seq_fallback():
    assert sort_seq_for(1720177182, None) == 1720177182000
    assert sort_seq_for(1720177182, 0) == 1720177182000
    assert sort_seq_for(1720177182, 1720177182123) == 1720177182123


def test_conv_key_stable():
    assert conv_key("wxid_peer") == conv_key("wxid_peer")
    assert len(conv_key("wxid_peer")) == 16


def test_from_record_handles_string_bools():
    record = {
        "conv_id": "a123:wxid_peer",
        "talker": "wxid_peer",
        "msg_uid": "s1",
        "server_id": "1",
        "create_time": 1,
        "sort_seq": 1000,
        "is_send": "0",
        "sender": "wxid_peer",
        "kind": "text",
        "text": "hi",
        "raw": "hi",
    }
    message = Message.from_record(record)
    assert message.is_send is False
    assert message.msg_uid == "s1"


def test_normalize_app_message_uses_title():
    raw = {
        "localId": 2,
        "serverId": 2,
        "localType": 49,
        "createTime": 10,
        "sortSeq": 10000,
        "isSend": 0,
        "senderUsername": "gh_x",
        "content": "[链接]",
        "rawContent": "<msg><appmsg><title>文章标题</title></appmsg></msg>",
    }
    message = normalize_message(raw, talker="gh_x", account_id="a123", src="win")
    assert message.kind == "app"
    assert message.text == "文章标题"

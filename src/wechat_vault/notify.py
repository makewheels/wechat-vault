"""Failure notifications to a Feishu group.

Two ways to send, picked from what the environment provides:
- Open API: FEISHU_APP_ID + FEISHU_APP_SECRET + FEISHU_CHAT_ID (works on any machine);
- lark-cli (already logged in as the bot on the Mac): FEISHU_CHAT_ID only.
Nothing here ever raises; a failed notification is only reported by the return value.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://open.feishu.cn"


def _post(url: str, body: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https endpoint
        return json.loads(response.read())


def _send_api(msg_type: str, content: str, app_id: str, app_secret: str, chat_id: str) -> bool:
    token = _post(f"{BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
                  {"app_id": app_id, "app_secret": app_secret}).get("tenant_access_token", "")
    if not token:
        return False
    result = _post(f"{BASE_URL}/open-apis/im/v1/messages?receive_id_type=chat_id",
                   {"receive_id": chat_id, "msg_type": msg_type, "content": content}, token)
    return result.get("code") == 0


def _find_lark_cli() -> str | None:
    found = os.environ.get("LARK_CLI") or shutil.which("lark-cli")
    if found:
        return found
    # launchd's PATH usually lacks nvm-installed node binaries.
    candidates = sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/lark-cli")))
    return candidates[-1] if candidates else None


def _send_cli(msg_type: str, content: str, chat_id: str) -> bool:
    cli = _find_lark_cli()
    if not cli:
        return False
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + str(Path(cli).parent)
    result = subprocess.run(
        [cli, "im", "+messages-send", "--as", "bot", "--chat-id", chat_id, "--msg-type", msg_type,
         "--content", content],
        capture_output=True, timeout=60, env=env, check=False,
    )
    return b'"ok": true' in result.stdout or b'"ok":true' in result.stdout


def _deliver(msg_type: str, content: str) -> bool:
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    if not chat_id:
        return False
    app_id, app_secret = os.environ.get("FEISHU_APP_ID", ""), os.environ.get("FEISHU_APP_SECRET", "")
    try:
        if app_id and app_secret:
            return _send_api(msg_type, content, app_id, app_secret, chat_id)
        return _send_cli(msg_type, content, chat_id)
    except Exception:  # noqa: BLE001 - a broken notifier must not mask the original failure
        return False


def send(text: str) -> bool:
    return _deliver("text", json.dumps({"text": text}))


def build_card(title: str, color: str, fields: list[tuple[str, str]], body: str = "") -> dict:
    """An interactive card: coloured header, two-column key/value grid, optional text, send time footer."""
    elements: list[dict] = [{
        "tag": "div",
        "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**{name}**\n{value}"}}
            for name, value in fields
        ],
    }]
    if body:
        elements += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": body}}]
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"发送时间 {time.strftime('%Y-%m-%d %H:%M:%S')}"}],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "elements": elements,
    }


def send_card(title: str, color: str, fields: list[tuple[str, str]], body: str = "") -> bool:
    return _deliver("interactive", json.dumps(build_card(title, color, fields, body), ensure_ascii=False))

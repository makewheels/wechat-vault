"""Read WeFlow's stored WeChat keys on the Mac and put them into Infisical. Prints nothing secret.

WeFlow keeps decryptKey / imageAesKey / imageXorKey as Electron safeStorage values ("safe:" + base64 of
"v10" + AES-128-CBC). The AES key is PBKDF2-SHA1(keychain password, "saltysalt", 1003, 16) and the
password is the keychain item "weflow Safe Storage". Reading that item needs an UNLOCKED login keychain,
so run this from a Terminal window in the logged-in GUI session (not over SSH), and click "Allow" if
macOS asks.

    python3 mac/wxkey_mac.py

Writes WECHAT_DB_KEY_MAC, WECHAT_IMAGE_AES_KEY_MAC and WECHAT_IMAGE_XOR_KEY_MAC to Infisical
(tools-secrets / dev / /wechat-vault) and a one-word result to /tmp/wxkey.status.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

STATUS = "/tmp/wxkey.status"
PROJECT = "79c61872-d992-44c7-a5ae-d3e70e004e7f"
FIELDS = {
    "decryptKey": ("WECHAT_DB_KEY_MAC", re.compile(r"[0-9a-fA-F]{64}")),
    "imageAesKey": ("WECHAT_IMAGE_AES_KEY_MAC", re.compile(r".{16}", re.S)),
    "imageXorKey": ("WECHAT_IMAGE_XOR_KEY_MAC", re.compile(r"(0x[0-9a-fA-F]{1,2}|[0-9]{1,3})")),
}


def done(msg, code=0):
    with open(STATUS, "w") as handle:
        handle.write(msg + "\n")
    sys.exit(code)


def decrypt(raw, key):
    result = subprocess.run(["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", (b" " * 16).hex()],
                            input=raw, capture_output=True)
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else None


config_path = os.path.expanduser("~/Library/Application Support/WeFlow/WeFlow-config.json")
config = json.load(open(config_path))
if not str(config.get("decryptKey", "")).startswith("safe:"):
    done("unexpected-format", 1)

found = subprocess.run(["security", "find-generic-password", "-s", "weflow Safe Storage", "-w"],
                       capture_output=True, text=True)
if found.returncode != 0:
    done(f"keychain-rc-{found.returncode}", 1)
aes_key = hashlib.pbkdf2_hmac("sha1", found.stdout.strip().encode(), b"saltysalt", 1003, 16)

env = dict(os.environ, PATH="/opt/homebrew/bin:" + os.environ.get("PATH", ""))
stored = []
for field, (secret_name, shape) in FIELDS.items():
    value = config.get(field)
    if value in (None, ""):
        continue
    value = str(value)
    if value.startswith("safe:"):
        raw = base64.b64decode(value[5:])
        value = decrypt(raw[3:], aes_key) if raw[:3] == b"v10" else None
    if value is None or not shape.fullmatch(value):
        if field == "decryptKey":
            done("decrypt-failed-or-bad-shape", 1)
        continue
    out = subprocess.run(["infisical", "secrets", "set", f"{secret_name}={value}", "--projectId", PROJECT,
                          "--env", "dev", "--path", "/wechat-vault"], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        done(f"infisical-rc-{out.returncode}", out.returncode)
    stored.append(secret_name)
done("ok " + ",".join(stored))

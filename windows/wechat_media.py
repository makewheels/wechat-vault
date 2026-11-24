"""Decode WeChat 4.x image .dat files (read-only, in memory).

Two on-disk formats exist:
  * V2: 6-byte signature 07 08 'V' '2' 08 07, u32le aes_size, u32le xor_size, 1 spare byte, then
    AES-128-ECB over the first aes_size bytes (padded to a 16-byte boundary), an untouched middle,
    and a tail of xor_size bytes XOR-ed with one byte.
  * Legacy: the whole file XOR-ed with one byte.
The result is accepted only when it carries a known image signature, so a wrong key never yields junk.
"""
from __future__ import annotations

import struct
import subprocess
V2_MAGIC = b"\x07\x08V2\x08\x07"
V2_HEADER = 15

try:
    from Crypto.Cipher import AES as _AES

    def _ecb_decrypt(key, data):
        return _AES.new(key, _AES.MODE_ECB).decrypt(data)
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def _ecb_decrypt(key, data):
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return decryptor.update(data) + decryptor.finalize()

SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"BM", b"wxgf")


def looks_like_image(data):
    return data[:3] == b"\xff\xd8\xff" or data[:4] in (b"\x89PNG", b"GIF8", b"wxgf") or (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def xor_bytes(data, key):
    return data.translate(bytes(b ^ key for b in range(256)))


def decode_v2(data, aes_key, xor_key):
    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    aligned = aes_size // 16 * 16 + 16
    end_aes = V2_HEADER + aligned
    if aes_key is None or len(aes_key) != 16 or end_aes > len(data) or xor_size > len(data) - end_aes:
        return None
    head = _ecb_decrypt(aes_key, data[V2_HEADER:end_aes])[:aes_size]
    middle_end = len(data) - xor_size
    tail = xor_bytes(data[middle_end:], xor_key) if xor_size else b""
    return head + data[end_aes:middle_end] + tail


def decode_legacy(data, xor_key):
    if not data:
        return None
    candidates = [xor_key] if xor_key is not None else []
    for first in (0xFF, 0x89, 0x47, 0x52):  # jpeg, png, gif, riff
        candidates.append(data[0] ^ first)
    for key in dict.fromkeys(candidates):
        plain = xor_bytes(data[:16], key)
        if looks_like_image(plain):
            return xor_bytes(data, key)
    return None


def decode_dat(data, aes_key=None, xor_key=None):
    """Return the plain image bytes, or None when the file cannot be decoded with the given keys."""
    if data[:6] == V2_MAGIC:
        plain = decode_v2(data, aes_key, xor_key if xor_key is not None else 0)
    elif looks_like_image(data[:16]):
        plain = data
    else:
        plain = decode_legacy(data, xor_key)
    return plain if plain is not None and looks_like_image(plain[:16]) else None


def wxgf_to_jpeg(data, ffmpeg):
    """WeChat's 'wxgf' wraps an HEVC still image; re-encode its first frame as JPEG with ffmpeg."""
    start = data.find(b"\x00\x00\x00\x01")
    if start < 0 or not ffmpeg:
        return None
    try:
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-f", "hevc", "-i", "pipe:0", "-frames:v", "1", "-f", "image2pipe",
             "-c:v", "mjpeg", "-q:v", "2", "pipe:1"],
            input=data[start:], capture_output=True, timeout=60, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and result.stdout[:3] == b"\xff\xd8\xff" else None

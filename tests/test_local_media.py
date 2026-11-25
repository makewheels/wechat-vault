import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_vault.local_media import V2, decode_image


def encrypted_image(key, xor):
    first = b"\xff\xd8\xff\xe0" + b"x" * 28
    rest = b"raw middle"
    tail = b"last segment\xff\xd9"
    padded = first + bytes([16]) * 16
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    data = V2 + struct.pack("<II", len(first), len(tail)) + b"\x01"
    data += enc.update(padded) + enc.finalize() + rest + bytes(b ^ xor for b in tail)
    return data, first + rest + tail


def test_v2_image_decrypt_preserves_aes_raw_and_xor_segments():
    key = b"0123456789abcdef"
    encrypted, expected = encrypted_image(key, 0x42)
    assert decode_image(encrypted, key, 0x42) == (expected, "jpg")


def test_wrong_key_or_xor_fails_closed():
    encrypted, _ = encrypted_image(b"0123456789abcdef", 0x42)
    with pytest.raises(ValueError):
        decode_image(encrypted, b"fedcba9876543210", 0x42)
    with pytest.raises(ValueError):
        decode_image(encrypted, b"0123456789abcdef", 0x43)

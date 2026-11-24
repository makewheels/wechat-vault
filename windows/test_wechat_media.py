import struct
import unittest

import wechat_media as wm

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(200)) * 3
AES_KEY = b"0123456789abcdef"


def encode_v2(plain, aes_key, xor_key, xor_size):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    aes_size = 64
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain[:aes_size]) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    head = encryptor.update(padded) + encryptor.finalize()
    middle = plain[aes_size:len(plain) - xor_size]
    tail = wm.xor_bytes(plain[len(plain) - xor_size:], xor_key)
    return wm.V2_MAGIC + struct.pack("<II", aes_size, xor_size) + b"\x00" + head + middle + tail


class DecodeDatTest(unittest.TestCase):
    def test_v2_roundtrip(self):
        data = encode_v2(JPEG, AES_KEY, 0x1E, 100)
        self.assertEqual(wm.decode_dat(data, AES_KEY, 0x1E), JPEG)

    def test_v2_wrong_key_is_rejected(self):
        data = encode_v2(JPEG, AES_KEY, 0x1E, 100)
        self.assertIsNone(wm.decode_dat(data, b"fedcba9876543210", 0x1E))
        self.assertIsNone(wm.decode_dat(data, None, 0x1E))

    def test_legacy_xor_derives_key_from_first_byte(self):
        data = wm.xor_bytes(JPEG, 0x1E)
        self.assertEqual(wm.decode_dat(data), JPEG)

    def test_unknown_content_is_rejected(self):
        self.assertIsNone(wm.decode_dat(b"not an image at all" * 4))


if __name__ == "__main__":
    unittest.main()

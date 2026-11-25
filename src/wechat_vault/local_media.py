"""Archive locally available Mac media, decrypting images before private OSS upload."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import struct
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config
from .store.oss import ObjectStore

V2 = bytes([7, 8, 86, 50, 8, 7])


def image_ext(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"wxgf"):
        return "wxgf"
    if data.startswith(b"BM"):
        return "bmp"
    if data.lstrip().startswith(b"<p>") and b"</p>" in data[-64:]:
        data.decode("utf-8")
        return "html"
    if data[4:8] == b"ftyp":
        return "heic"
    return None


def aes_head(data: bytes, key: bytes) -> tuple[bytes, bytes, int]:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(data) < 15 or data[:6] != V2:
        raise ValueError("unsupported image container")
    size, tail = struct.unpack_from("<II", data, 6)
    size = (size // 16 + 1) * 16
    if size > len(data) - 15 or tail > len(data) - 15 - size:
        raise ValueError("invalid image lengths")
    decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = decoder.update(data[15 : 15 + size]) + decoder.finalize()
    padding = plain[-1]
    if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:
        raise ValueError("image key mismatch")
    return plain[:-padding], data[15 + size :], tail


def infer_xor(paths, key: bytes) -> int:
    votes = Counter()
    for p in paths:
        if p.suffix.lower() != ".dat":
            continue
        try:
            data = p.read_bytes()
            if data[:6] != V2:
                continue
            plain, rest, tail = aes_head(data, key)
            if plain.startswith(b"\xff\xd8\xff") and tail >= 2:
                candidate = rest[-1] ^ 0xD9
                if rest[-2] ^ candidate == 0xFF:
                    votes[candidate] += 1
            if sum(votes.values()) >= 20:
                break
        except (OSError, ValueError):
            continue
    if not votes or votes.most_common(1)[0][1] < 3:
        raise ValueError("Cannot validate image XOR key from JPEG samples")
    return votes.most_common(1)[0][0]


def decode_image(data: bytes, key: bytes, xor: int) -> tuple[bytes, str]:
    if image_ext(data):
        return data, image_ext(data)
    if data[:6] == V2:
        first, rest, tail = aes_head(data, key)
        plain = first + (rest[:-tail] + bytes(b ^ xor for b in rest[-tail:]) if tail else rest)
    else:
        # Older .dat images use one byte XOR. Validate the inferred file signature.
        found = None
        for magic in (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF"):
            candidate = data[0] ^ magic[0] if data else 0
            if bytes(b ^ candidate for b in data[: len(magic)]) == magic:
                found = candidate
                break
        if found is None:
            raise ValueError("unsupported image encryption")
        plain = bytes(b ^ found for b in data)
    ext = image_ext(plain)
    if not ext:
        raise ValueError("decrypted image signature invalid")
    if ext == "jpg" and b"\xff\xd9" not in plain[-32:]:
        # Some WeChat JPEGs have trailers after the end marker.
        import io

        from PIL import Image

        try:
            image = Image.open(io.BytesIO(plain))
            image.load()
        except (OSError, ValueError) as exc:
            raise ValueError("decrypted JPEG is incomplete") from exc
    return plain, ext


def archive(root: Path, account: str, device: str, workers=12, limit=None):
    config = Config.from_env()
    config.require_cloud()
    oss = ObjectStore(config)
    key = os.environ["WECHAT_IMAGE_AES_KEY"].encode("ascii")
    if len(key) != 16:
        raise ValueError("Expected 16-character image key")
    paths = sorted(
        p for p in (root / "msg").rglob("*") if p.is_file() and not p.name.startswith(".")
    )
    xor = infer_xor(paths, key)
    if limit:
        paths = paths[:limit]
    existing = set(oss.list_keys("media/sha256/"))
    lock = threading.Lock()
    stats = Counter()
    prefix = f"manifests/{account}/{device}"

    def one(p):
        rel = str(p.relative_to(root))
        try:
            if p.suffix.lower() == ".dat":
                data, ext = decode_image(p.read_bytes(), key, xor)
                sha = hashlib.sha256(data).hexdigest()
                size = len(data)
            else:
                ext = p.suffix.lower().lstrip(".") or "bin"
                digest = hashlib.sha256()
                with p.open("rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(block)
                sha, size, data = digest.hexdigest(), p.stat().st_size, None
                if ext == "mp4":
                    with p.open("rb") as f:
                        head = f.read(16)
                    if head[4:8] != b"ftyp":
                        detected = image_ext(head)
                        if not detected:
                            raise ValueError("video container is not a clear MP4")
                        ext = detected
            obj = oss.key(sha, ext)
            mime = mimetypes.guess_type("file." + ext)[0] or "application/octet-stream"
            # Serialize equal objects by digest stripe to avoid concurrent duplicate uploads.
            stripe = stripes[int(sha[:4], 16) % len(stripes)]
            with stripe:
                if obj not in existing:
                    if data is None:
                        result = oss.put_file(p, obj, mime, sha)
                    else:
                        result = oss.bucket.put_object(
                            obj, data, headers={"Content-Type": mime, "x-oss-meta-sha256": sha}
                        )
                    # oss2 verifies the local/server CRC64 for the PUT body.
                    if result.status != 200 or result.crc is None:
                        raise ValueError("uploaded media CRC verification unavailable")
                    with lock:
                        existing.add(obj)
                        stats["uploaded"] += 1
                else:
                    with lock:
                        stats["present"] += 1
            return {
                "path": rel,
                "key": obj,
                "sha256": sha,
                "size": size,
                "ext": ext,
                "state": "decoded_container" if ext == "wxgf" else "done",
            }
        except Exception as e:
            # Do not print SDK exception bodies, which can contain signed requests.
            return {
                "path": rel,
                "state": "failed",
                "error": str(e) if isinstance(e, ValueError) else type(e).__name__,
            }

    stripes = [threading.Lock() for _ in range(256)]
    with tempfile.TemporaryDirectory(prefix="wechat-media-index-") as directory:
        index = Path(directory) / "index.jsonl"
        with index.open("w") as f, ThreadPoolExecutor(max_workers=workers) as pool:
            for i, row in enumerate(pool.map(one, paths), 1):
                stats[row["state"]] += 1
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                if i % 500 == 0:
                    print(f"[local-media] files={i}/{len(paths)} {dict(stats)}", flush=True)
        digest = hashlib.sha256(index.read_bytes()).hexdigest()
        obj = f"{prefix}/media-{digest}.jsonl"
        oss.put_file(index, obj, "application/x-ndjson", digest)
        summary = {
            "account": account,
            "device": device,
            "files": len(paths),
            "counts": dict(stats),
            "index_key": obj,
            "partial": bool(limit),
        }
        oss.bucket.put_object(
            f"{prefix}/media-latest.json",
            json.dumps(summary).encode(),
            headers={"Content-Type": "application/json"},
        )
        print("[local-media] " + json.dumps(summary), flush=True)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    result = archive(args.account_root, args.account, args.device, args.workers, args.limit)
    return int(bool(result["counts"].get("failed")))


if __name__ == "__main__":
    raise SystemExit(main())

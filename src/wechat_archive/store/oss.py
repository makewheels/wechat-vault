"""OSS object access for content-addressed media."""

from __future__ import annotations

from pathlib import Path

from ..config import Config


class ObjectStore:
    def __init__(self, config: Config):
        import oss2

        auth = oss2.Auth(config.ak_id, config.ak_secret)
        self.bucket = oss2.Bucket(auth, config.oss_endpoint, config.bucket)
        self.config = config

    @staticmethod
    def key(sha256: str, ext: str) -> str:
        return f"media/sha256/{sha256}.{ext.lstrip('.')}"

    def exists(self, key: str) -> bool:
        import oss2

        try:
            self.bucket.head_object(key)
            return True
        except (oss2.exceptions.NoSuchKey, oss2.exceptions.NotFound):
            return False

    def size(self, key: str) -> int | None:
        import oss2

        try:
            result = self.bucket.head_object(key)
        except (oss2.exceptions.NoSuchKey, oss2.exceptions.NotFound):
            return None
        return int(result.content_length)

    def put_file(self, path: str | Path, key: str, mime: str, sha256: str = "") -> None:
        headers = {"Content-Type": mime}
        if sha256:
            headers["x-oss-meta-sha256"] = sha256
        self.bucket.put_object_from_file(key, str(path), headers=headers)
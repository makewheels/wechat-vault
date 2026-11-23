"""Read the Windows staging package produced by HANDOFF-WINDOWS.md."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..model import Conversation, Message, conv_key, normalize_session


class StagingError(RuntimeError):
    pass


class StagingPackage:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.manifest_path = self.root / "manifest.json"
        self.messages_dir = self.root / "messages"
        self.media_dir = self.root / "media"
        self._manifest: dict[str, Any] | None = None
        self._media_index: dict[str, Path] | None = None

    @property
    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            if not self.manifest_path.exists():
                raise StagingError(f"manifest.json 不存在: {self.manifest_path}")
            self._manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            version = self._manifest.get("schema_version")
            if version != 1:
                raise StagingError(f"不支持的 schema_version={version!r}（期望 1）")
            if not self._manifest.get("account_id"):
                raise StagingError("manifest.account_id 缺失")
        return self._manifest

    @property
    def account_id(self) -> str:
        return str(self.manifest["account_id"])

    @property
    def source(self) -> str:
        return str(self.manifest.get("source") or "win")

    @classmethod
    def from_oss(cls, oss, prefix: str, root: str | Path) -> StagingPackage:
        """Materialize staging metadata (not media) from OSS into a temp dir."""
        root = Path(root)
        (root / "messages").mkdir(parents=True, exist_ok=True)
        (root / "media").mkdir(parents=True, exist_ok=True)
        prefix = prefix.rstrip("/")
        oss.get_to_file(f"{prefix}/manifest.json", root / "manifest.json")
        for name in ("sessions.jsonl", "contacts.jsonl", "errors.jsonl", "state.json"):
            key = f"{prefix}/{name}"
            if oss.exists(key):
                oss.get_to_file(key, root / name)
        for key in oss.list_keys(f"{prefix}/messages/"):
            if key.endswith(".jsonl"):
                oss.get_to_file(key, root / "messages" / Path(key).name)
        return cls(root)

    def sessions(self) -> Iterator[Conversation]:
        path = self.root / "sessions.jsonl"
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                talker = str(record.get("talker") or "")
                if not talker:
                    continue
                yield normalize_session(record, account_id=self.account_id)

    def messages(self, *, limit: int | None = None) -> Iterator[Message]:
        emitted = 0
        for session in self.sessions():
            path = self.messages_dir / f"{conv_key(session.talker)}.jsonl"
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    record.setdefault("conv_id", f"{self.account_id}:{session.talker}")
                    record.setdefault("talker", session.talker)
                    record.setdefault("src", self.source)
                    yield Message.from_record(record, default_src=self.source)
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        return

    def media_path(self, sha256: str, ext: str | None = None) -> Path | None:
        if not sha256:
            return None
        if ext:
            candidate = self.media_dir / f"{sha256}.{ext.lstrip('.')}"
            if candidate.exists():
                return candidate
        index = self._index_media()
        return index.get(sha256)

    def _index_media(self) -> dict[str, Path]:
        if self._media_index is not None:
            return self._media_index
        index: dict[str, Path] = {}
        if self.media_dir.exists():
            with os.scandir(self.media_dir) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    sha = name.split(".", 1)[0]
                    if len(sha) == 64:
                        index.setdefault(sha, Path(entry.path))
        self._media_index = index
        return index

    def counts(self) -> dict[str, int]:
        messages = 0
        sessions = 0
        for _ in self.sessions():
            sessions += 1
        for _ in self.messages():
            messages += 1
        return {"sessions": sessions, "messages": messages}
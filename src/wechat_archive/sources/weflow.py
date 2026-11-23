"""Read-only client for WeFlow's local HTTP API (Mac/Windows)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class WeFlowAPIError(RuntimeError):
    status_code: int
    body: str

    def __str__(self) -> str:
        return f"WeFlow API failed: HTTP {self.status_code}: {self.body[:300]}"


class WeFlowClient:
    def __init__(self, base_url: str = "http://127.0.0.1:5031", access_token: str = "", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token or ""
        self.timeout = timeout

    def health(self) -> bool:
        try:
            data = self._get_json("/health", auth=False, timeout=8)
        except Exception:
            return False
        return str(data.get("status", "")).lower() in {"ok", "healthy", "up"} or bool(data)

    def sessions(self, limit: int = 5000) -> list[dict[str, Any]]:
        return list(self._get_json("/api/v1/sessions", {"limit": limit}).get("sessions", []))

    def contacts(self, limit: int = 5000) -> list[dict[str, Any]]:
        return list(self._get_json("/api/v1/contacts", {"limit": limit}).get("contacts", []))

    def messages(
        self,
        talker: str,
        *,
        limit: int = 200,
        offset: int = 0,
        media: bool = True,
        start: str | int | None = None,
        end: str | int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        params: dict[str, Any] = {
            "talker": talker,
            "limit": limit,
            "offset": offset,
            "media": "1" if media else "0",
        }
        if start not in (None, ""):
            params["start"] = start
        if end not in (None, ""):
            params["end"] = end
        data = self._get_json("/api/v1/messages", params)
        page = list(data.get("messages", []))
        has_more = bool(data.get("hasMore", False))
        if "hasMore" not in data:
            has_more = len(page) >= limit
        return page, has_more

    def iter_messages(
        self,
        talker: str,
        *,
        page_size: int = 200,
        media: bool = True,
        max_pages: int | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        offset = 0
        pages = 0
        while True:
            page, has_more = self.messages(
                talker, limit=page_size, offset=offset, media=media
            )
            if not page:
                break
            yield page
            pages += 1
            if max_pages is not None and pages >= max_pages:
                break
            if not has_more:
                break
            offset += len(page)

    def download_media(self, message: dict[str, Any], dest: str | Path) -> bool:
        """Download decrypted media bytes to dest. Token is never persisted."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        media_url = message.get("mediaUrl")
        ref = message.get("mediaFileName") or message.get("fileName") or ""
        if isinstance(media_url, str) and media_url:
            request = self._request_from_url(media_url)
        elif ref:
            request = self._request(f"/api/v1/media/{urllib.parse.quote(str(ref), safe='/')}")
        else:
            return False
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, 60)) as response, dest.open("wb") as fh:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            return False
        return dest.exists() and dest.stat().st_size > 0

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        request = self._request(path, params, auth=auth)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise WeFlowAPIError(exc.code, body) from exc
        return json.loads(raw) if raw else {}

    def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> urllib.request.Request:
        merged = dict(params or {})
        if auth and self.access_token:
            merged.setdefault("access_token", self.access_token)
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = urllib.parse.urlencode(merged)
        if query:
            url = f"{url}?{query}"
        headers = {"Accept": "application/json"}
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return urllib.request.Request(url, headers=headers)

    def _request_from_url(self, media_url: str) -> urllib.request.Request:
        parsed = urllib.parse.urlparse(media_url)
        if parsed.scheme and parsed.netloc:
            params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            params.pop("access_token", None)
            return self._request(parsed.path, params)
        return self._request(media_url)
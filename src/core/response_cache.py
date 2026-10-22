"""Small, session-partitioned disk cache for refresh-time GET revalidation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class CachedResponse:
    body: bytes
    headers: dict[str, str]
    status: int = 200


class ResponseCache:
    """Bounded cache with atomic entry publication; never stores request credentials."""

    def __init__(self, directory: str | Path, max_bytes: int = 256 * 1024 * 1024,
                 max_response_bytes: int = 2 * 1024 * 1024):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.max_response_bytes = max_response_bytes
        self._lock = threading.RLock()

    @staticmethod
    def _headers(headers: Mapping[str, str] | None) -> dict[str, str]:
        return {str(key).lower(): str(value).strip() for key, value in (headers or {}).items()}

    @staticmethod
    def _digest(value: object) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return hashlib.sha256(raw).hexdigest()

    def _path(self, url: str, session: str, request_headers: Mapping[str, str]) -> Path:
        headers = self._headers(request_headers)
        # Hash all request-header values, including those named by Vary, without
        # persisting possibly sensitive values such as Authorization or Cookie.
        representation = {key: self._digest(value) for key, value in sorted(headers.items())}
        return self.directory / (self._digest([url, session, representation]) + ".json")

    @staticmethod
    def _safe_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
        safe = ResponseCache._headers(headers)
        for name in ("set-cookie", "set-cookie2", "authorization", "proxy-authenticate",
                     "www-authenticate", "location"):
            safe.pop(name, None)
        return safe

    def _read(self, path: Path, url: str, session: str, request_headers: Mapping[str, str]):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            headers = self._headers(request_headers)
            if record.get("url_hash") != self._digest(url) or record.get("session") != session:
                return None
            if any(self._digest(headers.get(name, "")) != fingerprint
                   for name, fingerprint in record.get("vary", {}).items()):
                return None
            body = base64.b64decode(record["body"], validate=True)
            if len(body) > self.max_response_bytes:
                return None
            os.utime(path, None)
            return CachedResponse(body, self._headers(record.get("headers")), int(record.get("status", 200)))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def load(self, url: str, session: str, request_headers: Mapping[str, str]) -> CachedResponse | None:
        with self._lock:
            return self._read(self._path(url, session, request_headers), url, session, request_headers)

    def store(self, url: str, session: str, request_headers: Mapping[str, str],
              response_headers: Mapping[str, str], body: bytes, status: int = 200) -> bool:
        headers = self._safe_response_headers(response_headers)
        if status != 200 or len(body) > self.max_response_bytes:
            return False
        directives = {part.strip().split("=", 1)[0].lower() for part in headers.get("cache-control", "").split(",")}
        vary_names = [name.strip().lower() for name in headers.get("vary", "").split(",") if name.strip()]
        path = self._path(url, session, request_headers)
        if "no-store" in directives or "*" in vary_names:
            with self._lock:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
        req = self._headers(request_headers)
        record = {
            "url_hash": self._digest(url),
            "session": session,
            "headers": headers,
            "status": status,
            "vary": {name: self._digest(req.get(name, "")) for name in vary_names},
            "body": base64.b64encode(body).decode("ascii"),
            "saved_at": time.time(),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with self._lock:
            fd, temp_name = tempfile.mkstemp(prefix=".response-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, path)
                self._evict(path)
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        return True

    def revalidate_304(self, url: str, session: str, request_headers: Mapping[str, str],
                       response_headers: Mapping[str, str]) -> CachedResponse | None:
        """Merge end-to-end 304 metadata into a matching stored response."""
        with self._lock:
            path = self._path(url, session, request_headers)
            existing = self._read(path, url, session, request_headers)
            if not existing:
                return None
            merged = dict(existing.headers)
            merged.update(self._safe_response_headers(response_headers))
            directives = {part.strip().split("=", 1)[0].lower()
                          for part in merged.get("cache-control", "").split(",")}
            if "no-store" in directives or "*" in {v.strip() for v in merged.get("vary", "").split(",")}:
                path.unlink(missing_ok=True)
                return CachedResponse(existing.body, merged)
            # A 304 may change Vary or Cache-Control; re-store so future matching obeys it.
            self.store(url, session, request_headers, merged, existing.body, status=200)
            return CachedResponse(existing.body, merged)

    def _evict(self, protected: Path) -> None:
        files = list(self.directory.glob("*.json"))
        sized = []
        total = 0
        for path in files:
            try:
                stat = path.stat()
                sized.append((stat.st_mtime, path, stat.st_size))
                total += stat.st_size
            except OSError:
                continue
        for _, path, size in sorted(sized):
            if total <= self.max_bytes:
                break
            if path == protected and len(sized) == 1:
                continue
            try:
                path.unlink()
                total -= size
            except OSError:
                pass

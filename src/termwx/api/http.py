"""Shared HTTP client with an on-disk JSON cache used as an offline fallback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx

USER_AGENT = "termwx/0.1 (terminal weather; https://github.com/termwx)"


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "termwx"


class Http:
    def __init__(self, cache_dir: Path | None = None, timeout: float = 15.0):
        self.cache_dir = cache_dir or default_cache_dir()
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def _cache_path(self, url: str, params: dict | None) -> Path:
        key = url + "?" + json.dumps(params or {}, sort_keys=True)
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    async def get_json(
        self, url: str, params: dict | None = None, headers: dict | None = None, cache: bool = True
    ) -> tuple[Any, bool]:
        """Return (data, stale). Falls back to the last cached copy on network errors."""
        path = self._cache_path(url, params)
        try:
            resp = await self.client.get(url, params=params, headers=headers)
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} from {httpx.URL(url).host}", resp.status_code)
            data = resp.json()
        except FetchError as e:
            # 4xx means the request itself is wrong; a cached copy won't help.
            if e.status and e.status < 500:
                raise
            return self._from_cache(path, e)
        except (httpx.HTTPError, ValueError) as e:
            return self._from_cache(path, e)
        if cache:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                tmp.replace(path)
            except OSError:
                pass
        return data, False

    def _from_cache(self, path: Path, err: Exception) -> tuple[Any, bool]:
        try:
            return json.loads(path.read_text()), True
        except (OSError, ValueError):
            msg = str(err) or err.__class__.__name__
            raise FetchError(msg) from err

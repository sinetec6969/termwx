"""Shared HTTP client with an on-disk JSON cache used as an offline fallback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx

# NOAA/NWS and the Aviation Weather Center ask for a descriptive, contactable User-Agent.
USER_AGENT = "termwx/0.3 (terminal weather; https://github.com/sinetec6969/termwx)"


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
        """Return (data, stale). Falls back to the last cached copy on network errors.

        HTTP 204 (a valid request with nothing to return, e.g. no recent METAR)
        comes back as (None, False).
        """
        return await self._get(url, params, headers, cache, lambda r: r.json())

    async def get_text_cached(self, url: str, params: dict | None = None, headers: dict | None = None) -> tuple[str, bool]:
        """Like get_json for plain-text products (e.g. the winds-aloft bulletin); 204 gives ("", False)."""
        text, stale = await self._get(url, params, headers, True, lambda r: r.text)
        return text or "", stale

    async def _get(self, url, params, headers, cache, decode) -> tuple[Any, bool]:
        path = self._cache_path(url, params)
        try:
            resp = await self.client.get(url, params=params, headers=headers)
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} from {httpx.URL(url).host}", resp.status_code)
            data = None if resp.status_code == 204 else decode(resp)
        except FetchError as e:
            # 4xx means the request itself is wrong; a cached copy won't help.
            # 429 (rate limited) is temporary, so treat it like an outage.
            if e.status and e.status < 500 and e.status != 429:
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

    async def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        """Fetch a non-JSON body (e.g. RSS). Not cached."""
        try:
            resp = await self.client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise FetchError(str(e) or e.__class__.__name__) from e
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} from {httpx.URL(url).host}", resp.status_code)
        return resp.text

    def _from_cache(self, path: Path, err: Exception) -> tuple[Any, bool]:
        try:
            return json.loads(path.read_text()), True
        except (OSError, ValueError):
            msg = str(err) or err.__class__.__name__
            raise FetchError(msg) from err

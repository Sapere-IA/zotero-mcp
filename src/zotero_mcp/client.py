"""HTTP access to Zotero.

Reads go to the Zotero desktop app's local API (http://127.0.0.1:23119/api): fast, no key, always
the library as the user sees it. Writes go to the zotero.org web API (needs an API key) and reach
the desktop app through Zotero sync. The local API is read-only by design, so this split is forced.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

import httpx

from .config import Config

KEY_RE = re.compile(r"^[A-Z0-9]{8}$")

LOCAL_HELP = (
    "Could not reach the Zotero desktop app. Make sure Zotero 7 or later is open, and that "
    "Settings → Advanced → 'Allow other applications on this computer to communicate with Zotero' "
    "is checked (in Spanish: Configuración → Avanzado → 'Permitir que otras aplicaciones de este "
    "equipo se comuniquen con Zotero')."
)


class ZoteroError(Exception):
    """An error whose message is meant to be shown to the user as-is."""


def check_key(key: str) -> str:
    """Zotero keys are 8 uppercase letters/digits. Validating them also keeps them safe to put in URLs."""
    k = (key or "").strip().upper()
    if not KEY_RE.match(k):
        raise ZoteroError(f"'{key}' is not a Zotero key (expected 8 letters/digits, e.g. 'AB12CD34').")
    return k


def chunks(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class ZoteroClient:
    def __init__(
        self,
        config: Config,
        *,
        local_transport: httpx.BaseTransport | None = None,
        web_transport: httpx.BaseTransport | None = None,
    ):
        self.config = config
        headers = {"Zotero-API-Version": "3"}
        self._local = httpx.Client(
            base_url=config.local_url, timeout=config.timeout, transport=local_transport, headers=headers
        )
        web_headers = dict(headers)
        if config.api_key:
            web_headers["Zotero-API-Key"] = config.api_key
        self._web = httpx.Client(
            base_url=config.web_url, timeout=config.timeout, transport=web_transport, headers=web_headers
        )
        self._key_info: dict | None = None

    # ------------------------------------------------------------------ local (read)

    def local_get(self, path: str, **params: Any) -> Any:
        try:
            r = self._local.get(path, params=_clean(params))
        except httpx.TimeoutException:
            raise ZoteroError("Zotero took too long to answer. Is it busy (e.g. syncing or indexing)? Try again.")
        except httpx.TransportError:
            raise ZoteroError(LOCAL_HELP)
        if r.status_code == 404:
            return None
        if r.status_code == 403:
            raise ZoteroError(LOCAL_HELP)
        if r.status_code >= 400:
            raise ZoteroError(f"Zotero answered {r.status_code} for {path}: {r.text[:300]}")
        return r.json()

    def local_get_all(self, path: str, **params: Any) -> list[dict]:
        """Follow pagination until every result is fetched."""
        out: list[dict] = []
        start = 0
        while True:
            page = self.local_get(path, limit=100, start=start, **params) or []
            out.extend(page)
            if len(page) < 100:
                return out
            start += 100

    # ------------------------------------------------------------------ web (write)

    def key_info(self) -> dict:
        if not self.config.api_key:
            raise ZoteroError(
                "Changing the library needs a Zotero API key and none is configured. Create one at "
                "https://www.zotero.org/settings/keys/new (personal library: allow library access + "
                "write access) and set it as ZOTERO_API_KEY in the MCP server config."
            )
        if self._key_info is None:
            r = self._web_request("GET", "/keys/current")
            if r.status_code == 403:
                raise ZoteroError("Zotero rejected the API key (ZOTERO_API_KEY). It may be revoked or mistyped.")
            self._raise_for(r, "checking the API key")
            self._key_info = r.json()
        return self._key_info

    @property
    def prefix(self) -> str:
        info = self.key_info()
        if not info.get("access", {}).get("user", {}).get("write"):
            raise ZoteroError(
                "The Zotero API key has no write access. Edit it at https://www.zotero.org/settings/keys "
                "and enable 'Allow write access' for the personal library."
            )
        return f"/users/{info['userID']}"

    @property
    def user_id(self) -> int:
        return int(self.key_info()["userID"])

    def web_get(self, path: str, **params: Any) -> Any:
        r = self._web_request("GET", path, params=_clean(params))
        if r.status_code == 404:
            return None
        self._raise_for(r, path)
        return r.json()

    def web_items(self, keys: list[str]) -> dict[str, dict]:
        """Current server copy of items, including trashed ones (needed to undo a trash)."""
        out: dict[str, dict] = {}
        for batch in chunks(sorted(set(keys)), 50):
            for it in self.web_get(f"{self.prefix}/items", itemKey=",".join(batch), includeTrashed=1, limit=50) or []:
                out[it["key"]] = it
        return out

    def web_collections(self, keys: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for batch in chunks(sorted(set(keys)), 50):
            for c in self.web_get(f"{self.prefix}/collections", collectionKey=",".join(batch), limit=50) or []:
                out[c["key"]] = c
        return out

    def web_children(self, key: str) -> list[dict]:
        out, start = [], 0
        while True:
            page = self.web_get(f"{self.prefix}/items/{check_key(key)}/children", limit=100, start=start) or []
            out.extend(page)
            if len(page) < 100:
                return out
            start += 100

    def web_write(self, kind: str, objects: list[dict]) -> tuple[dict[int, dict], dict[int, str]]:
        """POST up to 50 objects to /items or /collections (create, or partial update when key+version given).

        Returns ({index: saved object}, {index: error message}).
        """
        assert kind in ("items", "collections")
        ok: dict[int, dict] = {}
        failed: dict[int, str] = {}
        for offset in range(0, len(objects), 50):
            batch = objects[offset : offset + 50]
            r = self._web_request("POST", f"{self.prefix}/{kind}", json=batch)
            self._raise_for(r, f"saving {kind}")
            res = r.json()
            for idx, obj in (res.get("successful") or {}).items():
                ok[offset + int(idx)] = obj
            for idx, key in (res.get("unchanged") or {}).items():
                ok[offset + int(idx)] = {"key": key, "unchanged": True}
            for idx, err in (res.get("failed") or {}).items():
                code, msg = err.get("code"), err.get("message", "")
                if code == 412:
                    msg = "it was changed in Zotero after it was read; nothing was written for it. Try again."
                failed[offset + int(idx)] = msg
        return ok, failed

    def web_delete_collection(self, key: str, version: int) -> None:
        r = self._web_request(
            "DELETE",
            f"{self.prefix}/collections/{check_key(key)}",
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        if r.status_code == 412:
            raise ZoteroError("The folder was changed in Zotero after it was read. Try again.")
        self._raise_for(r, "deleting the folder")

    # ------------------------------------------------------------------ helpers

    def _web_request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(4):
            try:
                r = self._web.request(method, path, **kw)
            except httpx.TimeoutException:
                raise ZoteroError("zotero.org took too long to answer. Try again in a moment.")
            except httpx.TransportError:
                raise ZoteroError("Could not reach zotero.org. Check the internet connection.")
            if r.status_code in (429, 503) and attempt < 3:
                time.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 30))
                continue
            if r.headers.get("Backoff"):
                time.sleep(min(float(r.headers["Backoff"]), 30))
            return r
        return r

    @staticmethod
    def _raise_for(r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            raise ZoteroError(f"zotero.org answered {r.status_code} while {what}: {r.text[:300]}")

    def close(self) -> None:
        self._local.close()
        self._web.close()


def _clean(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None and v != ""}

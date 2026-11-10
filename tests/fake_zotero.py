"""In-memory stand-in for Zotero: the desktop app's local API and the zotero.org web API share one
library, as if sync were instant. Mimics the behaviors the server relies on: versions and 412
conflicts, partial updates through POST, rejecting fields invalid for the item type."""

from __future__ import annotations

import copy
import itertools
import json
from urllib.parse import parse_qs

import httpx

USER_ID = 4242
FIELDS = {
    "journalArticle": ["title", "abstractNote", "publicationTitle", "volume", "issue", "pages", "date",
                       "language", "DOI", "ISSN", "url", "extra"],
    "book": ["title", "abstractNote", "publisher", "place", "date", "ISBN", "language", "url", "extra"],
    "thesis": ["title", "abstractNote", "university", "date", "language", "url", "extra"],
    "attachment": ["title", "contentType", "filename", "url"],
    "note": ["note"],
}
_keys = (f"K{n:07d}" for n in itertools.count(1))


class FakeZotero:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.fulltext: dict[str, str] = {}
        self.version = 1
        self.requests: list[tuple[str, str]] = []
        self.write_access = True

    # ------------------------------------------------------------------ building the library

    def add_collection(self, name: str, parent: str | None = None) -> str:
        key = next(_keys)
        self.collections[key] = {"key": key, "version": self._bump(),
                                 "data": {"key": key, "name": name, "parentCollection": parent or False, "relations": {}}}
        return key

    def add_item(self, item_type: str = "journalArticle", *, key: str | None = None, **fields) -> str:
        key = key or next(_keys)
        data = {"key": key, "itemType": item_type, **{f: "" for f in FIELDS[item_type]}}
        if item_type not in ("note", "attachment"):
            data["creators"] = []
        data.update({"tags": [], "collections": [], "relations": {}, "dateAdded": "2024-01-01T00:00:00Z",
                     "dateModified": "2024-01-01T00:00:00Z"})
        data.update(copy.deepcopy(fields))
        self.items[key] = {"key": key, "version": self._bump(), "data": data}
        return key

    def add_pdf(self, parent: str, text: str | None = None) -> str:
        k = self.add_item("attachment", parentItem=parent, title="Full Text PDF", contentType="application/pdf")
        if text is not None:
            self.fulltext[k] = text
        return k

    def _bump(self) -> int:
        self.version += 1
        return self.version

    # ------------------------------------------------------------------ transports

    def local_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda r: self._handle(r, local=True))

    def web_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda r: self._handle(r, local=False))

    def _handle(self, req: httpx.Request, local: bool) -> httpx.Response:
        path = req.url.path
        self.requests.append((req.method, ("local " if local else "web ") + path))
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        if local:
            assert path.startswith("/api/users/0/"), path
            if req.method != "GET":
                return httpx.Response(501, text="local API is read-only")
            parts = path.removeprefix("/api/users/0/").strip("/").split("/")
        else:
            if path == "/keys/current":
                if req.headers.get("Zotero-API-Key") != "secret":
                    return httpx.Response(403, text="Invalid key")
                return httpx.Response(200, json={"userID": USER_ID, "username": "maria",
                                                 "access": {"user": {"library": True, "write": self.write_access}}})
            prefix = f"/users/{USER_ID}/"
            assert path.startswith(prefix), path
            if req.headers.get("Zotero-API-Key") != "secret":
                return httpx.Response(403, text="Forbidden")
            parts = path.removeprefix(prefix).strip("/").split("/")
            if req.method == "POST":
                return self._post(parts[0], json.loads(req.content))
            if req.method == "DELETE":
                return self._delete(parts, req)
        return self._get(parts, q, local)

    # ------------------------------------------------------------------ GET

    def _get(self, parts: list[str], q: dict, local: bool) -> httpx.Response:
        match parts:
            case ["collections"]:
                cols = list(self.collections.values())
                if "collectionKey" in q:
                    cols = [c for c in cols if c["key"] in q["collectionKey"].split(",")]
                return self._page([self._with_meta_col(c) for c in cols], q)
            case ["collections", key, "collections"]:
                return self._page([c for c in self.collections.values() if c["data"]["parentCollection"] == key], q)
            case ["collections", key, "items", *top]:
                items = [i for i in self._live() if key in i["data"].get("collections", [])]
                if top:
                    items = [i for i in items if not i["data"].get("parentItem")]
                return self._page(items, q)
            case ["items"] | ["items", "top"]:
                items = self._live(include_trashed=q.get("includeTrashed") == "1")
                if parts == ["items", "top"]:
                    items = [i for i in items if not i["data"].get("parentItem")]
                if "itemKey" in q:
                    items = [i for i in items if i["key"] in q["itemKey"].split(",")]
                if "tag" in q:
                    items = [i for i in items if q["tag"] in [t["tag"] for t in i["data"].get("tags", [])]]
                if q.get("q"):
                    items = [i for i in items if self._matches(i, q["q"].lower(), q.get("qmode", "titleCreatorYear"))]
                return self._page(items, q)
            case ["items", key]:
                it = self.items.get(key)
                return httpx.Response(200, json=self._with_meta(it)) if it else httpx.Response(404)
            case ["items", key, "children"]:
                return self._page([i for i in self._live() if i["data"].get("parentItem") == key], q)
            case ["items", key, "fulltext"]:
                if key not in self.fulltext:
                    return httpx.Response(404)
                return httpx.Response(200, json={"content": self.fulltext[key], "indexedPages": 3, "totalPages": 3})
            case ["tags"]:
                counts: dict[str, int] = {}
                for i in self._live():
                    for t in i["data"].get("tags", []):
                        counts[t["tag"]] = counts.get(t["tag"], 0) + 1
                return self._page([{"tag": t, "meta": {"numItems": n}} for t, n in counts.items()], q)
        return httpx.Response(404, text=f"no route {parts}")

    def _live(self, include_trashed: bool = False) -> list[dict]:
        return [self._with_meta(i) for i in self.items.values() if include_trashed or not i["data"].get("deleted")]

    def _with_meta(self, it: dict) -> dict:
        it = copy.deepcopy(it)
        names = [c.get("lastName") or c.get("name") for c in it["data"].get("creators", [])]
        it["meta"] = {"creatorSummary": names[0] if names else "", "parsedDate": it["data"].get("date", "")}
        return it

    def _with_meta_col(self, c: dict) -> dict:
        c = copy.deepcopy(c)
        c["meta"] = {"numItems": sum(1 for i in self._live() if c["key"] in i["data"].get("collections", [])
                                     and not i["data"].get("parentItem"))}
        return c

    def _matches(self, it: dict, q: str, qmode: str) -> bool:
        d = it["data"]
        hay = [d.get("title", ""), d.get("date", "")] + [c.get("lastName", "") for c in d.get("creators", [])]
        if qmode == "everything":
            hay += [str(v) for v in d.values()] + [self.fulltext.get(it["key"], "")]
        return any(q in h.lower() for h in hay)

    def _page(self, rows: list, q: dict) -> httpx.Response:
        start, limit = int(q.get("start", 0)), int(q.get("limit", 25))
        return httpx.Response(200, json=rows[start : start + limit], headers={"Total-Results": str(len(rows))})

    # ------------------------------------------------------------------ writes

    def _post(self, kind: str, objects: list[dict]) -> httpx.Response:
        store = self.items if kind == "items" else self.collections
        ok, failed, unchanged = {}, {}, {}
        for i, obj in enumerate(objects):
            obj = copy.deepcopy(obj)
            if "key" in obj:  # partial update
                cur = store.get(obj["key"])
                if cur is None:
                    failed[str(i)] = {"code": 404, "message": "not found"}
                    continue
                if obj.pop("version") != cur["version"]:
                    failed[str(i)] = {"code": 412, "message": "Precondition failed"}
                    continue
                key = obj.pop("key")
                bad = [f for f in obj if f not in cur["data"] and f not in ("deleted", "parentItem", "relations")]
                if bad:
                    failed[str(i)] = {"code": 400, "message": f"'{bad[0]}' is not a valid field for this type"}
                    continue
                if all(cur["data"].get(f) == v for f, v in obj.items()):
                    unchanged[str(i)] = key
                    continue
                cur["data"].update(obj)
                if cur["data"].get("deleted") in (0, False):
                    cur["data"].pop("deleted", None)
                cur["version"] = self._bump()
                ok[str(i)] = copy.deepcopy(cur)
            else:  # create
                if kind == "items":
                    t = obj.pop("itemType")
                    key = self.add_item(t, **obj)
                else:
                    key = self.add_collection(obj["name"], obj.get("parentCollection") or None)
                ok[str(i)] = copy.deepcopy(store[key])
        return httpx.Response(200, json={"successful": ok, "unchanged": unchanged, "failed": failed})

    def _delete(self, parts: list[str], req: httpx.Request) -> httpx.Response:
        assert parts[0] == "collections"
        c = self.collections.get(parts[1])
        if c is None:
            return httpx.Response(404)
        if int(req.headers["If-Unmodified-Since-Version"]) != c["version"]:
            return httpx.Response(412)
        del self.collections[parts[1]]
        return httpx.Response(204)

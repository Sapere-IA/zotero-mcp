"""Look up authoritative metadata on Crossref, mapped to Zotero field names."""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from .client import ZoteroError
from .duplicates import norm_doi

TYPE_MAP = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter": "bookSection",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "posted-content": "preprint",
    "report": "report",
    "dissertation": "thesis",
    "dataset": "dataset",
}
CONTAINER_FIELD = {"journalArticle": "publicationTitle", "conferencePaper": "proceedingsTitle", "bookSection": "bookTitle"}


class Crossref:
    def __init__(self, base_url: str, timeout: float, transport: httpx.BaseTransport | None = None):
        # No contact email in the User-Agent on purpose: it would identify the user to a third party.
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport, headers={"User-Agent": "zotero-mcp/0.1"}
        )

    def by_doi(self, doi: str) -> dict | None:
        doi = norm_doi(doi)
        if not re.match(r"^10\.\d{4,9}/\S+$", doi):
            raise ZoteroError(f"'{doi}' does not look like a DOI (they start with 10.).")
        r = self._get(f"/works/{quote(doi, safe='/')}")
        if r.status_code == 404:
            return None
        return to_zotero(r.json()["message"])

    def search(self, title: str, author: str | None = None, rows: int = 3) -> list[dict]:
        params = {"query.bibliographic": title, "rows": rows}
        if author:
            params["query.author"] = author
        r = self._get("/works", params=params)
        return [to_zotero(m) for m in r.json()["message"]["items"]]

    def _get(self, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.get(path, **kw)
        except httpx.HTTPError:
            raise ZoteroError("Could not reach Crossref (api.crossref.org). Check the internet connection.")
        if r.status_code >= 400 and r.status_code != 404:
            raise ZoteroError(f"Crossref answered {r.status_code}.")
        return r


def to_zotero(m: dict) -> dict:
    item_type = TYPE_MAP.get(m.get("type", ""), "document")
    out: dict = {"itemType": item_type}
    if m.get("title"):
        out["title"] = _clean(m["title"][0])
    creators = []
    for role, ctype in (("author", "author"), ("editor", "editor")):
        for a in m.get(role, []) or []:
            if a.get("family"):
                creators.append({"creatorType": ctype, "firstName": a.get("given", ""), "lastName": a["family"]})
            elif a.get("name"):
                creators.append({"creatorType": ctype, "name": a["name"]})
    if creators:
        out["creators"] = creators
    parts = (m.get("published") or m.get("issued") or {}).get("date-parts", [[None]])[0]
    if parts and parts[0]:
        out["date"] = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p)
    if m.get("container-title") and item_type in CONTAINER_FIELD:
        out[CONTAINER_FIELD[item_type]] = _clean(m["container-title"][0])
    for src, dst in (("volume", "volume"), ("issue", "issue"), ("page", "pages"), ("publisher", "publisher"), ("DOI", "DOI")):
        if m.get(src):
            out[dst] = str(m[src])
    if m.get("ISSN"):
        out["ISSN"] = ", ".join(m["ISSN"])
    if m.get("ISBN"):
        out["ISBN"] = " ".join(m["ISBN"])
    if m.get("abstract"):
        out["abstractNote"] = _clean(re.sub(r"<[^>]+>", " ", m["abstract"]))
    if m.get("language"):
        out["language"] = m["language"]
    return out


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def compare(current: dict, found: dict) -> list[str]:
    """Fields where Crossref differs from the item; only fields valid for the item's type."""
    diffs = []
    for f, v in found.items():
        if f == "itemType":
            if v != current.get("itemType"):
                diffs.append(f"itemType: item is {current.get('itemType')}, Crossref says {v} (change it in Zotero if needed)")
            continue
        if f not in current:
            continue
        cur = current.get(f)
        if f == "creators":
            a = [(c.get("lastName") or c.get("name") or "").lower() for c in cur or []]
            b = [(c.get("lastName") or c.get("name") or "").lower() for c in v]
            if a != b:
                diffs.append(f"creators: item has {_names(cur)}; Crossref has {_names(v)}")
        elif f == "DOI":
            if norm_doi(cur or "") != norm_doi(v):
                diffs.append(f"DOI: item has '{cur}', Crossref has '{v}'")
        elif _clean(str(cur or "")).lower() != _clean(str(v)).lower():
            shown = v if len(str(v)) < 120 else str(v)[:117] + "..."
            diffs.append(f"{f}: item has '{cur}' → Crossref has '{shown}'")
    return diffs


def _names(cs: list) -> str:
    return "; ".join(c.get("name") or f"{c.get('lastName', '')}, {c.get('firstName', '')}".strip(", ") for c in cs or []) or "(none)"

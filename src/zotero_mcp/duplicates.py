"""Find likely duplicate items, following Zotero's own rules: same DOI, same ISBN, or the same
title with compatible years (and no conflicting DOIs)."""

from __future__ import annotations

import re

from .library import NON_PAPER, fmt_line, fold, year_of

SKIP_FIELDS = {"key", "version", "itemType", "tags", "collections", "relations", "dateAdded",
               "dateModified", "deleted", "parentItem", "creators"}


def norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", fold(t))).strip()


def norm_doi(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", s)
    return s


def doi_of(d: dict) -> str:
    if d.get("DOI"):
        return norm_doi(d["DOI"])
    m = re.search(r"(?im)^\s*doi:\s*(\S+)", d.get("extra", "") or "")
    return norm_doi(m.group(1)) if m else ""


def isbns_of(d: dict) -> set[str]:
    return {re.sub(r"[^0-9xX]", "", x).upper() for x in re.split(r"[\s,;]+", d.get("ISBN", "") or "") if len(x) >= 10}


def filled_fields(d: dict) -> int:
    return sum(1 for f, v in d.items() if f not in SKIP_FIELDS and v not in ("", None, [], {})) + (
        1 if d.get("creators") else 0
    )


def find_groups(all_items: list[dict]) -> tuple[list[list[dict]], dict[str, int]]:
    """Returns (groups of top-level items, {item key: number of attachments+notes})."""
    children: dict[str, int] = {}
    files: set[str] = set()
    papers = []
    for it in all_items:
        d = it["data"]
        if d.get("deleted"):
            continue
        if d["itemType"] in NON_PAPER:
            if d.get("parentItem"):
                children[d["parentItem"]] = children.get(d["parentItem"], 0) + 1
                if d["itemType"] == "attachment":
                    files.add(d["parentItem"])
        else:
            papers.append(it)

    parent = list(range(len(papers)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    by_doi: dict[str, int] = {}
    by_isbn: dict[str, int] = {}
    by_title: dict[str, list[int]] = {}
    for i, it in enumerate(papers):
        d = it["data"]
        doi = doi_of(d)
        if doi:
            if doi in by_doi:
                union(i, by_doi[doi])
            by_doi.setdefault(doi, i)
        for isbn in isbns_of(d):
            if isbn in by_isbn:
                union(i, by_isbn[isbn])
            by_isbn.setdefault(isbn, i)
        t = norm_title(d.get("title", ""))
        if len(t) >= 12:
            by_title.setdefault(t, []).append(i)
    for idxs in by_title.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                di, dj = doi_of(papers[i]["data"]), doi_of(papers[j]["data"])
                yi, yj = year_of(papers[i]), year_of(papers[j])
                if di and dj and di != dj:
                    continue
                if yi and yj and abs(yi - yj) > 1:
                    continue
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for i, it in enumerate(papers):
        groups.setdefault(find(i), []).append(it)
    out = [g for g in groups.values() if len(g) > 1]
    for g in out:
        g.sort(key=lambda it: rank(it, children, files), reverse=True)
    out.sort(key=lambda g: fold(g[0]["data"].get("title", "")))
    return out, children


def rank(it: dict, children: dict[str, int], files: set[str] | None = None) -> tuple:
    d = it["data"]
    has_file = 1 if (files is not None and it["key"] in files) else 0
    # Most complete first; ties go to the oldest copy (keeps the original dateAdded).
    return (has_file, filled_fields(d), children.get(it["key"], 0), [-ord(c) for c in d.get("dateAdded", "")])


def render_groups(groups: list[list[dict]], children: dict[str, int], paths: dict[str, str], limit: int) -> str:
    if not groups:
        return "No likely duplicates found."
    out = [f"{len(groups)} group(s) of likely duplicates. The first item in each group is the suggested one to keep "
           "(most complete; has the file)."]
    for n, g in enumerate(groups[:limit], 1):
        out.append(f"\nGroup {n}:")
        for i, it in enumerate(g):
            d = it["data"]
            mark = "KEEP? " if i == 0 else ""
            out.append(
                f"{mark}{fmt_line(it, paths)}\n    {filled_fields(d)} fields filled · "
                f"{children.get(it['key'], 0)} attachments/notes · added {d.get('dateAdded', '')[:10]}"
            )
        types = {it["data"]["itemType"] for it in g}
        if len(types) > 1:
            out.append("    ⚠ different item types; Zotero can only merge items of the same type. Fix the type first.")
    if len(groups) > limit:
        out.append(f"\n… {len(groups) - limit} more groups (raise limit to see them).")
    return "\n".join(out)

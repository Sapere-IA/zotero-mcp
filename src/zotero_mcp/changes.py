"""Every write goes through here: read the current server copy, save what will be overwritten,
write with a version check, and log it so it can be undone."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable

from .client import ZoteroClient, ZoteroError, check_key
from .journal import Journal

DEFAULTS = {"tags": [], "collections": [], "relations": {}, "deleted": 0, "creators": [], "parentItem": False}
SYNC_NOTE = "Zotero desktop shows it after its next sync (usually a few seconds; or click the sync button)."

Patcher = Callable[[dict], "dict | None"]


def default(field: str):
    return copy.deepcopy(DEFAULTS.get(field, ""))


def tag_names(tags: list) -> list[str]:
    return [t["tag"] if isinstance(t, dict) else str(t) for t in tags or []]


@dataclass
class Plan:
    key: str
    title: str
    version: int
    before: dict
    after: dict


def item_title(data: dict) -> str:
    if data.get("itemType") == "note":
        return "(note)"
    if data.get("itemType") == "attachment":
        return f"(attachment) {data.get('title', '')}"
    return data.get("title") or data.get("caseName") or data.get("subject") or "(untitled)"


class Writer:
    def __init__(self, client: ZoteroClient, journal: Journal, label_collection: Callable[[str], str]):
        self.client = client
        self.journal = journal
        self.label = label_collection

    # ------------------------------------------------------------------ items

    def plan(self, patchers: dict[str, Patcher]) -> tuple[list[Plan], list[str]]:
        """Work out what each patcher would change, against the current copy on zotero.org."""
        keys = [check_key(k) for k in patchers]
        max_batch = self.client.config.max_batch
        if len(keys) > max_batch:
            raise ZoteroError(
                f"{len(keys)} items in one call is above the safety limit of {max_batch}. "
                "Split the work into smaller calls (or raise ZOTERO_MCP_MAX_BATCH)."
            )
        current = self.client.web_items(keys)
        plans, errors = [], []
        for key, patch_fn in zip(keys, patchers.values()):
            it = current.get(key)
            if it is None:
                errors.append(f"{key}: no item with this key in the library.")
                continue
            data = it["data"]
            try:
                patch = patch_fn(data) or {}
            except ZoteroError as e:
                errors.append(f"{key} ({item_title(data)}): {e}")
                continue
            before = {f: copy.deepcopy(data.get(f, default(f))) for f in patch}
            after = {f: v for f, v in patch.items() if _norm(f, v) != _norm(f, before[f])}
            if after:
                plans.append(Plan(key, item_title(data), it["version"], {f: before[f] for f in after}, after))
        return plans, errors

    def apply(self, plans: list[Plan]) -> tuple[list[dict], list[str]]:
        if not plans:
            return [], []
        payload = [{"key": p.key, "version": p.version, **p.after} for p in plans]
        ok, failed = self.client.web_write("items", payload)
        ops = [
            {"op": "update_item", "key": p.key, "title": p.title, "before": p.before, "after": p.after}
            for i, p in enumerate(plans)
            if i in ok and not ok[i].get("unchanged")
        ]
        errors = [f"{plans[i].key} ({plans[i].title}): {msg}" for i, msg in failed.items()]
        return ops, errors

    def run(self, summary: str, patchers: dict[str, Patcher], dry_run: bool) -> str:
        """Plan, then either preview or apply + journal. The common path for item-editing tools."""
        plans, errors = self.plan(patchers)
        if not plans and errors:
            raise ZoteroError("Nothing was changed." + _errors(errors))
        if not plans:
            return "Nothing to change: everything is already as requested."
        if dry_run:
            return self.render_preview(plans) + _errors(errors)
        ops, write_errors = self.apply(plans)
        return self.finish(summary, ops, errors + write_errors)

    def finish(self, summary: str, ops: list[dict], errors: list[str], extra: str = "") -> str:
        if not ops:
            return "Nothing was changed." + _errors(errors)
        entry = self.journal.append(summary, ops)
        lines = [
            f"Done: {summary}.",
            self.render_ops(ops),
            f"Change id: {entry['id']} (call undo with this id to revert it). {SYNC_NOTE}",
        ]
        if extra:
            lines.append(extra)
        return "\n".join(lines) + _errors(errors)

    # ------------------------------------------------------------------ collections

    def create_collection(self, name: str, parent: str | None) -> dict:
        ok, failed = self.client.web_write("collections", [{"name": name, "parentCollection": parent or False}])
        if 0 not in ok:
            raise ZoteroError(f"Could not create the folder: {failed.get(0, 'unknown error')}")
        return {"op": "create_collection", "key": ok[0]["key"], "name": name, "parentCollection": parent or False}

    def update_collection(self, key: str, changes: dict) -> dict:
        cur = self.client.web_collections([key]).get(key)
        if cur is None:
            raise ZoteroError(f"No folder with key {key}.")
        before = {f: cur["data"].get(f, False) for f in changes}
        ok, failed = self.client.web_write("collections", [{"key": key, "version": cur["version"], **changes}])
        if 0 not in ok:
            raise ZoteroError(f"Could not update the folder: {failed.get(0, 'unknown error')}")
        return {"op": "update_collection", "key": key, "name": cur["data"]["name"], "before": before, "after": changes}

    def delete_empty_collection(self, key: str) -> dict:
        cur = self.client.web_collections([key]).get(key)
        if cur is None:
            raise ZoteroError(f"No folder with key {key}.")
        p = self.client.prefix
        if self.client.web_get(f"{p}/collections/{key}/items", limit=1) or self.client.web_get(
            f"{p}/collections/{key}/collections", limit=1
        ):
            raise ZoteroError(
                f"The folder '{cur['data']['name']}' is not empty (it has items or subfolders). "
                "Only empty folders can be deleted with this tool."
            )
        self.client.web_delete_collection(key, cur["version"])
        return {
            "op": "delete_collection",
            "key": key,
            "name": cur["data"]["name"],
            "parentCollection": cur["data"].get("parentCollection") or False,
        }

    # ------------------------------------------------------------------ notes

    def create_note(self, parent_key: str, html: str) -> dict:
        obj = {"itemType": "note", "note": html, "parentItem": parent_key, "tags": [], "collections": [], "relations": {}}
        ok, failed = self.client.web_write("items", [obj])
        if 0 not in ok:
            raise ZoteroError(f"Could not create the note: {failed.get(0, 'unknown error')}")
        return {"op": "create_item", "key": ok[0]["key"], "title": f"note on {parent_key}"}

    # ------------------------------------------------------------------ undo

    def undo(self, change_id: str, force: bool = False) -> str:
        entry = self.journal.get(change_id)
        if entry.get("undone_by"):
            raise ZoteroError(f"Change {change_id} was already undone (by change {entry['undone_by']}).")
        new_ops: list[dict] = []
        problems: list[str] = []
        # Undo in reverse order, batching consecutive item edits into one request.
        runs: list[list[dict]] = []
        for op in reversed(entry["ops"]):
            is_item = op["op"] in ("update_item", "create_item")
            if runs and is_item and runs[-1][0]["op"] in ("update_item", "create_item"):
                runs[-1].append(op)
            else:
                runs.append([op])
        for run in runs:
            if run[0]["op"] in ("update_item", "create_item"):
                plans, issues = self._plan_item_reverts(run, force)
                ops, errs = self.apply(plans)
                new_ops += ops
                problems += issues + errs
            else:
                try:
                    op = self._revert_collection_op(run[0], force)
                    if op:
                        new_ops.append(op)
                except ZoteroError as e:
                    problems.append(f"folder '{run[0].get('name')}': {e}")
        if not new_ops and not problems:
            self.journal.mark_undone(change_id, "(nothing left to revert)")
            return f"Change {change_id} had already been reverted; nothing to do."
        entry2 = self.journal.append(f"undo of {change_id} ({entry['summary']})", new_ops, undoes=change_id)
        if problems:
            hint = "" if force else " Call undo again with force=true to overwrite those later edits."
            return (
                f"Partly undone (logged as change {entry2['id']}).\n{self.render_ops(new_ops)}\n"
                "Not reverted:\n- " + "\n- ".join(problems) + hint
            )
        self.journal.mark_undone(change_id, entry2["id"])
        return (
            f"Undone: {entry['summary']}.\n{self.render_ops(new_ops)}\n"
            f"This undo is itself change {entry2['id']} (it can be undone too). {SYNC_NOTE}"
        )

    def _plan_item_reverts(self, ops: list[dict], force: bool) -> tuple[list[Plan], list[str]]:
        current = self.client.web_items([op["key"] for op in ops])
        plans, problems = [], []
        for op in ops:
            cur = current.get(op["key"])
            if cur is None:
                if op["op"] != "create_item":
                    problems.append(f"{op['key']} ({op.get('title')}): item no longer exists.")
                continue
            data = cur["data"]
            revert: dict = {}
            if op["op"] == "create_item":
                revert = {"deleted": 1}  # to Zotero's trash, never erased
            else:
                for f, after_v in op["after"].items():
                    before_v = op["before"][f]
                    cur_v = data.get(f, default(f))
                    if f == "tags":
                        revert[f] = _undo_tags(cur_v, before_v, after_v)
                    elif f == "collections":
                        revert[f] = _undo_list(cur_v, before_v, after_v)
                    elif f == "relations":
                        revert[f] = _undo_relations(cur_v, before_v, after_v)
                    elif _norm(f, cur_v) == _norm(f, before_v):
                        continue  # already back to the old value
                    elif _norm(f, cur_v) != _norm(f, after_v) and not force:
                        problems.append(
                            f"{op['key']} ({op.get('title')}): '{f}' was edited again since this change "
                            f"(now {_short(f, cur_v)}); left as is."
                        )
                    else:
                        revert[f] = before_v
            before = {f: copy.deepcopy(data.get(f, default(f))) for f in revert}
            revert = {f: v for f, v in revert.items() if _norm(f, v) != _norm(f, before[f])}
            if revert:
                plans.append(Plan(op["key"], item_title(data), cur["version"], {f: before[f] for f in revert}, revert))
        return plans, problems

    def _revert_collection_op(self, op: dict, force: bool) -> dict | None:
        kind = op["op"]
        if kind == "create_collection":
            if not self.client.web_collections([op["key"]]):
                return None  # already gone
            return self.delete_empty_collection(op["key"])
        if kind == "delete_collection":
            return self.create_collection(op["name"], op.get("parentCollection") or None)
        if kind == "update_collection":
            cur = self.client.web_collections([op["key"]]).get(op["key"])
            if cur is None:
                raise ZoteroError("the folder no longer exists.")
            drift = [f for f, v in op["after"].items() if (cur["data"].get(f) or False) != v]
            if drift and not force:
                raise ZoteroError(f"it was edited again since this change ({', '.join(drift)}).")
            return self.update_collection(op["key"], op["before"])
        raise ZoteroError(f"unknown operation {kind}")

    # ------------------------------------------------------------------ rendering

    def render_preview(self, plans: list[Plan]) -> str:
        lines = [f"PREVIEW: nothing has been changed yet. {len(plans)} item(s) would change:"]
        for p in plans:
            lines.append(f"- [{p.key}] {p.title}")
            lines += [f"    {d}" for d in self._diff(p.before, p.after)]
        lines.append("Call the same tool again with dry_run=false to apply.")
        return "\n".join(lines)

    def render_ops(self, ops: list[dict], limit: int = 40) -> str:
        lines = []
        for op in ops[:limit]:
            kind = op["op"]
            if kind == "update_item":
                lines.append(f"- [{op['key']}] {op.get('title')}")
                lines += [f"    {d}" for d in self._diff(op["before"], op["after"])]
            elif kind == "create_item":
                lines.append(f"- created [{op['key']}] {op.get('title')}")
            elif kind == "create_collection":
                lines.append(f"- created folder '{op['name']}' [{op['key']}]")
            elif kind == "delete_collection":
                lines.append(f"- deleted empty folder '{op['name']}'")
            elif kind == "update_collection":
                lines.append(f"- folder '{op['name']}': " + "; ".join(self._diff(op["before"], op["after"])))
        if len(ops) > limit:
            lines.append(f"- … and {len(ops) - limit} more")
        return "\n".join(lines)

    def _diff(self, before: dict, after: dict) -> list[str]:
        out = []
        for f, new in after.items():
            old = before.get(f, default(f))
            if f == "tags":
                o, n = set(tag_names(old)), set(tag_names(new))
                parts = [f'+"{t}"' for t in sorted(n - o)] + [f'-"{t}"' for t in sorted(o - n)]
                out.append("tags: " + " ".join(parts))
            elif f in ("collections", "parentCollection"):
                o = set(old or []) if isinstance(old, list) else {old} - {False, None}
                n = set(new or []) if isinstance(new, list) else {new} - {False, None}
                parts = [f"+ {self.label(c)}" for c in sorted(n - o)] + [f"- {self.label(c)}" for c in sorted(o - n)]
                out.append("folders: " + ", ".join(parts))
            elif f == "deleted":
                out.append("moved to Zotero's trash" if new else "restored from Zotero's trash")
            elif f == "parentItem":
                out.append(f"attached to item {new} (was {old})")
            elif f == "relations":
                out.append("linked as the merged copy of its duplicates")
            else:
                out.append(f"{f}: {_short(f, old)} → {_short(f, new)}")
        return out


# ---------------------------------------------------------------------- helpers


def _errors(errors: list[str]) -> str:
    return ("\nProblems:\n- " + "\n- ".join(errors)) if errors else ""


def _norm(field: str, v):
    if field == "tags":
        return sorted(set(tag_names(v)))
    if field == "collections":
        return sorted(set(v or []))
    if field == "relations":
        return {k: sorted(_as_list(x)) for k, x in (v or {}).items() if _as_list(x)}
    if field == "deleted":
        return bool(v)
    if field == "parentItem":
        return v or False
    return v


def _short(field: str, v) -> str:
    if field == "creators":
        names = [
            c.get("name") or ", ".join(x for x in (c.get("lastName"), c.get("firstName")) if x) for c in v or []
        ]
        return '"' + "; ".join(names) + '"' if names else "(none)"
    s = str(v) if v not in (None, "") else ""
    if not s:
        return "(empty)"
    return '"' + (s if len(s) <= 80 else s[:77] + "...") + '"'


def _as_list(x) -> list:
    if not x:
        return []
    return list(x) if isinstance(x, list) else [x]


def _undo_tags(cur: list, before: list, after: list) -> list:
    b, a = set(tag_names(before)), set(tag_names(after))
    added, removed = a - b, b - a
    out = [t for t in cur or [] if t["tag"] not in added]
    have = set(tag_names(out))
    out += [t for t in before if t["tag"] in removed and t["tag"] not in have]
    return out


def _undo_list(cur: list, before: list, after: list) -> list:
    added, removed = set(after) - set(before), set(before) - set(after)
    out = [c for c in cur or [] if c not in added]
    out += [c for c in before if c in removed and c not in out]
    return out


def _undo_relations(cur: dict, before: dict, after: dict) -> dict:
    out = {}
    for pred in set(cur or {}) | set(before or {}) | set(after or {}):
        b, a = set(_as_list((before or {}).get(pred))), set(_as_list((after or {}).get(pred)))
        vals = [v for v in _as_list((cur or {}).get(pred)) if v not in (a - b)]
        vals += [v for v in sorted(b - a) if v not in vals]
        if vals:
            out[pred] = vals[0] if len(vals) == 1 else vals
    return out

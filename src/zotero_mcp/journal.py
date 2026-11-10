"""Append-only log of every change made through this server, so any change can be undone."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from .client import ZoteroError


class Journal:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "changes.jsonl"

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # a torn last line from a crash must not block undo of everything else
        return out

    def get(self, change_id: str) -> dict:
        for e in self.entries():
            if e["id"] == change_id:
                return e
        raise ZoteroError(f"No change with id '{change_id}'. Use the history tool to list changes.")

    def append(self, summary: str, ops: list[dict], undoes: str | None = None) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        entry = {
            "id": f"{now:%m%d-%H%M%S}-{secrets.token_hex(2)}",
            "time": now.isoformat(timespec="seconds"),
            "summary": summary,
            "ops": ops,
            "undone_by": None,
        }
        if undoes:
            entry["undoes"] = undoes
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def mark_undone(self, change_id: str, undone_by: str) -> None:
        entries = self.entries()
        for e in entries:
            if e["id"] == change_id:
                e["undone_by"] = undone_by
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)  # atomic on Windows too

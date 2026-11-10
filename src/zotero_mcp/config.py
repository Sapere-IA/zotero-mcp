"""Settings, read from environment variables (set in the Claude Code MCP config)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    """Where the change journal lives. %LOCALAPPDATA%\\zotero-mcp on Windows, ~/.zotero-mcp elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "zotero-mcp"
    return Path.home() / ".zotero-mcp"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    api_key: str = ""
    local_url: str = "http://127.0.0.1:23119/api"
    web_url: str = "https://api.zotero.org"
    crossref_url: str = "https://api.crossref.org"
    data_dir: Path = Path(".")
    max_batch: int = 100
    timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_key=os.environ.get("ZOTERO_API_KEY", "").strip(),
            local_url=os.environ.get("ZOTERO_LOCAL_URL", cls.local_url),
            web_url=os.environ.get("ZOTERO_WEB_URL", cls.web_url),
            crossref_url=os.environ.get("CROSSREF_URL", cls.crossref_url),
            data_dir=Path(os.environ.get("ZOTERO_MCP_DATA_DIR") or default_data_dir()),
            max_batch=_int_env("ZOTERO_MCP_MAX_BATCH", 100),
            timeout=float(_int_env("ZOTERO_MCP_TIMEOUT", 20)),
        )

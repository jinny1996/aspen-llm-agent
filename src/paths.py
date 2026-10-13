"""Where the shared data lives (parsed papers/figures, schema layer, Aspen function
catalog, validators).

Resolution order:
  1. environment variable ASPEN_AGENT_DATA_DIR
  2. ../shared in this repository (validators and schema/catalog files only; the parsed
     papers are not published)
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    env = os.environ.get("ASPEN_AGENT_DATA_DIR")
    if env:
        return Path(env)
    return _REPO / "shared"


def papers_dir() -> Path:
    """Parsed papers/figures. Not published; set ASPEN_AGENT_PAPERS_DIR to your own copy,
    otherwise ../data/parsed is used."""
    env = os.environ.get("ASPEN_AGENT_PAPERS_DIR")
    if env:
        return Path(env)
    return _REPO / "data" / "parsed"


# shared/ is organized in sub-folders.
_SHARED_SUBDIRS = ("aspen", "schema", "validators")


def shared_dirs() -> list[Path]:
    """Every directory that may hold shared files (the data dir itself plus its sub-folders)."""
    root = data_dir()
    return [root] + [root / s for s in _SHARED_SUBDIRS if (root / s).is_dir()]


def shared_file(name: str) -> Path:
    """Locate a shared file such as Schema_v5.yaml or aspen_code_v1.py by name."""
    for d in shared_dirs():
        if (d / name).exists():
            return d / name
    return data_dir() / name


REPO_DIR = _REPO
SRC_DIR = _REPO / "src"

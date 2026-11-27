"""Keep installed LX sources in sync with every ``sources`` folder.

The player no longer exposes any source configuration: the scripts under
``sources/`` *are* the source list. On **every launch** every candidate folder
is scanned and compared with what is registered in SQLite, so a new script is
picked up, an edited one is re-probed, a failed one gets another chance, and a
deleted one disappears - without the user managing anything.

Only scripts that are not already installed from unchanged content are
executed in the Node sandbox: a normal launch with no edits stays down to a
few milliseconds of file reads. A script that failed a previous launch is
deliberately retried on each new launch (Node may have become available, or a
dependency may have been fixed since), which is what makes dropping a new
file into ``sources/`` reliably take effect.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from config.settings import DATA_DIR
from core.lx_source import LxSource, build_source, get_store, load_sources

Progress = Callable[[str], None]

# Remembering which file versions were already probed keeps a normal launch
# instant: unchanged, successfully installed scripts are never executed again.
# Records with ok=False are intentionally NOT trusted as a "skip forever" flag.
_INDEX_FILE = "lx_script_index.json"


def _index_path() -> Path:
    return Path(DATA_DIR) / _INDEX_FILE


def _load_index() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_index(index: dict[str, dict[str, Any]]) -> None:
    path = _index_path()
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _candidate_folders() -> list[Path]:
    """Every location a user might reasonably keep source scripts, in priority order.

    * frozen builds: the exe-side ``sources`` (the folder users maintain) first,
      then any folder bundled inside the package;
    * source checkouts: the project root next to the code;
    * the working directory, for portable/CLI launches;
    * the per-user data directory, so scripts survive an app reinstall/upgrade.
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "sources")
        candidates.append(Path(getattr(sys, "_MEIPASS", ".") or ".") / "sources")
    root = Path(__file__).resolve().parent.parent
    candidates.append(root / "sources")
    candidates.append(Path.cwd() / "sources")
    candidates.append(Path(DATA_DIR) / "sources")
    return candidates


def sources_folders() -> list[Path]:
    """All existing folders that actually contain scripts, de-duplicated.

    Every launch re-evaluates this list, so a folder created or filled while
    the app was closed is picked up on the next start.
    """
    result: list[Path] = []
    seen: set[Path] = set()
    for path in _candidate_folders():
        try:
            key = path.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_dir() and any(path.glob("*.js")):
                result.append(path)
        except OSError:
            continue
    return result


def bundled_folder() -> Path | None:
    """Highest-priority script folder, if any (kept for external callers)."""
    folders = sources_folders()
    return folders[0] if folders else None


def _script_files(folders: list[Path]) -> list[Path]:
    """Merge scripts from all folders; equal names keep the higher-priority one.

    A user may have the same script both next to the exe and in the data
    directory. Installing it twice would show two identical sources, so the
    first folder in priority order wins for a given file name.
    """
    by_name: dict[str, Path] = {}
    for folder in folders:
        try:
            for path in folder.glob("*.js"):
                by_name.setdefault(path.name, path)
        except OSError:
            continue
    return [by_name[key] for key in sorted(by_name)]


def _existing_by_file() -> tuple[dict[Path, LxSource], list[LxSource]]:
    """Split registered sources into file-backed ones and the rest."""
    by_file: dict[Path, LxSource] = {}
    others: list[LxSource] = []
    try:
        sources = load_sources()
    except Exception:  # noqa: BLE001 - a locked DB must not break startup
        return ({}, [])
    for source in sources:
        url = (source.script_url or "").strip()
        if not url or url.lower().startswith(("http://", "https://")):
            others.append(source)
            continue
        try:
            by_file[Path(url).resolve()] = source
        except OSError:
            others.append(source)
    return (by_file, others)


def sync_bundled_sources(progress: Progress | None = None) -> tuple[int, int]:
    """Install new/changed scripts and drop ones no longer on disk.

    Returns ``(updated, failed)`` where ``updated`` counts scripts that were
    parsed and written this time (unchanged ones are skipped). Called on every
    launch; failed scripts are retried on every launch as well.
    """
    folders = sources_folders()
    if not folders:
        return (0, 0)

    store = get_store()
    existing, others = _existing_by_file()
    files = _script_files(folders)
    keep = {path.resolve() for path in files}

    index = _load_index()
    new_index: dict[str, dict[str, Any]] = {}
    updated = failed = 0
    for path in files:
        key = str(path.resolve())
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            failed += 1
            continue
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        current = existing.get(path.resolve())
        record = index.get(key) or {}
        # Skip only scripts that are installed, unchanged and known-good. A
        # previous failure must NOT short-circuit: Node may have been missing
        # at the time, or the user may have fixed the script since. Retrying is
        # what lets a newly dropped file eventually become available.
        if record.get("hash") == digest and record.get("ok") and \
                current is not None and current.script == text:
            new_index[key] = record
            continue
        if progress:
            progress(path.name)
        ok = False
        try:
            source = build_source(
                str(path), timeout=20.0, existing_id=current.id if current else ""
            )
            store.upsert(source)
            updated += 1
            ok = True
        except Exception:  # noqa: BLE001 - a dead script must not stop the rest
            failed += 1
        new_index[key] = {"hash": digest, "ok": ok}

    # The folders are the single source of truth: scripts removed from every
    # folder (and anything installed from a URL by an older version) are no
    # longer listed.
    for path_key, source in existing.items():
        if path_key in keep:
            continue
        try:
            store.delete(source.id)
        except Exception:  # noqa: BLE001 - keep startup alive on a locked DB
            pass
    for source in others:
        try:
            store.delete(source.id)
        except Exception:  # noqa: BLE001
            pass

    _save_index(new_index)
    return (updated, failed)

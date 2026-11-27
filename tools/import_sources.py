"""Install every LX script in a folder into the player's database.

Usage:  python tools/import_sources.py [folder]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.lx_source import build_source, get_store, load_sources  # noqa: E402


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sources"
    files = sorted(folder.glob("*.js"))
    if not files:
        print(f"目录里没有 .js 文件：{folder}")
        return 1

    store = get_store()
    existing = {source.script_url: source for source in load_sources()}
    added = updated = failed = 0

    for path in files:
        key = str(path)
        try:
            source = build_source(key, timeout=20.0)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"跳过 {path.name}：{exc}")
            failed += 1
            continue

        previous = existing.get(key)
        if previous is not None:
            source.id = previous.id
            source.enabled = previous.enabled
            updated += 1
            action = "更新"
        else:
            added += 1
            action = "新增"

        store.upsert(source)
        platforms = "、".join(info.name for info in source.platforms.values())
        print(f"{action} {source.name} v{source.version or '?'} -> {platforms}")

    print(f"\n新增 {added} · 更新 {updated} · 失败 {failed}")
    print("当前已安装：")
    for source in load_sources():
        state = "启用" if source.enabled else "停用"
        print(f"  [{state}] {source.name} v{source.version} · {source.platform_label()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Load every LX script in sources/ and report what each one offers."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.lx_proc import LxProcess, find_node  # noqa: E402


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sources"
    files = sorted(folder.glob("*.js"))
    node = find_node()
    print(f"node = {node}")
    print(f"scripts = {len(files)}\n")

    ok = failed = 0
    for path in files:
        started = time.monotonic()
        code = path.read_text(encoding="utf-8", errors="replace")
        proc = LxProcess(startup_timeout=25)
        try:
            info = proc.load(code, source_url=path.name)
            elapsed = time.monotonic() - started
            meta = info.meta
            platforms = ", ".join(
                f"{s.name}[{s.key}] {'/'.join(s.qualitys[:3]) or '默认'}"
                for s in info.sources.values()
            ) or "无"
            print(f"OK   {path.name}  ({elapsed:.1f}s)")
            print(f"     名称: {meta.get('name') or '?'}  v{meta.get('version') or '?'}  by {meta.get('author') or '?'}")
            print(f"     平台: {platforms}")
            ok += 1
        except Exception as exc:  # noqa: BLE001 - diagnostic tool
            elapsed = time.monotonic() - started
            print(f"FAIL {path.name}  ({elapsed:.1f}s) -> {exc}")
            failed += 1
        finally:
            proc.close()

    print(f"\n成功 {ok} / 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

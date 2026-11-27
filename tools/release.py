"""发布 TT 播放器新版本：打双包 + 创建 GitHub Release。

用法（在项目根目录）::

    python tools/release.py                 # 完整发布（打 zip + 创建 Release + 上传）
    python tools/release.py --pack-only     # 只打两个 zip，不上传

前置条件：
    1. dist/TT播放器/ 已由 快速打包.bat（或 PyInstaller）生成最新产物；
    2. dist/TT播放器/sources 与 runtime 已就位（全包需要）；
    3. 本机 Git Credential Manager 存有 GitHub token（git credential fill）。

资产策略（与 update.py 的下载逻辑严格对应）：
    * 精简包 TTPlayer-v{VERSION}-lite.zip：只含 _internal/app/**（业务源码 +
      app_version.txt）。引导器升级只下载这一个，体积 ≈ 100 KB 级。
    * 全包   TTPlayer-v{VERSION}-full.zip：完整 dist（exe + _internal + sources
      + runtime），供新用户首次安装；不含任何 .db 数据库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_APP = ROOT / "dist" / "TT播放器"
OUT_DIR = ROOT / "build" / "release"

OWNER = "antianxu14951"
REPO = "TTPlayer"
UPLOAD_API = f"https://api.github.com/repos/{OWNER}/{REPO}/releases"


# ------------------------------------------------------------------ version


def load_version() -> str:
    namespace: dict[str, str] = {}
    exec((ROOT / "version.py").read_text(encoding="utf-8"), namespace)  # noqa: S102
    return str(namespace["VERSION"])


def credential_token() -> str:
    """从 Git Credential Manager 取 GitHub token（与 git push 同源）。"""
    query = "protocol=https\nhost=github.com\n\n"
    proc = subprocess.run(
        ["git", "credential", "fill"], input=query, capture_output=True, text=True
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("未在 Git Credential Manager 找到 GitHub 凭据，请先 git push 一次。")


# -------------------------------------------------------------------- packs


def _add_tree(zf: zipfile.ZipFile, base: Path, arc_prefix: str) -> int:
    count = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in (".db", ".db-wal", ".db-shm", ".log", ".tmp"):
            continue
        if "__pycache__" in path.parts:
            continue
        zf.write(path, arc_prefix / path.relative_to(base))
        count += 1
    return count


def build_packs(version: str) -> tuple[Path, Path]:
    lite_path = OUT_DIR / f"TTPlayer-v{version}-lite.zip"
    full_path = OUT_DIR / f"TTPlayer-v{version}-full.zip"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    app_dir = DIST_APP / "_internal" / "app"
    if not (app_dir / "main.py").is_file():
        raise SystemExit(f"缺少业务目录：{app_dir}，请先跑 快速打包.bat")

    with zipfile.ZipFile(lite_path, "w", zipfile.ZIP_DEFLATED) as zf:
        n = _add_tree(zf, app_dir, "app")
    print(f"[pack] 精简包 {lite_path.name}：{n} 个文件，"
          f"{lite_path.stat().st_size / 1024:.0f} KB")

    with zipfile.ZipFile(full_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        n = _add_tree(zf, DIST_APP, "TT播放器")
    print(f"[pack] 全包   {full_path.name}：{n} 个文件，"
          f"{full_path.stat().st_size / 1048576:.0f} MB")

    for path in (lite_path, full_path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        (path.with_suffix(path.suffix + ".sha256")).write_text(
            f"{digest}  {path.name}\n", encoding="ascii")
        print(f"[pack] SHA256 {path.name} = {digest[:16]}…")
    return lite_path, full_path


# ------------------------------------------------------------------- github


def api_request(method: str, url: str, token: str, payload: dict | None = None,
                raw: bytes | None = None, content_type: str = "application/json") -> bytes:
    data = json.dumps(payload).encode("utf-8") if payload is not None else raw
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": content_type,
            "User-Agent": "TTPlayer-Release",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def release_exists(token: str, tag: str) -> int | None:
    try:
        body = api_request("GET", f"{UPLOAD_API}/tags/{tag}", token)
        return json.loads(body.decode("utf-8")).get("id")
    except Exception:
        return None


def create_release(token: str, tag: str, notes: str) -> int:
    body = api_request("POST", UPLOAD_API, token, payload={
        "tag_name": tag,
        "target_commitish": "master",
        "name": f"TT播放器 v{tag.lstrip('v')}",
        "body": notes or "详见提交历史。",
        "draft": False,
        "prerelease": False,
    })
    return int(json.loads(body.decode("utf-8"))["id"])


def upload_asset(token: str, release_id: int, path: Path) -> None:
    url = (f"https://uploads.github.com/repos/{OWNER}/{REPO}/releases/"
           f"{release_id}/assets?name={path.name}")
    # 上传接口不走 JSON Content-Type
    req = urllib.request.Request(
        url, data=path.read_bytes(), method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/zip",
            "User-Agent": "TTPlayer-Release",
        },
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        json.loads(resp.read().decode("utf-8"))
    print(f"[upload] {path.name} 完成")


# --------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-only", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="同名 Release 已存在时仍继续（资产补充上传）")
    args = parser.parse_args()

    version = load_version()
    tag = f"v{version}"
    print(f"[release] 版本 {tag}")

    lite_path, full_path = build_packs(version)
    if args.pack_only:
        print("[release] --pack-only：zip 已生成于", OUT_DIR)
        return 0

    token = credential_token()
    release_id = release_exists(token, tag)
    if release_id and not args.force:
        print(f"[release] {tag} 已存在（id={release_id}）。"
              f"如需补充资产请加 --force。")
        return 1
    if not release_id:
        notes = _extract_notes(version)
        release_id = create_release(token, tag, notes)
        print(f"[release] 已创建 Release {tag}（id={release_id}）")

    for asset in (lite_path, full_path,
                  lite_path.with_suffix(lite_path.suffix + ".sha256"),
                  full_path.with_suffix(full_path.suffix + ".sha256")):
        if asset.is_file():
            upload_asset(token, release_id, asset)

    print(f"[release] 完成：https://github.com/{OWNER}/{REPO}/releases/tag/{tag}")
    return 0


def _extract_notes(version: str) -> str:
    """从 version.py 的 RELEASES 取当前版本条目拼更新日志。"""
    namespace: dict[str, object] = {}
    exec((ROOT / "version.py").read_text(encoding="utf-8"), namespace)  # noqa: S102
    for item in namespace.get("RELEASES") or ():  # type: ignore[union-attr]
        if str(item.get("version")) == version:  # type: ignore[union-attr]
            lines = [f"### TT播放器 v{version} · {item.get('codename', '')}",
                     f"发布日期：{item.get('date', '')}", ""]
            lines += [f"- {note}" for note in item.get("notes") or ()]  # type: ignore[union-attr]
            lines += ["", "---",
                      "下载说明：新用户用 full 包；已安装用户由程序自动升级（lite 包）。",
                      "校验：每个 zip 均附带 .sha256 文件。"]
            return "\n".join(str(x) for x in lines)
    return f"TT播放器 v{version}"


if __name__ == "__main__":
    sys.exit(main())

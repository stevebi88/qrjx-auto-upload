#!/usr/bin/env python3
"""
构建「解压即用」离线部署包（方案A）：完全自包含，同事解压后无需安装 Python / 依赖 / 浏览器。

自包含原理（macOS/Linux）：
  - .runtime-python/ ：完整 Python 3.12 运行时（复制自构建机 base python）
  - .venv/           ：虚拟环境（python 符号链接指向 ../.runtime-python/bin/python3.12，
                       pyvenv.cfg home 用相对路径 → 解压到任何位置都能用）
  - .playwright-browsers/ ：playwright 1.58.0 / patchright 1.58.2 共用 chromium-1208 + headless + ffmpeg
  - tools/biliup/    ：B 站上传所需 biliup 二进制（从构建机 ~/.social-auto-upload 暂存，离线可用）
  - .venv/bin/sau    ：sh 自包含启动器（不依赖绝对 shebang）

用法（在项目根目录，需本平台 Python 3.12）：
  python3.12 build_offline_pkg.py             # 完整构建
  python3.12 build_offline_pkg.py --skip-env  # 跳过建 venv/装依赖，只重新打包

产物：
  macOS/Linux → dist/social-auto-upload-离线版-mac-arm64.tar.gz（tar 保留符号链接）
  Windows     → dist/social-auto-upload-离线版-win-x86_64.zip（Windows venv 自包含，无需 runtime）

同事部署：解压 → 运行「验证环境」脚本（脚本自动剥离 macOS 隔离标记并自检 CLI/浏览器）
          → mcp.json 指向包内 .venv/bin/python3.12 mcp_server/server.py
          → 首次使用前先按 docs/OFFLINE_GUIDE.md 执行一遍

macOS 隔离标记（quarantine）说明：
  压缩包经网盘/微信等传输后会带 com.apple.quarantine，解压时 macOS 会传播到全部内容，
  导致 numpy 等动态库被系统策略拒绝加载（ImportError: library load disallowed by system policy）、
  chromium 启动即被 SIGKILL。因此「验证环境.command」内置 xattr -cr 自动剥离；
  若双击脚本被 Gatekeeper 拦截，先在终端执行：xattr -cr <解压目录> 后再运行。
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
VENV = ROOT / ".venv"
BROWSERS = ROOT / ".playwright-browsers"
RUNTIME = ROOT / ".runtime-python"
DIST = ROOT / "dist"

IS_WIN = os.name == "nt"
VPY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
VPIP = VENV / ("Scripts/pip.exe" if IS_WIN else "bin/pip")

PIP_INDEX = os.environ.get("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
PLAYWRIGHT_HOST = os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST", "https://cdn.npmmirror.com/binaries/playwright")


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print(">>>", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


def banner(t: str) -> None:
    print("\n" + "=" * 60 + "\n  " + t + "\n" + "=" * 60)


def build_env() -> None:
    """建 venv + 装依赖 + 下载浏览器 + 自包含化。"""
    banner("1/5 创建虚拟环境 .venv")
    if VPY.exists():
        print("已存在 .venv，跳过创建")
    else:
        py = os.environ.get("PYTHON", sys.executable)
        run([py, "-m", "venv", str(VENV)])

    banner("2/5 安装依赖（pip 清华源）")
    run([VPIP, "install", "-U", "pip"])
    # sau 本体必须非 editable（否则指向构建机路径）
    r = run([VPIP, "install", ".", "--index-url", PIP_INDEX])
    if r.returncode != 0:
        raise SystemExit("安装 sau 依赖失败")
    # 依赖说明：uploader 混用 playwright/patchright 两个库（douyin 等用 patchright，PDD 用 playwright）
    # 两个库必须同 chromium 版本：playwright 1.58.0 与 patchright 1.58.2 都对应 chromium-1208 → 只打一个浏览器
    for extra in (["fastmcp"], ["playwright==1.58.0"], ["Flask[async]==3.1.1", "flask-cors==6.0.0"]):
        run([VPIP, "install", *extra, "--index-url", PIP_INDEX])

    banner("3/5 下载 Patchright 浏览器到包内 .playwright-browsers")
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(BROWSERS), PLAYWRIGHT_DOWNLOAD_HOST=PLAYWRIGHT_HOST)
    BROWSERS.mkdir(parents=True, exist_ok=True)
    r = run([VPY, "-m", "patchright", "install", "chromium"], env=env)
    if r.returncode != 0:
        run([VPY, "-m", "patchright", "install", "chromium"], env=env)  # 重试一次
    run([VPY, "-m", "patchright", "install", "ffmpeg"], env=env)

    banner("4/5 复制 Python 运行时到包内（.runtime-python）")
    if not IS_WIN:
        _make_runtime()
        _relink_venv_python()
        _rewrite_sau_launcher()
    # sitecustomize 所有平台都要：把项目根插入 sys.path（否则 conf/cookies 定位错误）
    _inject_sitecustomize()
    print("环境构建完成")


def _make_runtime() -> None:
    """复制构建机 base python（含 stdlib）到 .runtime-python。"""
    base = run([VPY, "-c", "import sys; print(sys.base_prefix)"], capture_output=True, text=True).stdout.strip()
    if not base:
        raise SystemExit("无法获取 base_prefix")
    src = Path(base)
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    shutil.copytree(src, RUNTIME, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"已复制运行时: {src} → {RUNTIME}")


def _relink_venv_python() -> None:
    """把 .venv/bin/python3.12 改为相对符号链接指向 ../.runtime-python/bin/python3.12。"""
    bin_dir = VENV / "bin"
    target = bin_dir / "python3.12"
    if target.is_symlink() or target.exists():
        target.unlink()
    # 相对：.venv/bin → ../.runtime-python/bin/python3.12
    os.symlink("../../.runtime-python/bin/python3.12", target)
    # pyvenv.cfg 用相对 home
    cfg = VENV / "pyvenv.cfg"
    cfg.write_text(
        "home = ../.runtime-python/bin\n"
        "include-system-site-packages = false\n"
        "version = 3.12.13\n"
        "executable = ../.runtime-python/bin/python3.12\n",
        encoding="utf-8",
    )
    print("venv python 已相对链接 + pyvenv.cfg 相对 home")


def _rewrite_sau_launcher() -> None:
    """重写 .venv/bin/sau 为 sh 自包含启动器（不依赖绝对 shebang）。"""
    launcher = VENV / "bin" / "sau"
    if IS_WIN:
        return
    launcher.write_text(
        "#!/bin/sh\n"
        "# social-auto-upload CLI（自包含启动器，由 build_offline_pkg.py 生成）\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'exec "$DIR/python3.12" -c "from sau_cli import main; raise SystemExit(main())" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    print("sau 启动器已重写为自包含 sh")


def _inject_sitecustomize() -> None:
    """sitecustomize：向上找项目根插 sys.path + 设浏览器路径。"""
    site_pkgs = _find_site_packages()
    if not site_pkgs:
        raise SystemExit("未找到 site-packages")
    code = '''# -*- coding: utf-8 -*-
# 自动注入（build_offline_pkg.py 生成，勿手改）：
#  1) 向上找项目根（含 .playwright-browsers 的目录），插入 sys.path[0]
#  2) 把 Playwright/Patchright 浏览器路径指向包内 .playwright-browsers
import os
import sys
from pathlib import Path

_cur = Path(__file__).resolve().parent
for _p in [_cur, *_cur.parents]:
    _b = _p / ".playwright-browsers"
    if _b.is_dir():
        _root = str(_p)
        if _root not in sys.path:
            sys.path.insert(0, _root)
        _b = str(_b)
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _b)
        os.environ.setdefault("PATCHRIGHT_BROWSERS_PATH", _b)
        break
'''
    (site_pkgs / "sitecustomize.py").write_text(code, encoding="utf-8")
    print(f"sitecustomize 已写入: {site_pkgs / 'sitecustomize.py'}")


def _find_site_packages() -> Path | None:
    r = run([VPY, "-c", "import site; print(site.getsitepackages()[0])"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _stage_biliup() -> None:
    """把构建机 ~/.social-auto-upload/tools/biliup 拷入项目 tools/biliup，B 站功能离线可用。"""
    src = Path.home() / ".social-auto-upload" / "tools" / "biliup"
    dst = ROOT / "tools" / "biliup"
    if not src.exists():
        print("[biliup] 未找到构建机缓存 ~/.social-auto-upload/tools/biliup，跳过（B 站功能将需联网下载）")
        return
    if dst.exists():
        print(f"[biliup] 已存在 {dst}，跳过复制")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"[biliup] 已复制 {src} → {dst}（包内离线可用）")


def _strip_xattrs(path: Path) -> None:
    """剥离 macOS 隔离标记（quarantine/provenance），避免 .so/浏览器被系统策略拒绝加载。"""
    if IS_WIN:
        return
    for cmd in (["xattr", "-cr", str(path)],):
        r = run(cmd)
        if r.returncode != 0:
            print(f"[xattr] 清理失败（可忽略）: {path}")


def make_launchers() -> None:
    if IS_WIN:
        (ROOT / "验证环境.bat").write_text(
            "@echo off\r\ncd /d %~dp0\r\n"
            "set PLAYWRIGHT_BROWSERS_PATH=%~dp0.playwright-browsers\r\n"
            "echo [social-auto-upload] 验证环境...\r\n"
            ".venv\\Scripts\\sau --help\r\n"
            "echo.\r\n"
            "echo 环境 OK。CLI: .venv\\Scripts\\sau douyin upload-video ...\r\n"
            "echo MCP: .venv\\Scripts\\python.exe mcp_server/server.py\r\n"
            "pause\r\n", encoding="utf-8")
    else:
        cmd = ROOT / "验证环境.command"
        cmd.write_text(
            "#!/bin/bash\n"
            'cd "$(dirname "$0")"\n'
            "\n"
            "# 剥离 macOS 隔离标记（压缩包经网盘/微信传输后带 quarantine，解压会传播到所有文件，\n"
            "# 不剥离会导致 numpy 等动态库被系统拒绝加载、chromium 启动即被杀）。\n"
            'xattr -cr "$(pwd)" 2>/dev/null\n'
            "\n"
            'export PLAYWRIGHT_BROWSERS_PATH="$(pwd)/.playwright-browsers"\n'
            'export PATCHRIGHT_BROWSERS_PATH="$(pwd)/.playwright-browsers"\n'
            'echo "[social-auto-upload] 验证环境（首次运行会剥离系统隔离标记，约需几秒）..."\n'
            'echo ""\n'
            'echo "── 1/2 检查 CLI ──"\n'
            "./.venv/bin/sau --help >/dev/null 2>&1 && echo \"CLI  ✅ 正常\" || { echo \"CLI  ❌ 失败，错误如下：\"; ./.venv/bin/sau --help 2>&1 | tail -20; exit 1; }\n"
            'echo ""\n'
            'echo "── 2/2 检查浏览器（chromium headless 启动）──"\n'
            "./.venv/bin/python3.12 - <<'PY'\n"
            "from patchright.sync_api import sync_playwright\n"
            "try:\n"
            "    with sync_playwright() as p:\n"
            "        b = p.chromium.launch(headless=True)\n"
            "        b.close()\n"
            '    print("浏览器  ✅ 正常")\n'
            "except Exception as e:\n"
            '    print("浏览器  ❌ 失败:", e)\n'
            "    raise SystemExit(1)\n"
            "PY\n"
            'echo ""\n'
            'echo "环境 OK ✅  可开始使用。CLI 示例："\n'
            'echo "  ./.venv/bin/sau douyin upload-video --account 账号名 --file videos/demo.mp4 --title 标题"\n'
            'echo ""\n'
            'echo "MCP 配置（加入 ~/.workbuddy/mcp.json 的 mcpServers 字段）："\n'
            'echo "  {"\n'
            'echo "    \\"social-auto-upload\\": {"\n'
            'echo "      \\"command\\": \\"$(pwd)/.venv/bin/python3.12\\","\n'
            'echo "      \\"args\\": [\\"$(pwd)/mcp_server/server.py\\"],"\n'
            'echo "      \\"env\\": { \\"MCP_TRANSPORT\\": \\"stdio\\" },"\n'
            'echo "      \\"disabled\\": false"\n'
            'echo "    }"\n'
            'echo "  }"\n'
            'echo ""\n'
            'echo "首次使用：./.venv/bin/sau <平台> login --account 你的账号名 --headed（cookie 与设备绑定，需本机扫码）"\n'
            'echo "完整指南见 docs/OFFLINE_GUIDE.md"\n',
            encoding="utf-8",
        )
        cmd.chmod(0o755)
        print("验证环境.command 已生成（含 xattr 剥离 + CLI/浏览器自检）")


def _excludes():
    # 注意：conf.py 必须打进离线包（sau_cli 运行必需；内容是默认模板无密钥）
    return {
        ".git", "cookies", "mcp_data", "logs", "dist", "__pycache__",
        ".pytest_cache", "*.pyc", ".DS_Store", "social_auto_upload.egg-info",
        ".venv/lib/python3*/site-packages/pip", "build", "*.egg-info",
    }


def _should_exclude(rel: str) -> bool:
    parts = rel.split("/")
    for ex in _excludes():
        if ex.endswith("*"):
            if rel.endswith(ex[1:]):
                return True
        elif ex in parts:
            return True
    return False


def make_pkg() -> Path:
    """macOS/Linux 用 tar.gz（保留符号链接）；Windows 用 zip（venv 自包含）。"""
    banner("打包")
    platform_tag = f"{'win' if IS_WIN else 'mac'}-{os.uname().machine if hasattr(os, 'uname') else 'x86_64'}"
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"social-auto-upload-离线版-{platform_tag}.{'zip' if IS_WIN else 'tar.gz'}"
    if out.exists():
        out.unlink()

    count = 0
    if IS_WIN:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f in ROOT.rglob("*"):
                if f.is_dir() or not f.exists() or f.name == ".git":
                    continue
                rel = f.relative_to(ROOT).as_posix()
                if _should_exclude(rel):
                    continue
                zf.write(f, f"social-auto-upload/{rel}")
                count += 1
    else:
        # tar.gz：保留符号链接（dereference=False）
        with tarfile.open(out, "w:gz", compresslevel=6) as tf:
            for f in ROOT.rglob("*"):
                if f.name == ".git" or not f.exists():
                    continue
                rel = f.relative_to(ROOT).as_posix()
                if _should_exclude(rel):
                    continue
                if f.is_dir():
                    continue  # tarfile.add 会递归
                try:
                    tf.add(f, arcname=f"social-auto-upload/{rel}", recursive=False)
                except (OSError, ValueError):
                    continue
                count += 1
    print(f"打包完成: {out}（{count} 个文件）")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-env", action="store_true")
    args = ap.parse_args()

    if not args.skip_env:
        build_env()
    else:
        print("--skip-env：跳过环境构建，仅打包")

    # B 站离线二进制暂存（无论是否 skip-env 都执行）
    _stage_biliup()

    make_launchers()

    # 打包前清理源目录隔离标记（本机后续直接拷贝目录时同样可用）
    if not IS_WIN:
        for p in (VENV, RUNTIME, BROWSERS, ROOT / "tools"):
            if p.exists():
                _strip_xattrs(p)

    out = make_pkg()

    # 打包后清理产物自身隔离标记（本机直接分发/AirDrop 场景）
    if not IS_WIN:
        _strip_xattrs(out)

    size = out.stat().st_size / 1024 / 1024
    print(f"\n✅ 离线包: {out}（{size:.0f} MB）")
    print("同事部署：解压 → 运行「验证环境.command」（自动剥离隔离标记+自检）→ 按 docs/OFFLINE_GUIDE.md 配 MCP")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
social-auto-upload (sau) + MCP 一键环境安装脚本  【跨平台：Windows / macOS / Linux】

在「本机」首次部署时使用一次即可：
  1. 用运行本脚本的 Python 创建隔离 venv（要求 3.10 <= 版本 < 3.13，推荐 3.12）
  2. 安装 sau（editable）+ playwright + fastmcp
  3. 下载 playwright chromium 浏览器（发布/扫码所需）
  4. 生成 conf.py（若不存在）

用法：
  python3 setup_env.py
  # 或指定 python3.12：
  /path/to/python3.12 setup_env.py

完成后，按 EXPORT.md 把 mcp.json 指到 <项目>/.venv 下的 python 即可。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import venv

# 国内加速：默认走 npmmirror 镜像下载 Chromium，
# 避免从微软 Azure CDN（playwright.azureedge.net）跨海拉取过慢。
# 若已自行 export PLAYWRIGHT_DOWNLOAD_HOST，则以你的为准（setdefault 不覆盖）。
os.environ.setdefault("PLAYWRIGHT_DOWNLOAD_HOST", "https://cdn.npmmirror.com/binaries/playwright")

# 国内加速：pip 也默认走清华镜像，避免从 pypi.org 拉 sau/playwright/fastmcp 及其依赖过慢。
# 若已自行 export PIP_INDEX_URL，则以你的为准（setdefault 不覆盖）。
os.environ.setdefault("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")

PROJECT = pathlib.Path(__file__).resolve().parent
VENV = PROJECT / ".venv"

# sau 上游要求：>=3.10, <3.13
MIN, MAX = (3, 10), (3, 13)


def banner(msg: str) -> None:
    print("\n" + "=" * 60 + f"\n{msg}\n" + "=" * 60)


def check_python() -> pathlib.Path:
    v = sys.version_info[:2]
    if not (MIN <= v < MAX):
        banner(f"❌ Python 版本不兼容：当前 {sys.version.split()[0]}，需要 {MIN[0]}.{MIN[1]}~{MAX[0]}.{MAX[1]-1}")
        print("请先安装 Python 3.12：")
        print("  macOS : brew install python@3.12")
        print("  Windows: https://www.python.org/downloads/release/python-3120/")
        print("然后重新运行：python3.12 setup_env.py")
        sys.exit(1)
    return pathlib.Path(sys.executable)


def run(cmd: list[str], **kw) -> None:
    print("+ " + " ".join(str(c) for c in cmd))
    try:
        subprocess.run(cmd, check=True, cwd=PROJECT, **kw)
    except subprocess.CalledProcessError as e:
        banner(f"❌ 命令失败（退出码 {e.returncode}）：{' '.join(cmd)}")
        sys.exit(e.returncode)


def main() -> None:
    py = check_python()
    banner(f"开始部署 social-auto-upload 环境\n项目: {PROJECT}\nPython: {sys.version.split()[0]}")

    # 1) 创建 venv
    if VENV.exists():
        print(f"[跳过] venv 已存在: {VENV}")
    else:
        banner("1/4 创建隔离 venv")
        venv.create(VENV, with_pip=True)

    vexe = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    # 2) 安装依赖
    pip_index = os.environ.get("PIP_INDEX_URL", "(默认官方源 pypi.org)")
    banner(f"2/4 安装依赖 (sau + playwright + fastmcp)\npip 源: {pip_index}")
    run([str(vexe), "-m", "pip", "install", "-U", "pip"])
    run([str(vexe), "-m", "pip", "install", "-e", "."])
    run([str(vexe), "-m", "pip", "install", "playwright==1.58.0", "fastmcp", "pillow"])

    # 3) 浏览器
    dl_host = os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST", "(默认官方源)")
    banner(f"3/4 下载 playwright chromium 浏览器\n下载源: {dl_host}")
    run([str(vexe), "-m", "playwright", "install", "chromium"])

    # 4) conf.py
    banner("4/4 生成 conf.py")
    if (PROJECT / "conf.py").exists():
        print("[跳过] conf.py 已存在")
    else:
        shutil.copy(PROJECT / "conf.example.py", PROJECT / "conf.py")
        print("[完成] 已复制 conf.example.py -> conf.py")

    vb = VENV / ("Scripts" if os.name == "nt" else "bin")
    banner("✅ 环境部署完成")
    print("MCP 启动用的 python：")
    print(f"  {vexe}")
    print("\n下一步：")
    print("  1) 按 EXPORT.md 配置 WorkBuddy 的 mcp.json（command 指向上面的 python）")
    print("  2) 首次绑定账号（需在本机弹浏览器扫码）：")
    print(f"     {vb}/sau douyin login --account 你的账号名 --headed")
    print("\n注意：账号 cookie 与设备绑定，必须在每台机器各自扫码绑定，不能复制他人 cookie。")


if __name__ == "__main__":
    main()

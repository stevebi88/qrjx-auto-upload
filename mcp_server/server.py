"""
social-auto-upload MCP Server
================================

把 dreammis/social-auto-upload (sau CLI) 封装成 MCP 工具，让 WorkBuddy Agent
能用自然语言驱动多平台、多账号的短视频/图文自动发布与定时任务。

运行环境要求：
- 使用隔离 venv 中的 python (Python 3.12) 与 sau CLI
- sau 必须在项目目录内运行（依赖本地 conf.py）
- 浏览器自动化平台需要 playwright chromium 已安装

工具清单：
- account_bind      绑定某平台某账号（触发登录，返回二维码/验证码提示）
- account_list      列出已绑定账号（平台+名称+状态）
- account_check     校验账号登录有效性
- publish_video     发布视频（支持多账号、话题、定时）
- publish_note      发布图文（抖音/快手/小红书）
- schedule_task     写入定时发布队列（SQLite）
- task_status       查询任务/发布结果
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# 路径与运行配置（跨平台：不写死任何绝对路径，全部从文件位置/解释器推导）
# --------------------------------------------------------------------------- #
# server.py 位于 <项目>/mcp_server/server.py，上一级即为项目根
PROJECT_DIR = Path(__file__).resolve().parent.parent  # social-auto-upload/
# 从运行该 server 的 python 解释器推导 venv 位置：
#   - Windows 下 venv 可执行在 Scripts/sau.exe
#   - macOS / Linux 下在 bin/sau
# 这样无论 venv 建在哪、叫什么名字，都能正确找到 sau CLI
_VENV_BIN = Path(sys.executable).parent
_SAU_NAME = "sau.exe" if os.name == "nt" else "sau"
SAU_BIN = _VENV_BIN / _SAU_NAME
PY_BIN = Path(sys.executable)
DATA_DIR = PROJECT_DIR / "mcp_data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "tasks.db"
ACCOUNTS_JSON = DATA_DIR / "accounts.json"

# 平台 -> sau 关键字映射
PLATFORMS = {
    "douyin": "抖音",
    "kuaishou": "快手",
    "xiaohongshu": "小红书",
    "bilibili": "Bilibili",
    "tencent": "视频号",
    "baijiahao": "百家号",
    "alipay": "支付宝生活号",
    "weibo": "微博",
    "hupu": "虎扑",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "sohu": "搜狐号",
    "pinduoduo": "多多视频",
}
# 支持图文的平台
NOTE_PLATFORMS = {"douyin", "kuaishou", "xiaohongshu"}

mcp = FastMCP("social-auto-upload")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _run_sau(args: list[str], timeout: int = 600) -> dict[str, Any]:
    """调用 sau CLI，返回 {ok, returncode, stdout, stderr}。"""
    if not SAU_BIN.exists():
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"sau 不存在: {SAU_BIN}"}
    try:
        proc = subprocess.run(
            [str(SAU_BIN), *args],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"超时({timeout}s)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _load_accounts() -> dict[str, Any]:
    if ACCOUNTS_JSON.exists():
        try:
            return json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_accounts(data: dict[str, Any]) -> None:
    ACCOUNTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            type TEXT,
            platform TEXT,
            accounts TEXT,
            payload TEXT,
            schedule_at TEXT,
            status TEXT,
            result TEXT,
            created_at TEXT,
            updated_at TEXT
        )"""
    )
    conn.commit()
    conn.close()


_init_db()


# --------------------------------------------------------------------------- #
# MCP 工具
# --------------------------------------------------------------------------- #
@mcp.tool()
def account_bind(platform: str, account_name: str, headless: bool = True) -> dict[str, Any]:
    """绑定某平台的某个账号，触发登录流程。

    Args:
        platform: 平台关键字，可选 douyin/kuaishou/xiaohongshu/bilibili/tencent/baijiahao/alipay/weibo/hupu/youtube/tiktok/sohu/pinduoduo
        account_name: 账号别名（自定义，如 '品牌号'），用于后续发布时指定
        headless: 是否无头浏览器。Mac 本机建议 False 以便看到扫码页面；远程/VPS 用 True

    Returns:
        含执行结果与提示。若需扫码，stdout 会包含二维码路径或登录链接。
    """
    platform = platform.lower()
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"不支持的平台: {platform}，可选: {list(PLATFORMS)}"}
    res = _run_sau([platform, "login", "--account", account_name, "--headless" if headless else "--no-headless"], timeout=900)
    # 记录绑定意图（成功与否都记，供 list 展示）
    accounts = _load_accounts()
    key = f"{platform}:{account_name}"
    accounts[key] = {
        "platform": platform,
        "account_name": account_name,
        "bound_at": datetime.now().isoformat(timespec="seconds"),
        "last_bind_ok": res["ok"],
    }
    _save_accounts(accounts)
    return {
        "ok": res["ok"],
        "platform": platform,
        "account_name": account_name,
        "stdout": res["stdout"],
        "stderr": res["stderr"],
        "hint": "若 stdout 含二维码路径，请扫码后重试 check；抖音短信验证可把验证码写入项目根目录 verify_code.txt",
    }


@mcp.tool()
def account_list() -> dict[str, Any]:
    """列出所有已登记（含尝试绑定）的账号。

    Returns:
        账号清单，每项含 platform/account_name/bound_at/last_bind_ok。
    """
    accounts = _load_accounts()
    items = list(accounts.values())
    return {"ok": True, "count": len(items), "accounts": items}


@mcp.tool()
def account_check(platform: str, account_name: str) -> dict[str, Any]:
    """校验某账号登录有效性（cookie 是否过期）。

    Args:
        platform: 平台关键字
        account_name: 账号别名

    Returns:
        {ok, valid} valid=true 表示仍有效。
    """
    platform = platform.lower()
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"不支持的平台: {platform}"}
    res = _run_sau([platform, "check", "--account", account_name], timeout=300)
    valid = res["ok"] and "valid" in res["stdout"].lower()
    # 同步状态
    accounts = _load_accounts()
    key = f"{platform}:{account_name}"
    if key in accounts:
        accounts[key]["last_check_ok"] = valid
        accounts[key]["last_check_at"] = datetime.now().isoformat(timespec="seconds")
        _save_accounts(accounts)
    return {"ok": True, "valid": valid, "platform": platform, "account_name": account_name, "raw": res["stdout"]}


@mcp.tool()
def publish_video(
    platform: str,
    account_names: list[str],
    video_file: str,
    title: str,
    desc: str = "",
    tags: list[str] | None = None,
    schedule_at: str | None = None,
    thumbnail: str | None = None,
) -> dict[str, Any]:
    """发布视频到指定平台的多个账号。

    Args:
        platform: 平台关键字（支持全部 11 个）
        account_names: 账号别名列表，可多个实现矩阵发布
        video_file: 视频文件绝对路径
        title: 标题
        desc: 简介/描述
        tags: 话题标签列表（无需带 #）
        schedule_at: 可选，定时发布时间 ISO 格式（如 2026-08-25T09:00）；为空则立即发布
        thumbnail: 可选封面图路径

    支持定时发布的平台（底层 SCHEDULED 策略）：douyin/kuaishou/tencent；其余平台定时将由调度器到点调用。
    """
    platform = platform.lower()
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"不支持的平台: {platform}"}
    if not Path(video_file).exists():
        return {"ok": False, "error": f"视频文件不存在: {video_file}"}
    tags_str = ",".join(tags) if tags else ""
    payload = {
        "platform": platform,
        "video_file": video_file,
        "title": title,
        "desc": desc,
        "tags": tags_str,
        "thumbnail": thumbnail,
    }
    if schedule_at:
        # 写入调度队列
        task_id = f"v_{int(time.time()*1000)}"
        _enqueue(task_id, "video", platform, account_names, payload, schedule_at)
        return {"ok": True, "scheduled": True, "task_id": task_id, "schedule_at": schedule_at, "accounts": account_names}
    # 立即发布
    results = []
    for acc in account_names:
        cmd = [platform, "upload-video", "--account", acc, "--file", video_file, "--title", title]
        if desc:
            cmd += ["--desc", desc]
        if tags_str:
            cmd += ["--tags", tags_str]
        if thumbnail:
            cmd += ["--thumbnail", thumbnail]
        res = _run_sau(cmd, timeout=900)
        results.append({"account": acc, "ok": res["ok"], "stdout": res["stdout"], "stderr": res["stderr"]})
    return {"ok": all(r["ok"] for r in results), "results": results}


@mcp.tool()
def publish_note(
    platform: str,
    account_names: list[str],
    images: list[str],
    title: str,
    note: str,
    tags: list[str] | None = None,
    schedule_at: str | None = None,
) -> dict[str, Any]:
    """发布图文到指定平台的多个账号（仅抖音/快手/小红书支持）。

    Args:
        platform: 仅 douyin/kuaishou/xiaohongshu
        account_names: 账号别名列表
        images: 图片文件绝对路径列表
        title: 标题
        note: 正文
        tags: 话题标签
        schedule_at: 可选定时发布
    """
    platform = platform.lower()
    if platform not in NOTE_PLATFORMS:
        return {"ok": False, "error": f"该平台不支持图文: {platform}，仅支持 {sorted(NOTE_PLATFORMS)}"}
    for im in images:
        if not Path(im).exists():
            return {"ok": False, "error": f"图片不存在: {im}"}
    tags_str = ",".join(tags) if tags else ""
    payload = {"platform": platform, "images": images, "title": title, "note": note, "tags": tags_str}
    if schedule_at:
        task_id = f"n_{int(time.time()*1000)}"
        _enqueue(task_id, "note", platform, account_names, payload, schedule_at)
        return {"ok": True, "scheduled": True, "task_id": task_id, "schedule_at": schedule_at, "accounts": account_names}
    results = []
    for acc in account_names:
        cmd = [platform, "upload-note", "--account", acc, "--images", *images, "--title", title, "--note", note]
        if tags_str:
            cmd += ["--tags", tags_str]
        res = _run_sau(cmd, timeout=900)
        results.append({"account": acc, "ok": res["ok"], "stdout": res["stdout"], "stderr": res["stderr"]})
    return {"ok": all(r["ok"] for r in results), "results": results}


def _enqueue(task_id: str, typ: str, platform: str, accounts: list[str], payload: dict, schedule_at: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, typ, platform, json.dumps(accounts, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False), schedule_at, "pending",
            "", datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()


@mcp.tool()
def schedule_task(task_id: str, action: str = "now") -> dict[str, Any]:
    """手动查看或触发一个定时任务。

    Args:
        task_id: 任务ID
        action: 'now' 查看；'run' 立即执行（忽略定时）
    """
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": f"任务不存在: {task_id}"}
    if action == "run":
        return _execute_task(row)
    cols = ["id", "type", "platform", "accounts", "payload", "schedule_at", "status", "result", "created_at", "updated_at"]
    return {"ok": True, "task": dict(zip(cols, row))}


@mcp.tool()
def task_status(limit: int = 20) -> dict[str, Any]:
    """查询最近的发布/定时任务状态。

    Args:
        limit: 返回最近 N 条
    """
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT id,type,platform,accounts,status,schedule_at,updated_at FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    tasks = [
        {"id": r[0], "type": r[1], "platform": r[2], "accounts": json.loads(r[3]), "status": r[4], "schedule_at": r[5], "updated_at": r[6]}
        for r in rows
    ]
    return {"ok": True, "count": len(tasks), "tasks": tasks}


def _execute_task(row) -> dict[str, Any]:
    """执行一个 pending 任务（被调度器或手动 run 调用）。"""
    task_id, typ, platform, accounts_json, payload_json, schedule_at, status, result, created_at, updated_at = row
    accounts = json.loads(accounts_json)
    payload = json.loads(payload_json)
    results = []
    if typ == "video":
        for acc in accounts:
            cmd = [platform, "upload-video", "--account", acc, "--file", payload["video_file"], "--title", payload["title"]]
            if payload.get("desc"):
                cmd += ["--desc", payload["desc"]]
            if payload.get("tags"):
                cmd += ["--tags", payload["tags"]]
            if payload.get("thumbnail"):
                cmd += ["--thumbnail", payload["thumbnail"]]
            res = _run_sau(cmd, timeout=900)
            results.append({"account": acc, "ok": res["ok"], "stdout": res["stdout"]})
    elif typ == "note":
        for acc in accounts:
            cmd = [platform, "upload-note", "--account", acc, "--images", *payload["images"], "--title", payload["title"], "--note", payload["note"]]
            if payload.get("tags"):
                cmd += ["--tags", payload["tags"]]
            res = _run_sau(cmd, timeout=900)
            results.append({"account": acc, "ok": res["ok"], "stdout": res["stdout"]})
    new_status = "done" if all(r["ok"] for r in results) else "failed"
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?", (new_status, json.dumps(results, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), task_id))
    conn.commit()
    conn.close()
    return {"ok": new_status == "done", "task_id": task_id, "status": new_status, "results": results}


# --------------------------------------------------------------------------- #
# 调度器（后台线程，扫描到期任务）
# --------------------------------------------------------------------------- #
def _scheduler_loop() -> None:
    while True:
        try:
            now = datetime.now()
            conn = sqlite3.connect(str(DB_PATH))
            due = conn.execute("SELECT * FROM tasks WHERE status='pending' AND schedule_at IS NOT NULL AND schedule_at <= ?", (now.isoformat(timespec="seconds"),)).fetchall()
            conn.close()
            for row in due:
                try:
                    _execute_task(row)
                except Exception as exc:  # noqa: BLE001
                    conn = sqlite3.connect(str(DB_PATH))
                    conn.execute("UPDATE tasks SET status='failed', result=?, updated_at=? WHERE id=?", (str(exc), datetime.now().isoformat(timespec="seconds"), row[0]))
                    conn.commit()
                    conn.close()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(30)


import threading  # noqa: E402

threading.Thread(target=_scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    mcp.run()

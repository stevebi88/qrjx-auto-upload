from __future__ import annotations

"""拟人化行为工具：随机延迟，降低"机器行为"特征被平台风控识别的概率。

用法：
    from utils.human_behavior import human_sleep
    await human_sleep(0.8, 2.4)   # 随机 0.8~2.4 秒

固定 sleep（如 0.5s/1s/2s）在风控模型里是典型的自动化信号，
改为区间随机后，操作节奏更接近真人。
"""
import asyncio
import json
import random
from datetime import datetime
from pathlib import Path

from conf import BASE_DIR

PUBLISH_HISTORY_FILE = BASE_DIR / "logs" / "publish_history.json"


async def human_sleep(min_seconds: float = 0.8, max_seconds: float = 2.4) -> None:
    """随机休眠 [min_seconds, max_seconds] 秒，模拟真人操作间隔。"""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


def human_sleep_sync(min_seconds: float = 0.8, max_seconds: float = 2.4) -> None:
    """同步版本（供非 async 流程使用）。"""
    import time

    time.sleep(random.uniform(min_seconds, max_seconds))


# ===== 发布节流（借鉴蚁小二：频率受控，避免高频率真实发布触发风控） =====

def _load_history() -> list[dict]:
    """读取本地发布历史（logs/publish_history.json）。"""
    try:
        with open(PUBLISH_HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def check_publish_allowed(
    platform: str,
    account: str,
    daily_limit: int = 2,
    min_interval_min: int = 120,
    hard_window: tuple[int, int] | None = None,
) -> tuple[bool, str]:
    """真实发布节流检查。返回 (是否允许, 拒绝原因)。

    - daily_limit: 每账号每日最大真实发布数（0 = 不限）
    - min_interval_min: 同账号两次真实发布的最小间隔（分钟）
    - hard_window: 发布小时窗口 (start, end)，窗口外硬拒；None = 不检查
    存草稿（draft=True）记录不计入节流统计。
    """
    history = _load_history()
    key = f"{platform}:{account}"
    # 只统计真实发布（非草稿）
    entries = [e for e in history if e.get("key") == key and not e.get("draft")]

    today = datetime.now().strftime("%Y-%m-%d")
    today_count = sum(1 for e in entries if str(e.get("ts", "")).startswith(today))
    if daily_limit and today_count >= daily_limit:
        return False, f"今日（{today}）已真实发布 {today_count} 条，达到上限 {daily_limit} 条"

    if min_interval_min and entries:
        last_ts = max(str(e.get("ts", "")) for e in entries)
        try:
            last_dt = datetime.fromisoformat(last_ts)
            elapsed_min = (datetime.now() - last_dt).total_seconds() / 60
            if elapsed_min < min_interval_min:
                return False, (
                    f"距上次真实发布仅 {elapsed_min:.0f} 分钟，"
                    f"低于最小间隔 {min_interval_min} 分钟"
                )
        except ValueError:
            pass

    if hard_window:
        start_h, end_h = hard_window
        now_h = datetime.now().hour
        if not (start_h <= now_h < end_h):
            return False, f"当前 {now_h} 点不在发布窗口（{start_h}-{end_h} 点）"

    return True, ""


def record_publish(platform: str, account: str, draft: bool = False) -> None:
    """记录一次发布（draft=True 表示存草稿，不占节流额度）。"""
    history = _load_history()
    history.append(
        {
            "key": f"{platform}:{account}",
            "platform": platform,
            "account": account,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "draft": draft,
        }
    )
    # 只保留最近 500 条，避免文件无限增长
    history = history[-500:]
    PUBLISH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PUBLISH_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

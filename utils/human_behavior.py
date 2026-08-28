from __future__ import annotations

"""拟人化行为工具：随机延迟，降低"机器行为"特征被平台风控识别的概率。

用法：
    from utils.human_behavior import human_sleep
    await human_sleep(0.8, 2.4)   # 随机 0.8~2.4 秒

固定 sleep（如 0.5s/1s/2s）在风控模型里是典型的自动化信号，
改为区间随机后，操作节奏更接近真人。
"""
import asyncio
import random


async def human_sleep(min_seconds: float = 0.8, max_seconds: float = 2.4) -> None:
    """随机休眠 [min_seconds, max_seconds] 秒，模拟真人操作间隔。"""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


def human_sleep_sync(min_seconds: float = 0.8, max_seconds: float = 2.4) -> None:
    """同步版本（供非 async 流程使用）。"""
    import time

    time.sleep(random.uniform(min_seconds, max_seconds))

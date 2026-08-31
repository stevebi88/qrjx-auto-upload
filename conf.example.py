from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
XHS_SERVER = "http://127.0.0.1:11901"  # only used by xhs-related flows
LOCAL_CHROME_PATH = ""  # optional, e.g. C:/Program Files/Google/Chrome/Application/chrome.exe
# 默认 headed（有界面）：小红书等平台对 headless 指纹检测严格（即使有 stealth 也会被识别），
# 用有界面模式发布可显著降低被风控标记的概率。需要静默运行时可临时 --headless 或改回 True。
LOCAL_CHROME_HEADLESS = False  # default headless behavior for uploader/examples

DEBUG_MODE = True  # default debug behavior
# Optional proxy for the YouTube uploader. Where youtube.com is blocked, direct
# connections time out and the (patchright) chromium does NOT use the system proxy.
# Point this at your local proxy port, e.g. "http://127.0.0.1:7890". None = no proxy.
YT_PROXY = None

# ===== 发布风控防护（借鉴蚁小二：频率节流 + 随机化，降低被平台风控标记概率） =====
# 背景：账号被封禁的根因之一是高频率真实发布 + 固定节奏。以下三项对「真实发布」生效，
# 存草稿（--draft）不占额度、不记入历史，用于验证表单交互。
PUBLISH_DAILY_LIMIT = 2          # 每账号每日最大真实发布数（0 = 不限）
PUBLISH_MIN_INTERVAL_MIN = 120   # 同账号两次真实发布的最小间隔（分钟）
PUBLISH_WINDOW = (9, 23)         # 推荐发布小时窗口 [start, end)（含头不含尾）
PUBLISH_WINDOW_HARD_BLOCK = False  # True = 窗口外硬拒；False = 仅警告（默认不阻塞手动发布）

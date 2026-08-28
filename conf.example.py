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

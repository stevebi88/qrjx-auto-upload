from pathlib import Path
from typing import List

from conf import BASE_DIR

SOCIAL_MEDIA_DOUYIN = "douyin"
SOCIAL_MEDIA_TENCENT = "tencent"
SOCIAL_MEDIA_TIKTOK = "tiktok"
SOCIAL_MEDIA_BILIBILI = "bilibili"
SOCIAL_MEDIA_KUAISHOU = "kuaishou"


def get_supported_social_media() -> List[str]:
    return [SOCIAL_MEDIA_DOUYIN, SOCIAL_MEDIA_TENCENT, SOCIAL_MEDIA_TIKTOK, SOCIAL_MEDIA_KUAISHOU]


def get_cli_action() -> List[str]:
    return ["upload", "login", "watch"]


async def set_init_script(context):
    stealth_js_path = Path(BASE_DIR / "utils/stealth.min.js")
    await context.add_init_script(path=stealth_js_path)
    return context


async def cdp_click(page, locator) -> bool:
    """CDP 真实鼠标点击（与真人/bsk 同机制，避免 force click 坐标偏差或 JS click 不触发 React 事件）。"""
    try:
        box = await locator.bounding_box()
        if not box:
            return False
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        return True
    except Exception:
        return False

# -*- coding: utf-8 -*-
"""搜狐视频创作者中心（搜狐号 / tv.sohu.com）视频上传 + 扫码登录。

功能：
  - sohu_cookie_gen: 有头/无头扫码登录（搜狐 passport 二维码，兼容 iframe 内嵌）
  - cookie_auth: 验证 cookie 是否有效
  - sohu_setup: 统一入口（检查/触发登录）
  - SohuVideo: 视频上传类

说明：首版选择器基于通用套路编写，页面 DOM 可能随平台改版变化；
联调失败时会在 logs/sohu_debug/ 自动落盘截图 + HTML 快照 + 控件清单，便于迭代。
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json as _json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Page, Playwright, async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.human_behavior import human_sleep
from utils.log import sohu_logger
from utils.login_qrcode import (
    build_login_qrcode_path,
    decode_qrcode_from_path,
    print_terminal_qrcode,
    remove_qrcode_file,
)

SOHU_INDEX_URL = "https://tv.sohu.com/s/center/index.html"
# 发布页候选 URL（第一个为官方发布入口，顺序尝试）
SOHU_PUBLISH_URLS = [
    "https://tv.sohu.com/s/center/index.html#/",
    "https://tv.sohu.com/s/center/index.html",
    "https://tv.sohu.com/s/center/upload",
]
# 登录跳转关键字（URL 含这些视为未登录/登录页）
SOHU_LOGIN_URL_MARKERS = ("passport.sohu.com", "login", "sso")


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _build_login_result(success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return
    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_launch_kwargs(headless: bool) -> dict:
    launch_kwargs = {"headless": headless}
    # 反检测：合并 browser_hook 的隐身启动参数（--disable-blink-features=AutomationControlled 等）
    try:
        from utils.browser_hook import get_browser_options
        _opts = get_browser_options()
        if _opts.get("args"):
            launch_kwargs["args"] = _opts["args"]
        if _opts.get("executable_path") and not launch_kwargs.get("executable_path"):
            launch_kwargs["executable_path"] = _opts["executable_path"]
    except Exception:
        pass
    if LOCAL_CHROME_PATH and not launch_kwargs.get("executable_path"):
        launch_kwargs["executable_path"] = LOCAL_CHROME_PATH
    return launch_kwargs


async def _stealth_context(context):
    """给浏览器上下文注入 stealth.min.js，隐藏自动化指纹（navigator.webdriver 等）。"""
    try:
        from utils.base_social_media import set_init_script
        return await set_init_script(context)
    except Exception:
        return context


def _resolve_account_file(account_file: str | Path) -> str:
    path = Path(account_file).expanduser()
    if path.is_absolute():
        return str(path)
    if len(path.parts) == 1:
        return str((Path(BASE_DIR) / "cookies" / "sohu_uploader" / path).resolve())
    return str(path.resolve())


async def _save_debug(page: Page, tag: str) -> None:
    """失败时落盘：截图 + HTML 快照 + 可见控件清单，方便迭代选择器。"""
    try:
        out_dir = Path(BASE_DIR) / "logs" / "sohu_debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=str(out_dir / f"{stamp}_{tag}.png"), full_page=False)
        html = await page.content()
        (out_dir / f"{stamp}_{tag}.html").write_text(html, encoding="utf-8")

        inventory: dict = {"url": page.url, "buttons": [], "inputs": [], "contenteditable": [], "file_inputs": [], "frames": []}
        for fr in page.frames:
            try:
                inventory["frames"].append(fr.url[:160])
                for el in await fr.locator("button, [role=button], a.btn, .btn").all():
                    txt = (await el.inner_text()).strip()
                    if txt and txt not in inventory["buttons"]:
                        inventory["buttons"].append(txt[:40])
                for el in await fr.locator("input").all():
                    inp_type = await el.get_attribute("type") or ""
                    ph = await el.get_attribute("placeholder") or ""
                    if inp_type == "file":
                        inventory["file_inputs"].append(ph)
                    else:
                        inventory["inputs"].append(f"{inp_type}|{ph}")
                for el in await fr.locator("[contenteditable=true]").all():
                    inventory["contenteditable"].append("CE")
            except Exception:
                continue
        (out_dir / f"{stamp}_{tag}.json").write_text(_json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        sohu_logger.warning(_msg("📸", f"调试快照已保存: {out_dir}/{stamp}_{tag}.png/.html/.json"))
    except Exception as exc:
        sohu_logger.warning(_msg("😵", f"保存调试快照失败: {exc}"))


async def _switch_to_qr_login(page: Page) -> bool:
    """切换到扫码登录 tab。

    搜狐创作者中心登录框默认停在「短信登录」tab，扫码面板 data-login-sdk="qrLogin" 是隐藏的，
    必须先点 <a data-sdk="qrLogin">扫码登录</a> 才会出二维码。
    """
    try:
        # 已在扫码 tab 则跳过
        selected = page.locator('.sh-login-tab a[data-sdk="qrLogin"].selected').first
        if await selected.count():
            return True
        tab = page.locator('a[data-sdk="qrLogin"], [data-login-sdk="qrLogin"]').first
        if not await tab.count():
            tab = page.get_by_text("扫码登录", exact=True).first
        if await tab.count() and await tab.is_visible():
            await tab.click(timeout=5000)
            await page.wait_for_timeout(1500)
            sohu_logger.info(_msg("🔄", "已切换到扫码登录 tab"))
            return True
    except Exception:
        pass
    return False


async def _extract_qrcode_src(page: Page) -> str:
    """从页面（含 iframe）里提取二维码图片 src（data URL 或可下载 URL）。"""
    # 1) 搜狐专属：qrlogin-img（登录框扫码 tab），含元素截图兜底
    for frame in page.frames:
        try:
            qr = frame.locator("img.qrlogin-img, [class*='qrlogin'] img").first
            if not await qr.count():
                continue
            await qr.wait_for(state="attached", timeout=8000)
            src = None
            for _ in range(20):
                src = await qr.get_attribute("src")
                if src:
                    break
                await page.wait_for_timeout(500)
            if not src:
                continue
            if src.startswith("data:image/"):
                return src
            abs_url = urljoin(frame.url, src)
            try:
                resp = await page.context.request.get(abs_url, timeout=15000)
                if resp.ok and resp.headers.get("content-type", "").startswith("image/"):
                    body = await resp.body()
                    content_type = resp.headers.get("content-type", "image/png").split(";")[0]
                    return f"data:{content_type};base64,{base64.b64encode(body).decode()}"
            except Exception:
                pass
            # 下载失败或非图片 → 元素截图兜底
            shot = await qr.screenshot()
            return f"data:image/png;base64,{base64.b64encode(shot).decode()}"
        except Exception:
            continue

    # 2) 通用兜底：扫描所有 frame 里的 img.qrcode / class*="qrcode" img
    for frame in page.frames:
        try:
            candidates = [
                frame.locator("img.qrcode").first,
                frame.locator('[class*="qrcode"] img, [class*="QRcode"] img').first,
            ]
            for qr in candidates:
                if not await qr.count():
                    continue
                await qr.wait_for(state="attached", timeout=5000)
                src = None
                for _ in range(20):
                    src = await qr.get_attribute("src")
                    if src:
                        break
                    await page.wait_for_timeout(500)
                if not src:
                    continue
                if src.startswith("data:image/"):
                    return src
                abs_url = urljoin(frame.url, src)
                resp = await page.context.request.get(abs_url)
                if resp.ok:
                    body = await resp.body()
                    content_type = resp.headers.get("content-type", "image/png").split(";")[0]
                    return f"data:{content_type};base64,{base64.b64encode(body).decode()}"
        except Exception:
            continue

    # 3) 主页面兜底选择器
    for selector in ["img.qrcode", '[class*="qrcode"] img', 'img[src^="data:image/"]']:
        try:
            qr = page.locator(selector).first
            if not await qr.count() or not await qr.is_visible():
                continue
            src = await qr.get_attribute("src")
            if src and src.startswith("data:image/"):
                return src
        except Exception:
            continue

    raise RuntimeError("未获取到搜狐登录二维码（页面上没找到二维码图片）")


async def _grab_qr(page: Page, account_file: str) -> dict:
    qrcode_src = await _extract_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="sohu_login_qrcode")
    qrcode_path.parent.mkdir(parents=True, exist_ok=True)
    if qrcode_src.startswith("data:"):
        import re as _re

        m = _re.match(r"data:[^;]+;base64,(.*)", qrcode_src, _re.S)
        if m:
            qrcode_path.write_bytes(base64.b64decode(m.group(1)))
        else:
            await page.screenshot(path=str(qrcode_path))
    else:
        await page.screenshot(path=str(qrcode_path))

    qrcode_content = decode_qrcode_from_path(qrcode_path)
    sohu_logger.info(_msg("🖼️", f"二维码已保存到: {qrcode_path}"))
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "搜狐APP/手机浏览器")
    else:
        sohu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    return {"image_path": str(qrcode_path), "image_data_url": qrcode_src}


async def _has_login_ui(page: Page) -> bool:
    """页面（含 iframe）里是否仍有登录要素：passport/login 框架、二维码图、登录按钮。

    用于防"弹窗/跳转导致离开登录页但并未真正登录成功"的误判。
    """
    for fr in page.frames:
        try:
            fr_url = fr.url.lower()
            if "passport" in fr_url or "login" in fr_url or "sso" in fr_url:
                return True
        except Exception:
            continue
    try:
        qr = page.locator("img.qrcode, [class*='qrcode'] img").first
        if await qr.count() and await qr.is_visible():
            return True
    except Exception:
        pass
    try:
        login_btn = page.get_by_text("登录", exact=True).first
        if await login_btn.count() and await login_btn.is_visible():
            return True
    except Exception:
        pass
    return False


async def _is_login_completed(page: Page) -> bool:
    """登录完成判断：URL 离开登录域 且 不再出现登录 UI 且 页面有实质内容（防误判）。"""
    url = page.url.lower()
    if any(m in url for m in SOHU_LOGIN_URL_MARKERS):
        return False
    await page.wait_for_timeout(1500)
    if await _has_login_ui(page):
        return False
    # 防"关掉登录弹窗后页面空白被误判成功"：登录后创作者中心应有实质内容
    try:
        body_text = await page.inner_text("body")
        if len(body_text.strip()) < 20:
            return False
    except Exception:
        return False
    return True


async def _is_qrcode_expired(page: Page) -> bool:
    """检测二维码是否已过期（出现"已过期/点击刷新"等提示）。"""
    for frame in page.frames:
        try:
            txt = (await frame.locator("body").inner_text(timeout=2000)).replace("\n", " ")
            if "二维码已过期" in txt or ("已过期" in txt and "刷新" in txt):
                return True
        except Exception:
            continue
    return False


async def _refresh_qrcode(page: Page) -> bool:
    """点击"刷新/点击刷新"让二维码重新生成。"""
    for frame in page.frames:
        try:
            candidates = [
                frame.get_by_text("点击刷新", exact=True).first,
                frame.get_by_text("刷新二维码", exact=True).first,
                frame.get_by_text("刷新", exact=True).first,
                frame.locator('button:has-text("刷新")').first,
                frame.locator('[class*="refresh"]').first,
            ]
            for c in candidates:
                if await c.count() and await c.is_visible():
                    await c.click(timeout=3000)
                    return True
        except Exception:
            continue
    return False


async def sohu_cookie_gen(account_file, qrcode_callback=None, poll_interval: int = 3, max_checks: int = 120, headless: bool = LOCAL_CHROME_HEADLESS):
    account_file = _resolve_account_file(account_file)
    Path(account_file).parent.mkdir(parents=True, exist_ok=True)
    qrcode_path = None
    result = _build_login_result(False, "failed", "搜狐号登录失败", account_file)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=headless))
        context = await _stealth_context(await browser.new_context())
        try:
            page = await context.new_page()
            await page.goto(SOHU_INDEX_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)

            # 登录框默认在「短信登录」tab，先切到「扫码登录」再抓二维码
            await _switch_to_qr_login(page)
            try:
                await page.locator("img.qrlogin-img, [class*='qrlogin'] img").first.wait_for(state="attached", timeout=10000)
            except Exception:
                pass

            if headless:
                sohu_logger.info(_msg("🧍", "无头登录中：二维码已存为图片，请用搜狐APP/手机扫码"))
            else:
                sohu_logger.info(_msg("🧍", "请在打开的浏览器中扫码登录搜狐号"))

            qrcode_info = await _grab_qr(page, account_file)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            await _emit_qrcode_callback(qrcode_callback, qrcode_info)
            sohu_logger.info(_msg("🧍", "请扫码，正在耐心等待登录完成（二维码过期会自动刷新，不用赶）"))

            # max_checks 默认 120 * 3s = 6 分钟；期间二维码过期会自动点刷新续命
            refresh_count = 0
            for i in range(max_checks):
                if await _is_login_completed(page):
                    sohu_logger.info(_msg("🥳", f"扫码成功，当前页面: {page.url}"))
                    result = _build_login_result(True, "success", "搜狐号扫码登录成功", account_file, qrcode_info, page.url)
                    break

                if await _is_qrcode_expired(page):
                    if await _refresh_qrcode(page):
                        refresh_count += 1
                        sohu_logger.info(_msg("🔄", f"二维码已过期，已自动刷新（第 {refresh_count} 次），请重新扫码"))
                        await page.wait_for_timeout(2000)
                        try:
                            qrcode_info = await _grab_qr(page, account_file)
                            new_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
                            if new_path and new_path != qrcode_path:
                                if remove_qrcode_file(qrcode_path):
                                    sohu_logger.info(_msg("🧹", f"旧二维码文件已清理: {qrcode_path}"))
                                qrcode_path = new_path
                            await _emit_qrcode_callback(qrcode_callback, qrcode_info)
                        except Exception as exc:
                            sohu_logger.warning(_msg("⚠️", f"刷新后重新抓取二维码失败: {exc}"))
                        continue
                    sohu_logger.info(_msg("🔄", "二维码已过期但未找到刷新按钮，等待页面自行刷新"))

                # 登录框还在但二维码不可见（可能被切到短信/账号 tab 或被弹窗遮住）→ 切回扫码
                if await _has_login_ui(page):
                    try:
                        qr = page.locator("img.qrlogin-img, [class*='qrlogin'] img").first
                        if not (await qr.count() and await qr.is_visible()):
                            sohu_logger.info(_msg("🔄", "登录框二维码不可见，尝试切回扫码登录"))
                            if await _switch_to_qr_login(page):
                                await page.wait_for_timeout(1500)
                                try:
                                    qrcode_info = await _grab_qr(page, account_file)
                                    new_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
                                    if new_path and new_path != qrcode_path:
                                        if remove_qrcode_file(qrcode_path):
                                            sohu_logger.info(_msg("🧹", f"旧二维码文件已清理: {qrcode_path}"))
                                        qrcode_path = new_path
                                    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
                                except Exception as exc:
                                    sohu_logger.warning(_msg("⚠️", f"切回扫码后重新抓取二维码失败: {exc}"))
                                continue
                    except Exception:
                        pass

                if i % 10 == 9:
                    sohu_logger.info(_msg("🧍", f"仍在等待扫码…（已等待 {round((i + 1) * poll_interval)}s，浏览器请保持打开）"))
                await page.wait_for_timeout(poll_interval * 1000)
            else:
                result = _build_login_result(False, "timeout", "等待搜狐号扫码登录超时", account_file, qrcode_info, page.url)

            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                sohu_logger.success(_msg("🥳", f"cookie 已保存: {account_file}"))
        except Exception as exc:
            if "page" in locals():
                await _save_debug(page, "login_error")
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                sohu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                sohu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
    return result


async def cookie_auth(account_file):
    account_file = _resolve_account_file(account_file)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=True))
        try:
            context = await _stealth_context(await browser.new_context(storage_state=account_file))
            page = await context.new_page()
            await page.goto(SOHU_INDEX_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)

            url = page.url.lower()
            if any(m in url for m in SOHU_LOGIN_URL_MARKERS):
                sohu_logger.info(_msg("🥹", "cookie 已失效（页面跳转到登录页）"))
                return False
            sohu_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            sohu_logger.warning(_msg("😵", f"cookie 校验出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def sohu_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS):
    account_file = _resolve_account_file(account_file)
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie 文件不存在或已失效", account_file)
            return result if return_detail else False
        sohu_logger.info(_msg("🥹", "cookie 文件不存在或已失效，自动打开浏览器请扫码登录"))
        result = await sohu_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie 有效", account_file)
    return result if return_detail else True


class SohuVideo(BaseVideoUploader):
    """搜狐视频创作者中心视频上传。"""

    def __init__(
        self,
        title,
        file_path,
        tags,
        account_file,
        publish_date=0,
        desc: str | None = None,
        thumbnail_path: str | None = None,
        debug: bool = True,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.account_file = _resolve_account_file(account_file)
        self.publish_date = publish_date
        self.desc = desc or ""
        self.thumbnail_path = thumbnail_path
        self.debug = debug
        self.headless = headless
        self.max_title_length = 40

    async def _wait_until_schedule(self) -> bool:
        """定时发布：等待到点再继续。返回 True 继续；False 表示 DRY-RUN 跳过真实发布。"""
        if not self.publish_date or isinstance(self.publish_date, int):
            return True
        now = datetime.now(tz=self.publish_date.tzinfo) if self.publish_date.tzinfo else datetime.now()
        delay = (self.publish_date - now).total_seconds()
        if delay <= 0:
            return True
        fmt = self.publish_date.strftime("%Y-%m-%d %H:%M")
        if os.environ.get("SAU_DRY_RUN_SCHEDULE"):
            sohu_logger.info(_msg("🧪", f"DRY-RUN 定时校验：目标 {fmt}，剩余 {int(delay)}s，跳过真实发布"))
            return False
        sohu_logger.info(_msg("⏰", f"定时发布：将在 {fmt} 发布（剩余 {int(delay)}s）"))
        while delay > 0:
            await asyncio.sleep(min(30, delay))
            now = datetime.now(tz=self.publish_date.tzinfo) if self.publish_date.tzinfo else datetime.now()
            delay = (self.publish_date - now).total_seconds()
        sohu_logger.info(_msg("🏃", "到点，开始发布"))
        return True

    async def validate_upload_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成搜狐号登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成搜狐号登录: {self.account_file}")
        if not self.title or not str(self.title).strip():
            raise ValueError("视频标题不能为空")
        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def upload(self, playwright: Playwright) -> None:
        sohu_logger.info(_msg("🧍", "先检查 cookie 和视频文件"))
        await self.validate_upload_args()
        sohu_logger.info(_msg("🥳", "上传前检查通过"))

        # 搜狐发布弹窗原生支持定时发布，无需进程内等待；DRY-RUN 仅做定时逻辑校验
        if os.environ.get("SAU_DRY_RUN_SCHEDULE") and self.publish_date:
            fmt = self.publish_date.strftime("%Y-%m-%d %H:%M")
            sohu_logger.info(_msg("🧪", f"DRY-RUN 定时校验：目标 {fmt}，跳过真实发布"))
            return

        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=self.headless))
        context = await _stealth_context(await browser.new_context(storage_state=self.account_file))
        try:
            page = await context.new_page()
            await self._goto_publish_page(page)
            sohu_logger.info(_msg("🏃", f"开始上传视频: {self.title}"))

            await self._set_video_file(page)
            await self._wait_form_ready(page)
            # 表单就绪 = 验证码已通过：立即保存 cookie，验证状态持久化（下次可 headless 免验证）
            await context.storage_state(path=self.account_file)
            sohu_logger.info(_msg("💾", "验证通过，cookie 已保存（后续可 --headless 免验证自动发布）"))
            await self._fill_title(page)
            await human_sleep(0.8, 2.0)
            await self._wait_upload_complete(page)
            if self.desc:
                await self._fill_desc(page)
                await human_sleep(0.8, 2.0)
            if self.tags:
                await self._fill_tags(page)
                await human_sleep(0.8, 2.0)
            if self.thumbnail_path:
                await self._upload_thumbnail(page)
            await self._submit_publish(page)

            await context.storage_state(path=self.account_file)
            sohu_logger.success(_msg("🥳", "cookie 更新完毕"))
        except Exception as exc:
            await _save_debug(page, "upload_error")
            raise
        finally:
            await context.close()
            await browser.close()

    async def _goto_publish_page(self, page: Page) -> None:
        last_err: Exception | None = None
        for url in SOHU_PUBLISH_URLS:
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
                fi = page.locator('input[type="file"]').first
                if await fi.count():
                    sohu_logger.info(_msg("🏃", f"已进入发布页: {url}"))
                    return
            except Exception as exc:
                last_err = exc
        # 都没找到文件框：尝试点「上传」按钮
        try:
            await page.goto(SOHU_INDEX_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            upload_btn = page.get_by_text("上传", exact=True).first
            if await upload_btn.count():
                await upload_btn.click(timeout=8000)
                await page.wait_for_timeout(4000)
                fi = page.locator('input[type="file"]').first
                if await fi.count():
                    sohu_logger.info(_msg("🏃", "已通过「上传」按钮进入发布页"))
                    return
        except Exception as exc:
            last_err = exc
        raise RuntimeError(f"未找到搜狐发布页（无文件选择框）: {last_err}")

    async def _set_video_file(self, page: Page) -> None:
        file_input = page.locator('input[type="file"][accept*="video"], input[type="file"][accept*="mp4"]').first
        if not await file_input.count():
            file_input = page.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=30000)
        await file_input.set_input_files(self.file_path)
        sohu_logger.info(_msg("🏃", f"已选择视频文件: {self.file_path}"))

    async def _wait_form_ready(self, page: Page, timeout: int = 300) -> None:
        """等待发布表单渲染（timeout 单位：秒）。

        headed 模式下遇到「点字验证码」会提示人工在浏览器完成并自动等待；
        headless 模式下直接报错，提示改用 --headed。
        """
        start = time.monotonic()
        captcha_hinted = False
        while time.monotonic() - start < timeout:
            try:
                body = await page.inner_text("body")
            except Exception:
                body = ""
            # 点字验证码拦截（选完视频后偶发出现）
            if "点字验证" in body or "请依次点击" in body or "依次点击" in body:
                if self.headless:
                    raise RuntimeError(
                        "搜狐触发「点字验证码」（反爬），headless 无法自动完成；请用 --headed 运行，由人工在浏览器弹窗中依次点击字符完成验证"
                    )
                if not captcha_hinted:
                    sohu_logger.warning(_msg("🔔", "检测到「点字验证码」：请在浏览器弹窗中依次点击指定字符完成验证（脚本将自动等待）"))
                    captcha_hinted = True
                await page.wait_for_timeout(4000)
                continue

            for loc in [
                page.locator("div[contenteditable=true]").first,
                page.locator('input[placeholder*="标题"], textarea[placeholder*="标题"]').first,
                page.locator('input[placeholder*="title"], textarea[placeholder*="title"]').first,
            ]:
                try:
                    await loc.wait_for(state="visible", timeout=8000)
                    sohu_logger.info(_msg("🏃", "发布表单已就绪"))
                    return
                except Exception:
                    continue
            await page.wait_for_timeout(1500)
        raise RuntimeError("等待发布表单超时（含人工验证码等待时间）")

    async def _fill_title(self, page: Page) -> None:
        title = str(self.title).strip()[: self.max_title_length]
        field = page.locator("div[contenteditable=true]").first
        if await field.count():
            await field.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            await field.fill(title)
        else:
            field = page.locator('input[placeholder*="标题"], textarea[placeholder*="标题"], input[placeholder*="title"]').first
            await field.wait_for(state="visible", timeout=15000)
            await field.fill(title)
        sohu_logger.info(_msg("🏷️", f"标题已填写: {title}"))

    async def _wait_upload_complete(self, page: Page, timeout: int = 600) -> None:
        import re as _re

        start = time.monotonic()
        seen_progress = False
        gone_count = 0
        while True:
            if time.monotonic() - start > timeout:
                sohu_logger.warning(_msg("⚠️", f"等待上传超时（>{timeout}s），继续后续步骤"))
                return
            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass
            if "上传失败" in body:
                raise RuntimeError("视频上传失败")
            m = _re.search(r"(\d{1,3})\s*%", body)
            pct = int(m.group(1)) if m else None
            if pct is not None and pct < 100:
                seen_progress = True
                gone_count = 0
                sohu_logger.info(_msg("🏃", f"上传中 {pct}%"))
                await asyncio.sleep(2)
                continue
            if seen_progress:
                gone_count += 1
                if gone_count >= 2:
                    sohu_logger.success(_msg("🥳", "视频上传完毕"))
                    return
                await asyncio.sleep(2)
                continue
            if time.monotonic() - start > 15:
                sohu_logger.success(_msg("🥳", "视频上传完毕"))
                return
            await asyncio.sleep(2)

    async def _fill_desc(self, page: Page) -> None:
        field = page.locator('textarea[placeholder*="简介"], textarea[placeholder*="描述"], input[placeholder*="简介"], input[placeholder*="描述"]').first
        if not await field.count():
            field = page.locator("textarea").first
        if not await field.count():
            # tiptap 简介编辑器：取标题之外的 contenteditable
            ces = page.locator("div[contenteditable=true]")
            for i in range(await ces.count()):
                el = ces.nth(i)
                try:
                    cls = await el.get_attribute("class") or ""
                    if "title" not in cls.lower():
                        field = el
                        break
                except Exception:
                    continue
        if not await field.count():
            sohu_logger.warning(_msg("⚠️", "未找到简介输入框，跳过简介"))
            return
        await field.click()
        await field.fill(str(self.desc)[:500])
        sohu_logger.info(_msg("📝", "简介已填写"))

    async def _fill_tags(self, page: Page) -> None:
        # 搜狐标签框：input.input-topic（圈子/作品集/播单同名但 readonly，用 :not([readonly]) 排除）
        field = page.locator('input.input-topic:not([readonly])').first
        if not await field.count():
            field = page.locator('input[placeholder*="标签"], input[placeholder*="话题"]').first
        if not await field.count():
            field = page.locator('textarea[placeholder*="标签"], textarea[placeholder*="话题"]').first
        if not await field.count():
            sohu_logger.warning(_msg("⚠️", "未找到标签输入框，跳过标签"))
            return
        await field.click()
        for t in self.tags:
            tag = str(t).lstrip("#").strip()
            if not tag:
                continue
            await field.fill(tag)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(600)
        sohu_logger.info(_msg("🏷️", f"标签已填写（逐个回车创建）: {', '.join(str(t) for t in self.tags)}"))

    async def _upload_thumbnail(self, page: Page) -> None:
        try:
            cover_entry = page.get_by_text("封面", exact=True).first
            if not await cover_entry.count():
                cover_entry = page.locator('button:has-text("封面"), [class*="cover"]').first
            if not await cover_entry.count():
                sohu_logger.warning(_msg("⚠️", "未找到封面上传入口，跳过封面"))
                return
            await cover_entry.scroll_into_view_if_needed()
            await cover_entry.click(timeout=8000)
            await page.wait_for_timeout(2000)
            img_input = page.locator('input[type="file"][accept*="image"]').first
            if not await img_input.count():
                img_input = page.locator('input[type="file"]').last
            await img_input.set_input_files(self.thumbnail_path)
            await page.wait_for_timeout(3000)
            confirm = page.locator('button:has-text("确定"), button:has-text("完成"), button:has-text("保存")').first
            if await confirm.count():
                await confirm.click(timeout=5000)
            sohu_logger.success(_msg("🖼️", "封面已上传"))
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"封面上传失败，跳过: {exc}"))

    async def _pick_radio(self, modal, text: str) -> bool:
        """在弹窗里点选 radio_item 文本（如「无需标注」）。"""
        for sel in [
            f'.radio_item:has-text("{text}")',
            f'label:has-text("{text}")',
            f'span:text-is("{text}")',
        ]:
            try:
                opt = modal.locator(sel).first
                if await opt.count():
                    await opt.click(timeout=4000)
                    return True
            except Exception:
                continue
        return False

    async def _select_region(self, modal, page: Page) -> None:
        """弹窗里的「地址/地区」字段：点开 select → 选中国。"""
        try:
            sel_el = None
            for sel in [
                'input[placeholder*="地址"]',
                'input[placeholder*="地点"]',
                'input[placeholder*="发生地点"]',
                '.form-item:has-text("地址") input',
                '.form-item:has-text("地区") input',
                '.form-item:has-text("地址") [class*="select"]',
                '.form-item:has-text("地区") [class*="select"]',
                'select',
            ]:
                try:
                    c = modal.locator(sel).first
                    if await c.count():
                        sel_el = c
                        break
                except Exception:
                    continue
            if sel_el is None:
                sohu_logger.warning(_msg("⚠️", "未找到「地址/地区」输入框，跳过"))
                return

            # 原生 select：直接 select_option
            try:
                tag = await sel_el.evaluate("e => e.tagName")
            except Exception:
                tag = ""
            if tag == "SELECT":
                # 优先按文本，其次按 value
                try:
                    await sel_el.select_option(label="中国")
                except Exception:
                    try:
                        await sel_el.select_option(value="中国")
                    except Exception:
                        pass
                sohu_logger.success(_msg("✅", "地址已选择：中国（原生 select）"))
                return

            # 自定义下拉：点开 → 选项列表可能在 modal 外
            await sel_el.scroll_into_view_if_needed()
            await sel_el.click(timeout=5000)
            await page.wait_for_timeout(1500)

            china = None
            for sel in [
                '[class*="option"]:has-text("中国"):not(:has-text("香港"))',
                'li:has-text("中国"):not(:has-text("香港"))',
                'span:text-is("中国")',
                'div:text-is("中国")',
                '[class*="dropdown"]:has-text("中国")',
            ]:
                try:
                    c = page.locator(sel).first
                    if await c.count() and await c.is_visible():
                        china = c
                        break
                except Exception:
                    continue
            if china is None:
                try:
                    china = page.get_by_text("中国", exact=True).first
                except Exception:
                    china = None
            if china is not None and await china.count() and await china.is_visible():
                await china.click(timeout=5000)
                sohu_logger.success(_msg("✅", "地址已选择：中国"))
            else:
                sohu_logger.warning(_msg("⚠️", "地址下拉里未找到「中国」选项，请人工在弹窗中选"))
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"设置地址失败: {exc}"))

    async def _set_publish_time(self, modal, page: Page) -> None:
        """弹窗里的「时间」字段：点击 input 打开日期选择器，选日期+时间，确认。

        搜狐用的是 readonly input + 自定义日期选择器（如 Element UI / 自研），不能直接 fill，
        必须点开选择器，选日期（点对应日号单元格），设小时/分钟，再点确定。
        """
        from datetime import timedelta

        if not self.publish_date or isinstance(self.publish_date, int):
            target_dt = datetime.now() + timedelta(hours=2)
            sohu_logger.info(_msg("🏃", "未指定 --schedule，时间默认设为当前+2小时"))
        else:
            target_dt = self.publish_date
        target_date_str = target_dt.strftime("%Y-%m-%d")
        target_time_str = target_dt.strftime("%H:%M")
        target_day = str(target_dt.day)
        target_hour = f"{target_dt.hour:02d}"
        target_minute = f"{target_dt.minute:02d}"

        inp = None
        for sel in [
            'input[placeholder*="时间"]',
            'input[placeholder*="发生时间"]',
            '.form-item:has-text("时间") input',
            '.form-item:has-text("发布时间") input',
        ]:
            try:
                c = modal.locator(sel).first
                if await c.count():
                    inp = c
                    break
            except Exception:
                continue
        if inp is None:
            sohu_logger.warning(_msg("⚠️", "未找到「时间」输入框，跳过定时设置"))
            return

        try:
            await inp.scroll_into_view_if_needed()
            await inp.click(timeout=5000)
            await page.wait_for_timeout(1500)
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"点时间输入框失败: {exc}"))
            return

        # 找弹出的日期选择器（可能在 page 级，不在 modal 内）
        picker = None
        for sel in [
            '.el-date-picker:visible',
            '.el-picker-panel:visible',
            '[class*="date-picker"]:visible',
            '[class*="datetime"]:visible',
            '[class*="calendar"]:visible',
            '.el-popper:visible',
        ]:
            try:
                c = page.locator(sel).first
                if await c.count() and await c.is_visible():
                    picker = c
                    break
            except Exception:
                continue
        if picker is None:
            sohu_logger.warning(_msg("⚠️", "日期选择器未弹出，请人工在弹窗里选时间"))
            await _save_debug(page, "no_date_picker")
            return

        # 1) 选日期：找含目标日号的 available 单元格
        day_clicked = False
        for sel in [
            'td.available', '[class*="date-table"] td.available',
            '.el-date-table td.available', '.el-date-table td',
        ]:
            try:
                n = await picker.locator(sel).count()
                for i in range(n):
                    cell = picker.locator(sel).nth(i)
                    txt = (await cell.inner_text()).strip().split("\n")[0]
                    if txt == target_day:
                        await cell.click(timeout=3000)
                        day_clicked = True
                        break
            except Exception:
                continue
            if day_clicked:
                break
        if not day_clicked:
            sohu_logger.warning(_msg("⚠️", f"日期选择器中未找到 {target_day} 号"))

        # 2) 设小时/分钟（Element UI 时间面板：input.placeholder 含 时/分）
        try:
            total_inp = await picker.locator("input").count()
            for i in range(min(total_inp, 8)):
                ti = picker.locator("input").nth(i)
                try:
                    ph = (await ti.get_attribute("placeholder") or "").lower()
                    if "时" in ph or "hour" in ph:
                        await ti.fill(target_hour)
                    elif "分" in ph or "minute" in ph:
                        await ti.fill(target_minute)
                    elif "秒" in ph or "second" in ph:
                        await ti.fill("00")
                except Exception:
                    continue
        except Exception:
            pass

        # 3) 点确定
        confirm_clicked = False
        for sel in [
            'button:has-text("确定")',
            '.el-button--text:has-text("确定")',
            '[class*="picker-footer"] button:has-text("确定")',
        ]:
            try:
                c = picker.locator(sel).first
                if await c.count():
                    await c.click(timeout=5000)
                    confirm_clicked = True
                    break
            except Exception:
                continue
        if not confirm_clicked:
            try:
                c = page.locator('button:has-text("确定"):visible').first
                if await c.count():
                    await c.click(timeout=5000)
                    confirm_clicked = True
            except Exception:
                pass

        if confirm_clicked:
            await page.wait_for_timeout(500)
            try:
                actual = (await inp.input_value()).strip()
                sohu_logger.success(_msg("⏰", f"时间已通过日期选择器设置：{actual or f'{target_date_str} {target_time_str}'}"))
            except Exception:
                sohu_logger.success(_msg("⏰", f"时间已通过日期选择器设置：{target_date_str} {target_time_str}"))
        else:
            await _save_debug(page, "date_picker_failed")
            sohu_logger.warning(_msg("⚠️", "日期选择器中未找到确定按钮，请人工在弹窗里选时间"))

    async def _confirm_modal(self, modal, page: Page) -> bool:
        """点击弹窗确认按钮：优先 a.btn-confirm(确认发布)，等待其解除禁用。"""
        # 1) a.btn-confirm / 确认发布（初始 btn-confirm-disabled，勾选声明后启用）
        try:
            btn = modal.locator('a.btn-confirm, a:has-text("确认发布"), a:has-text("确定")').first
            if await btn.count():
                # 等待解除禁用（class 不再含 disabled）
                for _ in range(10):
                    cls = await btn.get_attribute("class") or ""
                    if "disabled" not in cls:
                        break
                    await page.wait_for_timeout(1000)
                await btn.click(timeout=5000)
                sohu_logger.info(_msg("🏃", "已点击「确认发布」"))
                return True
        except Exception:
            pass
        # 2) 兜底 button/span
        for confirm_text in ("确定", "确认", "完成", "提交"):
            try:
                btn = modal.locator(f'button:has-text("{confirm_text}"), span:has-text("{confirm_text}")').first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=5000)
                    sohu_logger.info(_msg("🏃", f"已点击弹窗「{confirm_text}」"))
                    return True
            except Exception:
                continue
        return False

    async def _handle_publish_modal(self, page: Page) -> None:
        """处理发布弹窗（可能两段）：
        第一段：创作内容声明 → 勾选「无需标注」→ 点「确认发布」(a.btn-confirm)。
        第二段（确认后可能出现）：定时发布下拉 / 地区选中国 → 再确认。
        """
        for round_no in range(3):
            modal = None
            for _ in range(3):
                try:
                    modal = page.locator(
                        ".alertBox:visible, [class*='dialog']:visible, [class*='modal']:visible, [class*='popup']:visible"
                    ).first
                    await modal.wait_for(state="visible", timeout=4000)
                    break
                except Exception:
                    await page.wait_for_timeout(1200)
            if modal is None:
                sohu_logger.info(_msg("🏃", "无弹窗，结束弹窗处理"))
                return

            try:
                body = await modal.inner_text()
            except Exception:
                body = ""

            # 1) 创作者声明：勾选「无需标注」
            if any(k in body for k in ("声明", "原创", "转载", "标注")):
                if await self._pick_radio(modal, "无需标注"):
                    sohu_logger.success(_msg("✅", "已勾选「无需标注」"))
                await page.wait_for_timeout(800)

            # 2) 定时发布下拉 / 地区
            if "定时发布" in body or "立即发布" in body or "发布" in body:
                await self._set_publish_time(modal, page)
            if "地区" in body:
                await self._select_region(modal, page)

            # 3) 确认本弹窗
            if await self._confirm_modal(modal, page):
                await page.wait_for_timeout(2000)
                # 确认后可能有第二段弹窗（定时/地区），继续下一轮
                continue

            sohu_logger.warning(_msg("⚠️", f"弹窗（第{round_no + 1}段）未找到确认按钮，按 Esc 关闭"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(1000)
        await _save_debug(page, "after_publish_modal")

    # ---- 定时发布（JS 驱动版，适配搜狐真实 DOM）----
    # 结构（图文/视频共用 .form-item-issue）：
    #   .select-issue 下拉：审核通过后立即发布 / 指定时间发布
    #   .select-date 下拉：li.select-item，文本格式 "2026-8-27"（无前导零）
    #   .select-time 下拉：.time-pan 内两列 .time-item-wrap（时/分），
    #       每列只渲染当前值±1，靠 .arrow-up/.arrow-down 翻页；.time-pan-foot .confirm 确定（div 非 button）
    _SOHU_JS_CLICK = "(el) => { if(el) el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true})); }"

    async def _js_click(self, page: Page, js_expr: str) -> None:
        # js_expr 可能是裸语句（无箭头）或已是完整箭头函数，避免双重包裹导致不执行
        s = js_expr.strip()
        if not s.startswith("() =>"):
            s = "() => { %s }" % s
        try:
            await page.evaluate(s)
        except Exception:
            pass

    async def _set_top_schedule(self, page: Page) -> None:
        """主表单顶部「定时发布」：切「指定时间发布」→ 选日期 → 箭头翻页选时/分 → 确定（JS 驱动）。"""
        if not self.publish_date or isinstance(self.publish_date, int):
            return
        try:
            # 0) 图文页先展开「更多选项」（视频页无此按钮，querySelector 返回 null 自动跳过）
            await self._js_click(page, "document.querySelector('button.form-more-button')?.click()")
            await page.wait_for_timeout(1200)

            # 1) 切「指定时间发布」：直接 dispatch click li（dropdown 常驻 DOM，先点 input 反而破坏 Vue 状态）
            await self._js_click(page,
                "const lis=[...document.querySelectorAll('.form-item-issue .select-issue li.select-item')]; "
                "const t=lis.find(l=>l.textContent.includes('指定时间发布')); if(t) t.dispatchEvent(new MouseEvent('click',{bubbles:true}));")
            await page.wait_for_timeout(800)

            # 2) 日期下拉：视频页带前导零(2026-08-28)、图文页无前导零(2026-8-28)，两种格式都试
            target_date = self.publish_date.strftime("%Y-%m-%d")
            target_date_alt = f"{self.publish_date.year}-{self.publish_date.month}-{self.publish_date.day}"
            target_hour = f"{self.publish_date.hour:02d}"
            target_minute = f"{self.publish_date.minute:02d}"
            date_hit = await page.evaluate(
                "() => { const lis=[...document.querySelectorAll('.form-item-issue .select-date li.select-item')]; "
                "const t=lis.find(l=>{const v=l.textContent.trim(); return v==='%s' || v==='%s';}); "
                "if(t){ t.dispatchEvent(new MouseEvent('click',{bubbles:true})); return t.textContent.trim(); } "
                "return 'NO:' + lis.map(l=>l.textContent.trim()).join(','); }" % (target_date, target_date_alt)
            )
            if isinstance(date_hit, str) and date_hit.startswith("NO:"):
                sohu_logger.warning(_msg("⚠️", f"日期列表里没有 {target_date}：{date_hit}"))
            else:
                sohu_logger.info(_msg("📅", f"日期已选 {date_hit or target_date}"))
            await page.wait_for_timeout(600)

            # 3) 时间：找"时间"label后的 .select 容器（视频页 select-time-wrapper / 图文页 select-time），点 input 打开浮层
            await self._js_click(page,
                "() => { const titles=[...document.querySelectorAll('.form-item-issue .select-title')]; "
                "const tt=titles.find(x=>x.textContent.trim()==='时间'); if(!tt) return; "
                "let n=tt.nextElementSibling; "
                "while(n && !(n.classList && n.classList.contains('select'))) n=n.nextElementSibling; "
                "const inp=n && n.querySelector('input'); "
                "if(inp) inp.dispatchEvent(new MouseEvent('click',{bubbles:true})); }")
            await page.wait_for_timeout(800)
            h_ok = await self._scroll_time_wrap(page, 0, target_hour)
            m_ok = await self._scroll_time_wrap(page, 1, target_minute)
            await page.wait_for_timeout(400)
            await self._js_click(page,
                "const c=document.querySelector('.form-item-issue .time-pan-foot .confirm'); "
                "if(c) c.dispatchEvent(new MouseEvent('click',{bubbles:true}));")
            await page.wait_for_timeout(600)
            sohu_logger.success(_msg("⏰", f"定时发布已设：{target_date} {target_hour}:{target_minute}（时={h_ok} 分={m_ok}）"))
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"设置定时发布失败: {exc}"))

    async def _scroll_time_wrap(self, page: Page, idx: int, target: str) -> bool:
        """在时间面板第 idx 列（0=小时, 1=分钟）点箭头翻页直到 .item.on == target。"""
        state_js = (
            "() => { const w=document.querySelectorAll('.form-item-issue .time-item-wrap')[%d]; "
            "if(!w) return null; "
            "const on=w.querySelector('.item.on'); "
            "const up=w.querySelector('.arrow-up'), down=w.querySelector('.arrow-down'); "
            "const cur=on?on.textContent.trim():''; "
            "return cur+'|'+(up?up.classList.contains('disable'):'x')+'|'+(down?down.classList.contains('disable'):'x'); }" % idx
        )
        click_js = (
            "() => { const w=document.querySelectorAll('.form-item-issue .time-item-wrap')[%d]; "
            "const a=w?w.querySelector('.arrow-%s'):null; if(a) a.dispatchEvent(new MouseEvent('click',{bubbles:true})); }"
        )
        for _ in range(70):  # 分钟 59→00 最多 59 次，给足余量
            raw = await page.evaluate(state_js)
            if raw is None:
                return False
            cur, up_dis, down_dis = raw.split("|")
            if cur == target:
                return True
            if not cur:
                return False
            c, tg = int(cur), int(target)
            if tg > c and down_dis != "true":
                await self._js_click(page, click_js % (idx, "down"))
            elif tg < c and up_dis != "true":
                await self._js_click(page, click_js % (idx, "up"))
            else:
                return False
            await page.wait_for_timeout(300)
        return False

    async def _set_form_declaration(self, page: Page) -> None:
        """主表单里的「创作内容声明」：勾选「无需标注」。"""
        item = page.locator('.radio_item:has-text("无需标注")').first
        if not await item.count():
            return
        try:
            await item.scroll_into_view_if_needed()
            await item.click(timeout=4000)
            sohu_logger.success(_msg("✅", "已勾选「无需标注」"))
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"勾选无需标注失败: {exc}"))

    async def _set_form_content_time(self, page: Page) -> None:
        """主表单里的「内容发生时间」：点击 readonly input 弹日期选择器，选当前日期。"""
        from datetime import datetime
        target_day = str(datetime.now().day)
        try:
            inp = page.locator('input[placeholder*="发生时间"]').first
            if not await inp.count():
                inp = page.locator('input[placeholder*="发布内容发生时间"]').first
            if not await inp.count():
                inp = page.locator('.form-item:has-text("时间") input').last
            if not await inp.count():
                sohu_logger.warning(_msg("⚠️", "未找到内容发生时间输入框")); return
            await inp.scroll_into_view_if_needed()
            await inp.click(timeout=5000)
            await page.wait_for_timeout(1500)

            # 找日期选择器 popup
            picker = None
            for sel in ['.el-date-picker:visible', '.el-picker-panel:visible', '[class*="date-picker"]:visible', '[class*="datetime"]:visible']:
                c = page.locator(sel).first
                if await c.count() and await c.is_visible():
                    picker = c; break
            if picker is None:
                await _save_debug(page, "no_content_picker"); return

            # 选今日
            day_clicked = False
            for sel in ['td.available', '.el-date-table td.available']:
                n = await picker.locator(sel).count()
                for i in range(n):
                    cell = picker.locator(sel).nth(i)
                    txt = (await cell.inner_text()).strip().split('\n')[0]
                    if txt == target_day:
                        await cell.click(timeout=3000); day_clicked = True; break
                if day_clicked: break

            # 点确定
            for sel in ['button:has-text("确定")', '.el-button--text:has-text("确定")']:
                c = picker.locator(sel).first
                if await c.count():
                    await c.click(timeout=5000); break
            if day_clicked:
                sohu_logger.info(_msg("⏰", "内容发生时间已设为今日"))
            else:
                sohu_logger.warning(_msg("⚠️", "日期选择器中未找到今日"))
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"设置内容发生时间失败: {exc}"))

    async def _set_form_address(self, page: Page) -> None:
        """主表单里的「地址」：选中国（原生 select / 自定义下拉都兼容）。"""
        try:
            sel_el = page.locator('input[placeholder*="发生地点"]').first
            if not await sel_el.count():
                sel_el = page.locator('input[placeholder*="发布内容发生地点"]').first
            if not await sel_el.count():
                sel_el = page.locator('.form-item:has-text("地址") input, select').first
            if not await sel_el.count():
                sohu_logger.warning(_msg("⚠️", "未找到地址select")); return
            try:
                tag = (await sel_el.evaluate("e => e.tagName")) or ""
            except Exception:
                tag = ""
            if tag == "SELECT":
                try:
                    await sel_el.select_option(label="中国")
                except Exception:
                    await sel_el.select_option(value="中国")
                sohu_logger.success(_msg("✅", "地址已选择：中国（select）"))
                return
            await sel_el.scroll_into_view_if_needed()
            await sel_el.click(timeout=5000)
            await page.wait_for_timeout(1200)
            for sel in [
                'li:has-text("中国"):not(:has-text("香港"))',
                'span:text-is("中国")',
                'div:text-is("中国")',
                '[class*="option"]:has-text("中国")',
            ]:
                c = page.locator(sel).first
                if await c.count() and await c.is_visible():
                    await c.click(timeout=4000)
                    sohu_logger.success(_msg("✅", "地址已选择：中国"))
                    return
            sohu_logger.warning(_msg("⚠️", "地址下拉里未找到「中国」选项，请人工在弹窗里选"))
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"设置地址失败: {exc}"))

    async def _prefill_publish_form(self, page: Page) -> None:
        """预填主表单：定时发布(可选) + 无需标注 + 内容发生时间(今日) + 地址(中国)。

        全填好后再点发布 → 不会再弹"声明"弹窗。
        """
        sohu_logger.info(_msg("🏃", "预填主表单（定时/声明/时间/地址）"))
        await self._set_top_schedule(page)
        await self._set_form_declaration(page)
        await self._set_form_content_time(page)
        await self._set_form_address(page)

    async def _submit_publish(self, page: Page) -> None:
        # 1. 先在主表单预填：定时发布 / 无需标注 / 时间 / 地址
        #    全填好后再点发布就不会弹"声明"弹窗（弹窗只在缺字段时出现）
        await self._prefill_publish_form(page)

        # 2. 点发布按钮
        publish_btn = page.locator('span.button-red:has-text("发布"), span:text-is("发布")').first
        if not await publish_btn.count():
            publish_btn = page.locator('button:text-is("发布")').first
        if not await publish_btn.count():
            publish_btn = page.locator('button:has-text("发布"), [class*="publish"] button, span:text-is("发布")').first
        if not await publish_btn.count():
            raise RuntimeError("未找到发布按钮")
        await publish_btn.wait_for(state="visible", timeout=15000)
        await publish_btn.scroll_into_view_if_needed()
        await publish_btn.click(force=True)
        sohu_logger.info(_msg("🏃", "已点击发布按钮"))
        await page.wait_for_timeout(2000)

        # 3. 兜底：万一有弹窗（缺字段时出现的声明弹窗）
        await self._handle_publish_modal(page)

        start = time.monotonic()
        while time.monotonic() - start < 30:
            try:
                toast = page.locator('[class*="message"]:has-text("成功"), [class*="toast"]:has-text("成功"), text="发布成功", text="提交成功"').first
                if await toast.count() and await toast.is_visible():
                    sohu_logger.success(_msg("🥳", "视频发布成功"))
                    return
                # URL 跳到内容管理页 = 成功
                cur_url = page.url.lower()
                if "video/list" in cur_url or "/content" in cur_url or "/list" in cur_url:
                    sohu_logger.success(_msg("🥳", "视频发布成功（已跳到内容列表）"))
                    return
            except Exception:
                # 页面可能已关闭/跳转，视为流程结束
                try:
                    cur_url = page.url.lower()
                    if "video/list" in cur_url or "/content" in cur_url:
                        sohu_logger.success(_msg("🥳", "视频发布成功（页面跳转）"))
                        return
                except Exception:
                    pass
                sohu_logger.info(_msg("🏃", "页面状态变化，结束成功检测"))
                return
            await page.wait_for_timeout(1000)
        sohu_logger.warning(_msg("⚠️", "发布后 30s 内未捕获成功提示，请人工确认；已保存快照"))

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

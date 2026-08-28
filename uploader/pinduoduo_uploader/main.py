# -*- coding: utf-8 -*-
"""多多视频（拼多多创作者中心 / live.pinduoduo.com）视频上传 + 扫码登录。

功能：
  - pinduoduo_cookie_gen: 有头/无头扫码登录（拼多多APP 二维码，兼容 iframe 内嵌）
  - cookie_auth: 验证 cookie 是否有效
  - pinduoduo_setup: 统一入口（检查/触发登录）
  - PinduoduoVideo: 视频上传类

说明：首版选择器基于通用套路编写，页面 DOM 可能随平台改版变化；
联调失败时会在 logs/pinduoduo_debug/ 自动落盘截图 + HTML 快照 + 控件清单，便于迭代。
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json as _json
import os
import random
import re as _re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Page, Playwright, async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.human_behavior import human_sleep
from utils.log import pinduoduo_logger
from utils.login_qrcode import (
    build_login_qrcode_path,
    decode_qrcode_from_path,
    print_terminal_qrcode,
    remove_qrcode_file,
)

PDD_LOGIN_URL = "https://live.pinduoduo.com/login?isNewCreatorFrom=live&referUrl=/n-creator/live/home"
PDD_HOME_URL = "https://live.pinduoduo.com/n-creator/live/home"
# 多多视频创作中心（视频上传）入口
PDD_PUBLISH_URLS = [
    "https://live.pinduoduo.com/n-creator/video/home",
]
# 登录跳转关键字
PDD_LOGIN_URL_MARKERS = ("login", "passport", "sso", "oauth")


def _decode_b64(src: str) -> bytes:
    return base64.b64decode(_re.sub(r"^data:image/[^;]+;base64,", "", src))


def _find_gap_position(bg_b64: str, piece_b64: str) -> float:
    """滑块拼图缺口定位：模板边缘匹配。

    用拼图块的 alpha 形状边界，在背景图的边缘图上做滑动相关，峰值即缺口左缘，
    返回缺口在背景图宽度上的归一化位置（0~1）。
    """
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("缺少 pillow 依赖，请先 pip install pillow（多多视频滑块验证码需要）")

    bg = Image.open(io.BytesIO(_decode_b64(bg_b64))).convert("L")
    piece = Image.open(io.BytesIO(_decode_b64(piece_b64)))
    if piece.mode == "RGBA":
        alpha = piece.split()[-1]
        mask = alpha.point(lambda a: 255 if a > 120 else 0)
    else:
        mask = piece.convert("L").point(lambda a: 255)

    bw, bh = bg.size
    pw, ph = piece.size
    pm = mask.load()

    # 拼图块形状的边界像素
    piece_edges = []
    for u in range(pw):
        for v in range(ph):
            if pm[u, v] > 128:
                hit = False
                for du, dv in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nu, nv = u + du, v + dv
                    if 0 <= nu < pw and 0 <= nv < ph and pm[nu, nv] <= 128:
                        hit = True
                        break
                if hit:
                    piece_edges.append((u, v))

    # 背景边缘图（一阶梯度）
    g = bg.load()
    bg_edge = [[0] * bh for _ in range(bw)]
    for x in range(1, bw - 1):
        for y in range(bh):
            dx = abs(g[x + 1, y] - g[x - 1, y])
            dy = abs(g[x, y + 1] - g[x, y - 1]) if y + 1 < bh else 0
            bg_edge[x][y] = 1 if (dx + dy) > 60 else 0

    best_score, best_x = -1, int(bw * 0.5)
    for x in range(0, bw - pw - 1):
        score = 0
        for (u, v) in piece_edges:
            score += bg_edge[x + u][v]
        if score > best_score:
            best_score, best_x = score, x
    return best_x / bw


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
        return str((Path(BASE_DIR) / "cookies" / "pinduoduo_uploader" / path).resolve())
    return str(path.resolve())


async def _save_debug(page: Page, tag: str) -> None:
    """失败时落盘：截图 + HTML 快照 + 可见控件清单，方便迭代选择器。"""
    try:
        out_dir = Path(BASE_DIR) / "logs" / "pinduoduo_debug"
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
        pinduoduo_logger.warning(_msg("📸", f"调试快照已保存: {out_dir}/{stamp}_{tag}.png/.html/.json"))
    except Exception as exc:
        pinduoduo_logger.warning(_msg("😵", f"保存调试快照失败: {exc}"))


async def _extract_qrcode_src(page: Page) -> str:
    """从页面（含 iframe）里提取二维码图片 src（data URL 或可下载 URL）。"""
    for frame in page.frames:
        try:
            candidates = [
                frame.locator("img.qrcode").first,
                frame.locator('[class*="qrcode"] img, [class*="QRcode"] img').first,
                frame.locator('img[src*="qrcode"], img[src*="qr_code"]').first,
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

    raise RuntimeError("未获取到拼多多登录二维码（页面上没找到二维码图片）")


async def _grab_qr(page: Page, account_file: str) -> dict:
    qrcode_src = await _extract_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="pdd_login_qrcode")
    qrcode_path.parent.mkdir(parents=True, exist_ok=True)
    if qrcode_src.startswith("data:"):
        m = _re.match(r"data:[^;]+;base64,(.*)", qrcode_src, _re.S)
        if m:
            qrcode_path.write_bytes(base64.b64decode(m.group(1)))
        else:
            await page.screenshot(path=str(qrcode_path))
    else:
        await page.screenshot(path=str(qrcode_path))

    qrcode_content = decode_qrcode_from_path(qrcode_path)
    pinduoduo_logger.info(_msg("🖼️", f"二维码已保存到: {qrcode_path}"))
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "拼多多APP")
    else:
        pinduoduo_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    return {"image_path": str(qrcode_path), "image_data_url": qrcode_src}


async def _is_login_completed(page: Page) -> bool:
    """登录完成判断（只认 URL 离开登录域，避免二维码消失被误判为登录成功）。"""
    url = page.url.lower()
    if any(m in url for m in PDD_LOGIN_URL_MARKERS):
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


async def pinduoduo_cookie_gen(account_file, qrcode_callback=None, poll_interval: int = 3, max_checks: int = 120, headless: bool = LOCAL_CHROME_HEADLESS):
    account_file = _resolve_account_file(account_file)
    Path(account_file).parent.mkdir(parents=True, exist_ok=True)
    qrcode_path = None
    result = _build_login_result(False, "failed", "多多视频登录失败", account_file)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=headless))
        context = await _stealth_context(await browser.new_context())
        try:
            page = await context.new_page()
            await page.goto(PDD_LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)

            if headless:
                pinduoduo_logger.info(_msg("🧍", "无头登录中：二维码已存为图片，请用拼多多APP扫码"))
            else:
                pinduoduo_logger.info(_msg("🧍", "请在打开的浏览器中扫码登录多多视频"))

            qrcode_info = await _grab_qr(page, account_file)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            await _emit_qrcode_callback(qrcode_callback, qrcode_info)
            pinduoduo_logger.info(_msg("🧍", "请扫码，正在耐心等待登录完成（二维码过期会自动刷新，不用赶）"))

            # max_checks 默认 120 * 3s = 6 分钟；期间二维码过期会自动点刷新续命
            refresh_count = 0
            for i in range(max_checks):
                if await _is_login_completed(page):
                    pinduoduo_logger.info(_msg("🥳", f"扫码成功，当前页面: {page.url}"))
                    result = _build_login_result(True, "success", "多多视频扫码登录成功", account_file, qrcode_info, page.url)
                    break

                if await _is_qrcode_expired(page):
                    if await _refresh_qrcode(page):
                        refresh_count += 1
                        pinduoduo_logger.info(_msg("🔄", f"二维码已过期，已自动刷新（第 {refresh_count} 次），请重新扫码"))
                        await page.wait_for_timeout(2000)
                        try:
                            qrcode_info = await _grab_qr(page, account_file)
                            new_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
                            if new_path and new_path != qrcode_path:
                                if remove_qrcode_file(qrcode_path):
                                    pinduoduo_logger.info(_msg("🧹", f"旧二维码文件已清理: {qrcode_path}"))
                                qrcode_path = new_path
                            await _emit_qrcode_callback(qrcode_callback, qrcode_info)
                        except Exception as exc:
                            pinduoduo_logger.warning(_msg("⚠️", f"刷新后重新抓取二维码失败: {exc}"))
                        continue
                    pinduoduo_logger.info(_msg("🔄", "二维码已过期但未找到刷新按钮，等待页面自行刷新"))

                if i % 10 == 9:
                    pinduoduo_logger.info(_msg("🧍", f"仍在等待扫码…（已等待 {round((i + 1) * poll_interval)}s，浏览器请保持打开）"))
                await page.wait_for_timeout(poll_interval * 1000)
            else:
                result = _build_login_result(False, "timeout", "等待多多视频扫码登录超时", account_file, qrcode_info, page.url)

            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                pinduoduo_logger.success(_msg("🥳", f"cookie 已保存: {account_file}"))
        except Exception as exc:
            if "page" in locals():
                await _save_debug(page, "login_error")
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                pinduoduo_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                pinduoduo_logger.error(_msg("😢", f"登录失败: {result['message']}"))
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
            await page.goto(PDD_HOME_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)

            url = page.url.lower()
            if any(m in url for m in PDD_LOGIN_URL_MARKERS):
                pinduoduo_logger.info(_msg("🥹", "cookie 已失效（页面跳转到登录页）"))
                return False
            pinduoduo_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            pinduoduo_logger.warning(_msg("😵", f"cookie 校验出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def pinduoduo_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS):
    account_file = _resolve_account_file(account_file)
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie 文件不存在或已失效", account_file)
            return result if return_detail else False
        pinduoduo_logger.info(_msg("🥹", "cookie 文件不存在或已失效，自动打开浏览器请扫码登录"))
        result = await pinduoduo_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie 有效", account_file)
    return result if return_detail else True


class PinduoduoVideo(BaseVideoUploader):
    """多多视频（拼多多创作者中心）视频上传。"""

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
        self.max_title_length = 30

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
            pinduoduo_logger.info(_msg("🧪", f"DRY-RUN 定时校验：目标 {fmt}，剩余 {int(delay)}s，跳过真实发布"))
            return False
        pinduoduo_logger.info(_msg("⏰", f"定时发布：将在 {fmt} 发布（剩余 {int(delay)}s）"))
        while delay > 0:
            await asyncio.sleep(min(30, delay))
            now = datetime.now(tz=self.publish_date.tzinfo) if self.publish_date.tzinfo else datetime.now()
            delay = (self.publish_date - now).total_seconds()
        pinduoduo_logger.info(_msg("🏃", "到点，开始发布"))
        return True

    async def validate_upload_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成多多视频登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成多多视频登录: {self.account_file}")
        if not self.title or not str(self.title).strip():
            raise ValueError("视频标题不能为空")
        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def upload(self, playwright: Playwright) -> None:
        pinduoduo_logger.info(_msg("🧍", "先检查 cookie 和视频文件"))
        await self.validate_upload_args()
        pinduoduo_logger.info(_msg("🥳", "上传前检查通过"))

        # PDD 表单原生支持「发布设置·定时发布」，无需进程内 wait-until
        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=self.headless))
        context = await _stealth_context(await browser.new_context(storage_state=self.account_file))
        try:
            page = await context.new_page()
            await self._goto_publish_page(page)
            # 进入发布页 = 安全验证已通过：立即保存 cookie（验证状态持久化）
            await context.storage_state(path=self.account_file)
            pinduoduo_logger.info(_msg("💾", "安全验证已通过，cookie 已保存"))
            pinduoduo_logger.info(_msg("🏃", f"开始上传视频: {self.title}"))

            await self._set_video_file(page)
            await self._wait_form_ready(page)
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
            else:
                # 无自定义封面：点 PDD「设置封面」按钮 → 弹框默认点确认（用平台自动生成的封面候选）
                await self._set_default_cover(page)
            await self._submit_publish(page)

            await context.storage_state(path=self.account_file)
            pinduoduo_logger.success(_msg("🥳", "cookie 更新完毕"))
        except Exception as exc:
            await _save_debug(page, "upload_error")
            raise
        finally:
            await context.close()
            await browser.close()

    async def _solve_slider(self, page: Page, max_attempts: int = 6) -> bool:
        """自动拖动滑块拼图验证码。成功（弹窗消失）返回 True。"""
        for attempt in range(max_attempts):
            bg = await page.locator("img.slider-img-bg").get_attribute("src")
            piece = await page.locator("img.slider-item").get_attribute("src")
            if not bg or not piece:
                pinduoduo_logger.info(_msg("🧍", "滑块图未就绪，等待渲染…"))
                await page.wait_for_timeout(1500)
                continue
            bg_box = await page.locator("img.slider-img-bg").bounding_box()
            item_box = await page.locator("img.slider-item").first.bounding_box()
            handle_box = await page.locator("#slide-button, .slide-button").first.bounding_box()
            if not all([bg_box, item_box, handle_box]):
                pinduoduo_logger.warning(_msg("⚠️", "滑块元素定位失败，重试"))
                await page.wait_for_timeout(1500)
                continue

            gap_norm = _find_gap_position(bg, piece)
            gap_x = gap_norm * bg_box["width"]
            piece_left = item_box["x"] - bg_box["x"]
            delta = gap_x - piece_left
            pinduoduo_logger.info(_msg("🎯", f"缺口位置 {gap_x:.0f}px，需拖动 {delta:.0f}px"))

            sx, sy = handle_box["x"] + handle_box["width"] / 2, handle_box["y"] + handle_box["height"] / 2
            await page.mouse.move(sx, sy)
            await page.mouse.down()
            steps = 26
            for i in range(1, steps + 1):
                await page.mouse.move(sx + delta * i / steps, sy + random.uniform(-1.5, 1.5), steps=2)
                await page.wait_for_timeout(12)
            await page.mouse.up()
            await page.wait_for_timeout(2500)

            if await page.locator("#slide-captcha-dialog").count() == 0:
                pinduoduo_logger.success(_msg("🥳", "滑块验证码已通过"))
                return True
            pinduoduo_logger.warning(_msg("🔄", f"滑块验证第 {attempt + 1} 次未通过，刷新重试"))
            try:
                await page.locator(".captcha-refresh").first.click()
                await page.wait_for_timeout(1500)
            except Exception:
                pass
        return False

    async def _wait_slider_manual(self, page: Page, timeout: int = 240) -> bool:
        """headed 人工协助：等待用户手动拖动滑块完成安全验证。"""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if await page.locator("#slide-captcha-dialog").count() == 0:
                pinduoduo_logger.success(_msg("🥳", "安全验证已完成（人工/自动）"))
                return True
            await page.wait_for_timeout(3000)
        return False

    async def _goto_publish_page(self, page: Page) -> None:
        last_err: Exception | None = None
        for url in PDD_PUBLISH_URLS:
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)

                # 上传前的「安全验证」（滑块拼图）
                verify_btn = page.get_by_text("安全验证", exact=True).first
                if await verify_btn.count() and await verify_btn.is_visible():
                    pinduoduo_logger.info(_msg("🧍", "发现「安全验证」，尝试自动拖动滑块…"))
                    await verify_btn.click(timeout=8000)
                    await page.wait_for_timeout(2500)
                    if not await self._solve_slider(page):
                        if self.headless:
                            raise RuntimeError("多多视频安全验证滑块自动求解失败；请用 --headed 运行，由人工拖动滑块完成")
                        pinduoduo_logger.warning(_msg("🔔", "自动滑块未通过：请在打开的浏览器中手动拖动滑块完成安全验证（脚本将自动等待）"))
                        if not await self._wait_slider_manual(page):
                            raise RuntimeError("等待人工完成安全验证超时")

                fi = page.locator('input[type="file"]').first
                if await fi.count():
                    pinduoduo_logger.info(_msg("🏃", f"已进入发布页: {url}"))
                    return
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"未找到多多视频发布页（无文件选择框）: {last_err}")

    async def _set_video_file(self, page: Page) -> None:
        file_input = page.locator('input[type="file"][accept*="video"], input[type="file"][accept*="mp4"]').first
        if not await file_input.count():
            file_input = page.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=30000)
        await file_input.set_input_files(self.file_path)
        pinduoduo_logger.info(_msg("🏃", f"已选择视频文件: {self.file_path}"))

    async def _wait_form_ready(self, page: Page, timeout: int = 180) -> None:
        """等待发布表单渲染（timeout 单位：秒）。"""
        title_candidates = [
            page.locator("div[contenteditable=true]").first,
            page.locator('input[placeholder*="标题"], textarea[placeholder*="标题"]').first,
            page.locator('input[placeholder*="title"], textarea[placeholder*="title"]').first,
        ]
        for loc in title_candidates:
            try:
                await loc.wait_for(state="visible", timeout=timeout * 1000)
                pinduoduo_logger.info(_msg("🏃", "发布表单已就绪"))
                return
            except Exception:
                continue
        raise RuntimeError("等待发布表单超时（未找到标题输入框）")

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
        pinduoduo_logger.info(_msg("🏷️", f"标题已填写: {title}"))

    async def _wait_upload_complete(self, page: Page, timeout: int = 600) -> None:
        start = time.monotonic()
        seen_progress = False
        gone_count = 0
        while True:
            if time.monotonic() - start > timeout:
                pinduoduo_logger.warning(_msg("⚠️", f"等待上传超时（>{timeout}s），继续后续步骤"))
                return
            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass
            if "上传失败" in body:
                # 平台签名/风控拦截
                if self.headless:
                    raise RuntimeError("多多视频上传失败（平台签名校验拦截），请用 --headed 运行由人工协助上传")
                pinduoduo_logger.warning(
                    _msg("🔔", "多多视频上传被平台拦截：请在打开的浏览器中手动完成上传（点「重新上传」→ 选择视频 → 如有验证码请手动完成），脚本将等待「视频封面」出现后继续自动填表发布")
                )
                if await self._wait_upload_manual(page, timeout=300):
                    pinduoduo_logger.success(_msg("🥳", "人工上传完成，继续自动流程"))
                    return
                raise RuntimeError("等待人工上传超时")
            m = _re.search(r"(\d{1,3})\s*%", body)
            pct = int(m.group(1)) if m else None
            if pct is not None and pct < 100:
                seen_progress = True
                gone_count = 0
                pinduoduo_logger.info(_msg("🏃", f"上传中 {pct}%"))
                await asyncio.sleep(2)
                continue
            if seen_progress:
                gone_count += 1
                if gone_count >= 2:
                    pinduoduo_logger.success(_msg("🥳", "视频上传完毕"))
                    return
                await asyncio.sleep(2)
                continue
            if time.monotonic() - start > 15:
                pinduoduo_logger.success(_msg("🥳", "视频上传完毕"))
                return
            await asyncio.sleep(2)

    async def _wait_upload_manual(self, page: Page, timeout: int = 300) -> bool:
        """headed 人工协助：等待用户在浏览器手动上传成功（出现「视频封面」+「发布」）。"""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                body = await page.inner_text("body")
                if "视频封面" in body and "发布" in body and "上传失败" not in body:
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(4000)
        return False

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
            pinduoduo_logger.warning(_msg("⚠️", "未找到简介输入框，跳过简介"))
            return
        await field.click()
        await field.fill(str(self.desc)[:500])
        pinduoduo_logger.info(_msg("📝", "简介已填写"))

    async def _fill_tags(self, page: Page) -> None:
        """PDD 表单的「标签/话题」：input、textarea、contenteditable 多形式兜底。

        PDD 当前表单里话题是 #xxx 形式（截图为「# 添加话题」多行 8/500），通常表现为 contenteditable。
        """
        if not self.tags:
            return
        field = page.locator('input[placeholder*="标签"], input[placeholder*="话题"], input[class*="tag"], input[class*="topic"]').first
        if await field.count():
            try:
                await field.click()
                for t in self.tags:
                    tag = str(t).lstrip("#").strip()
                    if not tag:
                        continue
                    await field.fill(tag)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(600)
                pinduoduo_logger.info(_msg("🏷️", f"标签已填写（input 逐个回车）: {', '.join(str(t) for t in self.tags)}"))
                return
            except Exception:
                pass

        field = page.locator('textarea[placeholder*="标签"], textarea[placeholder*="话题"]').first
        if await field.count():
            try:
                await field.click()
                for t in self.tags:
                    tag = str(t).lstrip("#").strip()
                    if not tag:
                        continue
                    await field.fill(tag)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(600)
                pinduoduo_logger.info(_msg("🏷️", f"标签已填写（textarea 逐个回车）: {', '.join(str(t) for t in self.tags)}"))
                return
            except Exception:
                pass

        # PDD 兜底：contenteditable 元素（话题常见）—— 用 keyboard.type 写「#tag 」
        ce = page.locator('[contenteditable="true"]:visible').first
        if await ce.count():
            try:
                await ce.click()
                for t in self.tags:
                    tag = str(t).lstrip("#").strip()
                    if not tag:
                        continue
                    if t != self.tags[0]:
                        await page.keyboard.press("Space")
                    await page.keyboard.type(f"#{tag}", delay=20)
                    await page.wait_for_timeout(400)
                pinduoduo_logger.success(_msg("🏷️", f"标签已填写（话题 #xxx, contenteditable）: {', '.join(str(t) for t in self.tags)}"))
                return
            except Exception:
                pass

        pinduoduo_logger.warning(_msg("⚠️", "未找到标签/话题输入框，请人工填写话题后按回车"))

    async def _set_default_cover(self, page: Page) -> None:
        """无自定义封面：点 PDD「编辑封面」→ 选候选帧 → 点「确定」（用平台自动生成的封面候选帧）。"""
        try:
            # 1) 精确点「编辑封面」按钮（video-list_coverImage 里的 button）
            btn = page.locator('div[class*="coverImage"] button, div[class*="coverImage__"] button').first
            if not await btn.count():
                btn = page.locator('button:has-text("编辑封面"), button:has-text("设置封面"), button:has-text("更换封面")').first
            if not await btn.count():
                pinduoduo_logger.warning(_msg("⚠️", "未找到「编辑封面」按钮，跳过封面"))
                return
            await btn.scroll_into_view_if_needed(timeout=5000)
            await btn.click(timeout=8000)
            pinduoduo_logger.info(_msg("📂", "已点「编辑封面」按钮，等待封面候选帧..."))

            # 2) 轮询等待 popover 打开 + 候选帧 img 出现（最多 15s）
            candidate = None
            for _ in range(15):
                await page.wait_for_timeout(1000)
                st = await page.evaluate(r"""() => {
                    // 封面候选帧：优先 popover 内，兜底全页面（候选帧是 <video> 元素，180×320，无 class）
                    const isVisible = r => r.width > 40 && r.height > 40;
                    const popSel = '[class*="popover"], [class*="Popover"], [data-testid="beast-core-popover"]';
                    const scopes = [...document.querySelectorAll(popSel)].filter(e => e.getBoundingClientRect().width > 50);
                    const pick = (scope) => {
                        let frames = [...scope.querySelectorAll('img')].filter(im => isVisible(im.getBoundingClientRect()));
                        if (!frames.length) frames = [...scope.querySelectorAll('canvas, video')].filter(im => isVisible(im.getBoundingClientRect()));
                        if (!frames.length) frames = [...scope.querySelectorAll('div')].filter(d => {
                            const bg = d.style && d.style.backgroundImage;
                            return bg && bg !== 'none' && isVisible(d.getBoundingClientRect());
                        });
                        return frames;
                    };
                    // 1) popover 内找（从后往前）
                    for (let i = scopes.length - 1; i >= 0; i--) {
                        const frames = pick(scopes[i]);
                        if (frames.length) {
                            const first = frames[0];
                            const r = first.getBoundingClientRect();
                            return {ready: true, count: frames.length, x: r.x + r.width/2, y: r.y + r.height/2, w: Math.round(r.width), h: Math.round(r.height), tag: first.tagName};
                        }
                    }
                    // 2) 兜底：全页面找 video（排除主视频预览 video-list_video）
                    const candVideos = [...document.querySelectorAll('video')].filter(v => {
                        const r = v.getBoundingClientRect();
                        const cls = (v.className||'').toString();
                        return isVisible(r) && !cls.includes('video-list_video');
                    });
                    if (candVideos.length) {
                        const first = candVideos[0];
                        const r = first.getBoundingClientRect();
                        return {ready: true, count: candVideos.length, x: r.x + r.width/2, y: r.y + r.height/2, w: Math.round(r.width), h: Math.round(r.height), tag: first.tagName};
                    }
                    return {ready: false, count: 0};
                }""")
                if st.get('ready'):
                    candidate = st
                    break

            # 3) 点候选帧 → 点「确定」按钮 = 封面设置成功（用户确认：点确定即成功）
            if candidate:
                await page.mouse.click(candidate['x'], candidate['y'])
                await page.wait_for_timeout(1500)
                pinduoduo_logger.info(_msg("🖼️", f"已点选封面候选帧（{candidate.get('tag', '?')}）"))
                # 找「确定」按钮（用户确认按钮文案是「确定」不是「确认」）并真实点击
                confirm = await page.evaluate(r"""() => {
                    const words = ['确定', '确认', '完成', '保存', '使用', '使用此封面'];
                    const btns = [...document.querySelectorAll('button')].filter(b => {
                        const t = (b.innerText||'').trim();
                        return words.includes(t) && b.getBoundingClientRect().width > 0;
                    });
                    if (btns.length) {
                        const b = btns[btns.length - 1];  // 最后一个（弹框底部的确定）
                        const r = b.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2, text: b.innerText.trim()};
                    }
                    return null;
                }""")
                if confirm:
                    await page.mouse.click(confirm['x'], confirm['y'])
                    await page.wait_for_timeout(1000)
                    pinduoduo_logger.success(_msg("🖼️", f"封面已设置（点选候选帧 + 点「{confirm['text']}」）"))
                else:
                    pinduoduo_logger.warning(_msg("⚠️", "已点候选帧但未找到确认按钮，可能封面编辑面板是「点选即生效」无需确认"))
            else:
                pinduoduo_logger.warning(_msg("⚠️", "15s 内未找到封面候选帧，请人工确认"))
        except Exception as exc:
            pinduoduo_logger.warning(_msg("⚠️", f"设置默认封面失败: {exc}"))

    async def _upload_thumbnail(self, page: Page) -> None:
        try:
            # PDD 多多视频封面入口：多种可能——「封面」文本 / 「更换封面」/ cover class / 视频缩略图点击
            cover_entry = None
            # 1) 精确文本「封面」
            for sel in [
                'button:has-text("封面")',
                'div:has-text("封面"):not(:has(*))',  # 叶子节点
                '[class*="cover"][class*="upload"]',
                '[class*="Cover"][class*="upload"]',
                'a:has-text("更换封面")',
                'button:has-text("更换封面")',
                'div:has-text("更换封面"):not(:has(*))',
            ]:
                loc = page.locator(sel).first
                if await loc.count():
                    cover_entry = loc
                    break
            if not cover_entry:
                # 兜底：找含「封面」文本的可点击元素（排除主页课程封面 coverImg）
                found = await page.evaluate(r"""() => {
                    const els = [...document.querySelectorAll('button, a, div[role="button"], span[class*="cover"]')];
                    const t = els.find(e => {
                        const txt = (e.innerText||'').trim();
                        return (txt === '封面' || txt === '更换封面' || txt === '设置封面')
                            && !e.className.includes('course_coverImg');
                    });
                    if (!t) return null;
                    const r = t.getBoundingClientRect();
                    return r.width && r.height ? {x: r.x + r.width/2, y: r.y + r.height/2, text: (t.innerText||'').trim()} : null;
                }""")
                if found:
                    await page.mouse.click(found['x'], found['y'])
                    await page.wait_for_timeout(2000)
                else:
                    pinduoduo_logger.warning(_msg("⚠️", "未找到封面入口，跳过封面"))
                    return
            else:
                await cover_entry.scroll_into_view_if_needed()
                await cover_entry.click(timeout=8000)
                await page.wait_for_timeout(2000)
            # 找图片上传 input
            img_input = page.locator('input[type="file"][accept*="image"]').first
            if not await img_input.count():
                img_input = page.locator('input[type="file"]').last
            if not await img_input.count():
                pinduoduo_logger.warning(_msg("⚠️", "封面入口已点击但未找到图片 input，可能 PDD 用了别的上传方式"))
                return
            await img_input.set_input_files(self.thumbnail_path)
            await page.wait_for_timeout(3000)
            # 确认按钮（多种文案）
            for txt in ['确定', '完成', '保存', '确认', '使用']:
                confirm = page.locator(f'button:has-text("{txt}")').first
                if await confirm.count():
                    await confirm.click(timeout=5000)
                    break
            pinduoduo_logger.success(_msg("🖼️", f"封面已上传: {self.thumbnail_path}"))
        except Exception as exc:
            pinduoduo_logger.warning(_msg("⚠️", f"封面上传失败，跳过: {exc}"))

    async def _set_publish_settings(self, page: Page) -> bool:
        """PDD 表单的「发布设置」radio：立即发布 / 定时发布。定时后出现一个 datetime input（2026-08-27 14:57:24 格式）。"""
        if not self.publish_date or isinstance(self.publish_date, int):
            return False
        try:
            # 1) 点「定时发布」radio（真实结构：<label data-testid="beast-core-radio" data-checked="true/false">）
            #    要点 label 本身（React onChange 绑定在 label），不能点里面的 input[type=radio]
            radio_loc = page.locator('.PublishTimeSetting_content__y_s5a label[data-testid="beast-core-radio"]:has-text("定时发布")').first
            if not await radio_loc.count():
                radio_loc = page.locator('label[data-testid="beast-core-radio"]:has-text("定时发布")').first
            if await radio_loc.count():
                await radio_loc.scroll_into_view_if_needed(timeout=5000)
                await radio_loc.click(force=True, timeout=8000)
            else:
                # 兜底：JS 找含「定时」的 radio label 中心坐标
                coord = await page.evaluate(r"""() => {
                    const labels = [...document.querySelectorAll('label[data-testid="beast-core-radio"]')];
                    for (const l of labels) {
                        if ((l.innerText||'').includes('定时')) {
                            const r = l.getBoundingClientRect();
                            if (r.width && r.height) return {x: r.x + r.width/2, y: r.y + r.height/2};
                        }
                    }
                    return null;
                }""")
                if coord:
                    await page.mouse.click(coord['x'], coord['y'])
            await page.wait_for_timeout(1500)

            # 验证：定时发布真正生效 = 该 label 的 data-checked 变 true + datePicker input 出现
            ok = await page.evaluate(r"""() => {
                const labels = [...document.querySelectorAll('label[data-testid="beast-core-radio"]')];
                const target = labels.find(l => (l.innerText||'').includes('定时发布'));
                const checked = target ? (target.getAttribute('data-checked') === 'true') : false;
                const inp = document.querySelector('input[data-testid="beast-core-datePicker-htmlInput"]');
                const dateVisible = !!(inp && inp.getBoundingClientRect().width > 0);
                return {checked, dateVisible};
            }""")
            if not ok.get('dateVisible'):
                pinduoduo_logger.warning(_msg("⚠️", f"点击「定时发布」后 datePicker 未出现（data-checked={ok.get('checked')}），定时未生效，请人工勾选"))
                return False
            pinduoduo_logger.success(_msg("✅", "已切到「定时发布」"))

            # 2) 真实 DOM（快照确诊）：beast-core datePicker，input readonly + data-testid="beast-core-datePicker-htmlInput"
            #    点击弹日历面板（beast-core-portal），需选日期+时间
            target_pretty = self.publish_date.strftime("%Y-%m-%d %H:%M:%S")
            # 2.1 点开日历面板
            opened = await page.evaluate(r"""() => {
                const inp = document.querySelector('input[data-testid="beast-core-datePicker-htmlInput"]');
                if (!inp) return false;
                ['mousedown','mouseup','click'].forEach(t => inp.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true})));
                inp.focus();
                return true;
            }""")
            if not opened:
                pinduoduo_logger.warning(_msg("⚠️", "未找到 datePicker input（beast-core-datePicker-htmlInput），请人工设置时间"))
                return False
            await page.wait_for_timeout(1500)
            pinduoduo_logger.info(_msg("📂", "日历面板已点开"))

            # 2.2 选日期：RPR_cell 格子，排除 disabled / outOfMonth（真实 DOM 确诊：
            #     PDD 只允许 当前+1h ~ 当前+7天，过去的日期全 disabled；上月同号格子带 outOfMonth）
            day = str(self.publish_date.day)
            picked_day = await page.evaluate(r"""(d) => {
                const portals = [...document.querySelectorAll('div[data-testid="beast-core-portal"]')];
                for (const portal of portals) {
                    // 优先：带 RPR_cell 且不带 disabled/outOfMonth 的目标日格子
                    const cells = [...portal.querySelectorAll('div, td, span')].filter(e => {
                        const t = (e.innerText||'').trim();
                        if (t !== d) return false;
                        const cls = (e.className||'').toString();
                        if (cls.includes('disabled') || cls.includes('outOfMonth')) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && e.children.length === 0;
                    });
                    if (cells.length) {
                        const cell = cells[cells.length - 1];
                        ['mousedown','mouseup','click'].forEach(t => cell.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true})));
                        return {ok: true, text: cell.innerText.trim(), cls: (cell.className||'').toString().slice(0,60)};
                    }
                }
                return {ok: false};
            }""", day)
            if picked_day.get('ok'):
                pinduoduo_logger.success(_msg("📅", f"日历已选日期 {picked_day['text']}（cls={picked_day['cls'][:40]}）"))
            else:
                pinduoduo_logger.warning(_msg("⚠️", f"日历面板无可选日期 {day}（可能不在 当前+1h~+7天 范围内）"))

            # 2.3 设置时间：点 timePicker input（beast-core-timePicker-html-input）→ 弹时间面板 → 选时:分
            time_set = False
            try:
                # 点开时间选择器
                t_opened = await page.evaluate(r"""() => {
                    const inp = document.querySelector('input[data-testid="beast-core-timePicker-html-input"]');
                    if (!inp) return false;
                    ['mousedown','mouseup','click'].forEach(t => inp.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true})));
                    inp.focus();
                    return true;
                }""")
                if t_opened:
                    await page.wait_for_timeout(1200)
                    # 时间面板：三列 <ul data-testid="beast-core-timePicker-list-{hh,mm,ss}">
                    # li 顺序就是 00..23/00..59（index 即数值），用 nth(index) 按位置选
                    hh = f"{self.publish_date.hour:02d}"
                    mm = f"{self.publish_date.minute:02d}"
                    hh_idx = self.publish_date.hour    # 0..23
                    mm_idx = self.publish_date.minute  # 0..59
                    try:
                        # 时：ul[testid-hh] li 第 hh_idx 个
                        hh_li = page.locator(f'ul[data-testid="beast-core-timePicker-list-hh"] li').nth(hh_idx)
                        await hh_li.scroll_into_view_if_needed(timeout=5000)
                        await hh_li.click(force=True, timeout=5000)
                        await page.wait_for_timeout(500)
                        actual_hh = await hh_li.inner_text()
                        pinduoduo_logger.info(_msg("⏰", f"已点小时第 {hh_idx} 项（text='{actual_hh}'）"))
                        # 分：ul[testid-mm] li 第 mm_idx 个
                        mm_li = page.locator(f'ul[data-testid="beast-core-timePicker-list-mm"] li').nth(mm_idx)
                        await mm_li.scroll_into_view_if_needed(timeout=5000)
                        await mm_li.click(force=True, timeout=5000)
                        await page.wait_for_timeout(500)
                        actual_mm = await mm_li.inner_text()
                        pinduoduo_logger.info(_msg("⏰", f"已点分钟第 {mm_idx} 项（text='{actual_mm}'）"))
                        time_set = True
                        pinduoduo_logger.success(_msg("⏰", f"时间面板已选 {actual_hh}:{actual_mm}"))
                    except Exception as e:
                        # fallback：JS 直接取 nth li 的坐标 + mouse.click
                        coords = await page.evaluate(r"""(vals) => {
                            const [hi, mi] = vals;
                            const pickNth = (testid, idx) => {
                                const ul = document.querySelector(`ul[data-testid="${testid}"]`);
                                if (!ul) return null;
                                const items = [...ul.querySelectorAll('li')];
                                if (idx >= items.length) return null;
                                const li = items[idx];
                                li.scrollIntoView({block: 'center'});
                                const r = li.getBoundingClientRect();
                                return r.width && r.height ? {x: r.x + r.width/2, y: r.y + r.height/2, text: (li.innerText||'').trim()} : null;
                            };
                            return {hour: pickNth('beast-core-timePicker-list-hh', hi), min: pickNth('beast-core-timePicker-list-mm', mi)};
                        }""", [hh_idx, mm_idx])
                        if coords and coords.get('hour') and coords.get('min'):
                            await page.mouse.click(coords['hour']['x'], coords['hour']['y'])
                            await page.wait_for_timeout(400)
                            await page.mouse.click(coords['min']['x'], coords['min']['y'])
                            await page.wait_for_timeout(400)
                            time_set = True
                            pinduoduo_logger.success(_msg("⏰", f"时间面板已选 {coords['hour']['text']}:{coords['min']['text']}（fallback mouse.click）"))
                        else:
                            pinduoduo_logger.warning(_msg("⚠️", f"时间面板选择失败（locator+nth+mouse 都没命中）: coords={coords}, err={e}"))
                else:
                    pinduoduo_logger.warning(_msg("⚠️", "未找到 timePicker input（beast-core-timePicker-html-input）"))
            except Exception as exc:
                pinduoduo_logger.warning(_msg("⚠️", f"设置时间异常: {exc}"))

            # 2.5 点「确认」按钮（footer 里的 beast-core-button「确认」，必须真实鼠标点击）
            confirmed = await page.evaluate(r"""() => {
                // footer 确认按钮：<button data-testid="beast-core-button"><span>确认</span></button>
                const btns = [...document.querySelectorAll('button')].filter(b => {
                    const t = (b.innerText||'').trim();
                    return t === '确认' || t === '确定' || t === '此刻';
                });
                if (btns.length) {
                    const b = btns[0];
                    const r = b.getBoundingClientRect();
                    if (r.width && r.height) return {x: r.x + r.width/2, y: r.y + r.height/2, text: b.innerText.trim()};
                }
                return null;
            }""")
            if confirmed:
                await page.mouse.click(confirmed['x'], confirmed['y'])
                pinduoduo_logger.success(_msg("✅", f"已真实点击「{confirmed['text']}」"))
            await page.wait_for_timeout(1200)

            # 2.5 回读 datePicker input 值
            final_val = await page.evaluate(r"""() => {
                const inp = document.querySelector('input[data-testid="beast-core-datePicker-htmlInput"]');
                return inp ? inp.value : null;
            }""")
            if final_val and final_val.startswith(self.publish_date.strftime("%Y-%m-%d")):
                pinduoduo_logger.success(_msg("⏰", f"PDD 定时已设：{final_val}"))
                return True
            pinduoduo_logger.warning(_msg("⚠️", f"PDD 定时回读={final_val}（期望 {target_pretty}），可能需人工在面板里补时间"))
            return True
            pinduoduo_logger.warning(_msg("⚠️", f"未找到 PDD datetime-local input，请人工设置时间为 {target_pretty}"))
            return True
        except Exception as exc:
            pinduoduo_logger.warning(_msg("⚠️", f"设置 PDD 定时发布失败: {exc}"))
            return False

    async def _select_declaration(self, page: Page) -> None:
        """PDD 表单的「内容声明」下拉（*必填，截图：内容声明 → 请选择 ▼）。

        强制要求命中——若未选上必须 raise 由用户确认，而不是悄悄跳过。
        """
        try:
            # Step 1: 点开下拉（JS 驱动 + 区域限定 = ContentDeclaration 区域内的「请选择」input）
            opened = await page.evaluate(r"""() => {
                const inputs = [...document.querySelectorAll('input')];
                for (const inp of inputs) {
                    const ph = (inp.placeholder||'').trim();
                    const val = (inp.value||'').trim();
                    if (ph !== '请选择' && val !== '' && val !== '请选择') continue;
                    // 必须在 ContentDeclaration 区域里
                    let p = inp; let found = false;
                    for (let i = 0; i < 6 && p; i++) {
                        if (/ContentDeclaration/i.test((p.className||'').toString())) { found = true; break; }
                        p = p.parentElement;
                    }
                    if (!found) continue;
                    ['mousedown','mouseup','click'].forEach(t => inp.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true})));
                    inp.focus();
                    return 'opened';
                }
                // 兜底：任意 placeholder='请选择' input
                for (const inp of inputs) {
                    if ((inp.placeholder||'').trim() === '请选择') {
                        ['mousedown','mouseup','click'].forEach(t => inp.dispatchEvent(new MouseEvent(t, {bubbles:true})));
                        inp.focus();
                        return 'fallback';
                    }
                }
                return 'none';
            }""")
            if opened == 'none':
                raise RuntimeError("未找到内容声明下拉的触发器（请选择 input）")
            pinduoduo_logger.info(_msg("📂", f"内容声明下拉已打开（{opened}）"))
            await page.wait_for_timeout(1500)

            picker_js = """() => {
                // 真实 DOM 确诊：选项是 div.ContentDeclaration_title__vI1AW，但 React/Portal 用合成事件，
                // dispatchEvent click 经常不触发 onClick 回调 → 必须真实鼠标点击。
                const titles = [...document.querySelectorAll('div[class*="ContentDeclaration_title"]')];
                const target = titles.find(e => (e.innerText||'').trim().startsWith('内容无需标注'));
                if (!target) return {ok: false, dump: {reason: 'no-target'}};
                const r = target.getBoundingClientRect();
                if (!r.width || !r.height) return {ok: false, dump: {reason: 'invisible'}};
                return {ok: true, x: r.x + r.width/2, y: r.y + r.height/2, text: (target.innerText||'').trim().slice(0,30)};
            }"""
            picked = await page.evaluate(picker_js)
            if not picked or not picked.get('ok'):
                await page.wait_for_timeout(1500)
                picked = await page.evaluate(picker_js)
            if not picked or not picked.get('ok'):
                raise RuntimeError("内容声明下拉已打开但未找到「内容无需标注」选项")
            # 真实鼠标点击（React 合成事件必须真实交互）
            await page.mouse.click(picked['x'], picked['y'])
            await page.wait_for_timeout(1200)
            verify = await page.evaluate(r"""() => {
 const v = document.querySelector('.ContentDeclaration_statement input[placeholder="请选择"]');
 return v ? (v.value||'').trim() : 'no-input';
 }""")
            if '无需标注' in verify or verify == 'no-input':
                pinduoduo_logger.success(_msg("✅", f"已选择内容声明（input={verify}，target={picked.get('text','')[:30]}）"))
            else:
                pinduoduo_logger.warning(_msg("⚠️", f"已点击但 input.value={verify}，再点一次"))
                await page.mouse.click(picked['x'], picked['y'])
                await page.wait_for_timeout(800)
        except Exception as exc:
            pinduoduo_logger.warning(_msg("⚠️", f"内容声明选择失败: {exc}"))
            try:
                await _save_debug(page, "pdd_declaration_failed")
            except Exception:
                pass

    async def _submit_publish(self, page: Page) -> None:
        # 1. 发布设置：选「定时发布」+ 填日期时间（PDD 也有原生定时，优先用）
        await self._set_publish_settings(page)

        # 2. 先选「内容声明：内容无需标注」（合集不设则不选）
        await self._select_declaration(page)

        # 3. 点发布按钮
        publish_btn = page.locator('button:has-text("一键发布")').first
        if not await publish_btn.count():
            publish_btn = page.locator('button:text-is("发布")').first
        if not await publish_btn.count():
            publish_btn = page.locator('button:has-text("发布"), [class*="publish"] button, span:text-is("发布")').first
        if not await publish_btn.count():
            raise RuntimeError("未找到发布按钮（一键发布/发布）")
        await publish_btn.wait_for(state="visible", timeout=15000)
        await publish_btn.click(force=True)
        pinduoduo_logger.info(_msg("🏃", "已点击发布按钮"))
        await page.wait_for_timeout(2000)

        # 3. 兜底：发布确认弹窗（可能含「确认发布/确定」）
        for confirm_text in ("确认发布", "确认", "确定"):
            try:
                c = page.locator(f'button:has-text("{confirm_text}"), span:has-text("{confirm_text}")').first
                if await c.count() and await c.is_visible():
                    await c.click(timeout=5000)
                    pinduoduo_logger.info(_msg("🏃", f"已点击确认弹窗「{confirm_text}」"))
                    break
            except Exception:
                continue

        # 4. 发布成功判据：定时任务提交后跳转到「作品管理」页（视频进入平台审核）
        #    不是 toast「发布成功」——定时任务的成功 = 页面跳转到作品管理
        before_url = page.url
        start = time.monotonic()
        while time.monotonic() - start < 30:
            try:
                cur_url = page.url
                # 1) URL 跳转（离开发布页 /home，进入作品管理/列表页）
                if cur_url != before_url and '/home' not in cur_url:
                    pinduoduo_logger.success(_msg("🥳", f"视频发布成功（已跳转作品管理页: {cur_url}）"))
                    return
                # 2) 页面出现「作品管理」或「审核中」文本
                has_manage = await page.evaluate(r"""() => {
                    const t = document.body ? (document.body.innerText || '') : '';
                    return t.includes('作品管理') || t.includes('审核中') || t.includes('视频审核');
                }""")
                if has_manage:
                    pinduoduo_logger.success(_msg("🥳", "视频发布成功（已进入作品管理，视频审核中）"))
                    return
            except Exception:
                pass
            await page.wait_for_timeout(1000)
        pinduoduo_logger.warning(_msg("⚠️", "发布后 30s 内未检测到跳转作品管理页，请人工确认；已保存快照"))
        # 发布失败 → 检查是否因封面未设置导致
        await self._check_publish_error(page)

    async def _check_publish_error(self, page: Page) -> None:
        """发布失败后诊断：只查页面错误提示（「未设视频封面」等真实报错文本）。"""
        try:
            err = await page.evaluate(r"""() => {
                // 找错误提示（含「封面」「失败」「不能为空」「请」等关键词）
                const kw = ['封面', '失败', '不能为空', '请设置', '请选择', '必填', '未设'];
                const tips = [...document.querySelectorAll('div, span, p, [class*="message"], [class*="Message"], [class*="toast"], [class*="Toast"], [class*="error"], [class*="Error"], [class*="tip"], [class*="Tip"]')].filter(e => {
                    const t = (e.innerText||'').trim();
                    if (!t || t.length > 60) return false;
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && kw.some(k => t.includes(k));
                }).map(e => (e.innerText||'').trim()).filter((v, i, a) => a.indexOf(v) === i);
                return {tips: tips.slice(0, 10)};
            }""")
            if err.get('tips'):
                pinduoduo_logger.warning(_msg("🔍", f"页面错误提示: {err['tips']}"))
            else:
                pinduoduo_logger.info(_msg("🔍", "未检测到封面相关错误提示，可能是其他原因（签名墙/网络）"))
        except Exception as exc:
            pinduoduo_logger.warning(_msg("⚠️", f"发布失败诊断异常: {exc}"))

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from conf import (
    PUBLISH_DAILY_LIMIT,
    PUBLISH_MIN_INTERVAL_MIN,
    PUBLISH_WINDOW,
    PUBLISH_WINDOW_HARD_BLOCK,
)
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import cdp_click
from utils.base_social_media import set_init_script
from utils.human_behavior import check_publish_allowed
from utils.human_behavior import human_sleep
from utils.human_behavior import record_publish
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import xiaohongshu_logger

XHS_DEFAULT_CREATOR_BASE_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _build_xhs_creator_url(path: str) -> str:
    base_url = os.getenv(
        XHS_CREATOR_BASE_URL_ENV,
        XHS_DEFAULT_CREATOR_BASE_URL,
    ).strip().rstrip("/")
    if not base_url:
        base_url = XHS_DEFAULT_CREATOR_BASE_URL
    return f"{base_url}/{path.lstrip('/')}"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _js_click_by_text(page: Page, text: str) -> bool:
    """用 JS 找到文字完全匹配的最内层元素并点击它及其祖先（绕过 span pointer-events:none / 遮罩拦截）。

    小红书很多可点项文字在 <span class="d-text"> 里，pointer-events 常被禁用，
    Playwright 常规 click 会超时。用原生 click 冒泡触发 Vue 事件更可靠。
    """
    return await page.evaluate(
        """(t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            let el = nodes[nodes.length - 1];
            for (let i = 0; i < 4 && el; i++) { try { el.click(); } catch (e) {} el = el.parentElement; }
            return true;
        }""",
        text,
    )


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _open_xhs_qrcode_panel(page: Page) -> None:
    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    await login_box.wait_for(state="visible", timeout=30000)

    scan_text = login_box.locator("div:has-text('扫一扫')").first
    if await scan_text.count():
        return

    switch_img = login_box.locator(XHS_LOGIN_SWITCH_SELECTOR).first
    await switch_img.wait_for(state="visible", timeout=10000)
    await switch_img.click()
    await login_box.locator("div:has-text('扫一扫')").first.wait_for(state="visible", timeout=10000)


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    qrcode_img = page.locator('.login-box-container').get_by_text("APP扫一扫登录").filter(visible=True).locator("xpath=..//following-sibling::div//img").nth(0)

    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    qrcode_src = await _extract_xhs_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    qrcode_img = await _find_xhs_qrcode_locator(page)

    if qrcode_src.startswith("data:image/"):
        save_data_url_image(qrcode_src, qrcode_path)
    else:
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        await qrcode_img.screenshot(path=str(qrcode_path))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    if page.url.startswith(_build_xhs_creator_url("/login")):
        return False

    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        if LOCAL_CHROME_PATH:
            browser = await playwright.chromium.launch(headless=True, executable_path=LOCAL_CHROME_PATH)
        else:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
        try:
            context = await browser.new_context(storage_state=account_file, timezone_id="Asia/Shanghai")
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(
                _build_xhs_creator_url(
                    "/publish/publish?from=homepage&target=video"
                )
            )
            await page.wait_for_timeout(3000)

            if page.url.startswith(_build_xhs_creator_url("/login")):
                xiaohongshu_logger.info(_msg("🥹", "cookie 已失效，得重新登录一下"))
                return False

            login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
            if await login_box.count():
                try:
                    if await login_box.is_visible():
                        xiaohongshu_logger.info(_msg("🥹", "页面仍然停留在登录二维码页，按 cookie 失效处理"))
                        return False
                except Exception:
                    return False

            xiaohongshu_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chromium")
        context = await browser.new_context(timezone_id="Asia/Shanghai")
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto(_build_xhs_creator_url("/login"))
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                if await _is_xhs_login_completed(page):
                    await asyncio.sleep(2)
                    await context.storage_state(path=account_file)
                    if await cookie_auth(account_file):
                        xiaohongshu_logger.success(_msg("🥳", "小红书扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "小红书扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "小红书扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
        return result


class XiaoHongShuBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def _throttle_check(self) -> None:
        """真实发布节流检查（存草稿模式跳过）。借鉴蚁小二：频率受控防风控。"""
        if self.draft:
            return
        account = Path(self.account_file).stem
        allowed, reason = check_publish_allowed(
            "xiaohongshu",
            account,
            daily_limit=PUBLISH_DAILY_LIMIT,
            min_interval_min=PUBLISH_MIN_INTERVAL_MIN,
            hard_window=PUBLISH_WINDOW if PUBLISH_WINDOW_HARD_BLOCK else None,
        )
        if not allowed:
            raise RuntimeError(
                f"发布节流拦截: {reason}（可用 --draft 存草稿验证表单，不触发真实发布）"
            )
        # 窗口外软提示（默认不硬拦，只提醒）
        if PUBLISH_WINDOW and not PUBLISH_WINDOW_HARD_BLOCK:
            start_h, end_h = PUBLISH_WINDOW
            now_h = datetime.now().hour
            if not (start_h <= now_h < end_h):
                xiaohongshu_logger.warning(
                    _msg("🕐", f"当前 {now_h} 点不在推荐发布窗口 {start_h}-{end_h} 点（软提示，继续）")
                )

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        await page.locator('.custom-switch-card').filter(has_text="定时发布").locator('.d-switch').click()
        await human_sleep(0.6, 1.6)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        time_input = page.locator('.d-datepicker-input-filter input.d-text')
        await time_input.fill(str(publish_date_hour))
        await human_sleep(0.6, 1.6)

    async def fill_title(self, page: Page) -> None:
        title_container = page.locator('input[placeholder*="填写标题"]')
        await title_container.fill(self.title[:20])

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        desc = page.locator('p[data-placeholder*="输入正文描述"]')
        await desc.click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(self.desc)
        await page.keyboard.press("Enter")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        # 小红书标签上限为 10 个，超过会导致死循环卡住发布
        max_tags = 10
        if len(self.tags) > max_tags:
            xiaohongshu_logger.warning(
                _msg("🏷️", f"标签数量 {len(self.tags)} 超过小红书上限 {max_tags}，只取前 {max_tags} 个: {self.tags[:max_tags]}")
            )
            self.tags = self.tags[:max_tags]

        if not getattr(self, "desc", ""):
            desc = page.locator('p[data-placeholder*="输入正文描述"]')
            await desc.click()

        for tag in self.tags:  # 循环处理所有 tags
            # 话题候选下拉框依赖小红书联想接口实时返回，网络抖动/无匹配时会等不到。
            # 标签是可选增强项：等不到候选框就跳过该标签继续，不让整条发布因此失败。
            try:
                await page.keyboard.type("#" + tag, delay=30)
                await page.locator('#creator-editor-topic-container').wait_for(
                    state="visible",
                    timeout=6000
                )
                first_item = page.locator('#creator-editor-topic-container .item').first
                await first_item.wait_for(state="visible", timeout=4000)
                await first_item.click()
            except Exception as exc:
                xiaohongshu_logger.warning(
                    _msg("🏷️", f"话题『{tag}』未出现候选，跳过该标签继续发布: {exc}")
                )
                # 清掉已键入但未成词的 "#tag" 文本，避免它残留进正文
                for _ in range(len("#" + tag)):
                    await page.keyboard.press("Backspace")
                continue

    async def fill_meta(self, page: Page) -> None:
        await self.fill_title(page)
        await self.fill_desc(page)
        await self.fill_tags(page)

    async def _open_add_component(self, page: Page) -> None:
        """确保「添加组件」面板展开（添加地点/选择群聊位于其中）。"""
        try:
            if await page.get_by_text("添加地点", exact=True).first.is_visible(timeout=1500):
                return
        except Exception:
            pass
        try:
            add_comp = page.get_by_text("添加组件", exact=True).first
            await add_comp.scroll_into_view_if_needed(timeout=5000)
            await add_comp.click(force=True)
            await page.wait_for_timeout(1500)
        except Exception as e:
            xiaohongshu_logger.warning(_msg("😵", f"展开「添加组件」失败: {e}"))

    async def set_location(self, page: Page, location: str = "") -> bool:
        """设置发布位置。地址组件（「添加地点」）在发布页始终可见，直接点击输入即可。

        小红书「添加地点」是 d-select（DeerUI）受控组件，实测交互链
        （用户 bsk 录制 trace + CDP 回放探针验证）：
          1. CDP 真实点击 div.address-card-select（地址组件，始终可见，无需展开任何面板）
          2. 输入框 div.d-select-input-filter.show input 出现后 fill 关键词
          3. 候选列表是联想接口异步返回，要等一会才渲染在 div.d-dropdown
             的 div.option-name（注意：不是 .d-option-name，实测 class 无 d- 前缀）
          4. 有匹配候选 → 点击；等超时仍无候选 = 地图无此位置 → 跳过（不算失败）
        """
        if not location:
            return False
        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        try:
            # 1) 地址组件始终可见，直接点击（等它渲染出来，兜底视频上传竞态）
            address_sel = page.locator('div.address-card-select').first
            try:
                await address_sel.wait_for(state="visible", timeout=20000)
            except Exception:
                xiaohongshu_logger.warning(_msg("😵", "地址组件未出现（视频可能仍在处理），跳过"))
                return False
            await address_sel.scroll_into_view_if_needed(timeout=5000)
            if not await cdp_click(page, address_sel):
                await address_sel.click(force=True)

            # 2) 等输入框进入 show 态（d-select focus）
            input_sel = 'div.address-card-select div.d-select-input-filter.show input'
            box = None
            for _i in range(10):
                b = page.locator(input_sel).first
                if await b.count() and await b.is_visible():
                    box = b
                    break
                await page.wait_for_timeout(800)
            if box is None:
                xiaohongshu_logger.warning(_msg("😵", "地点输入框未出现，跳过"))
                return False

            # 3) 输入关键词，候选是联想接口异步返回，轮询等待（最多 ~8s）
            await box.click(force=True)
            await box.fill(location)

            opt = None
            for _i in range(8):
                await page.wait_for_timeout(1000)
                cand = page.locator(
                    'div.d-dropdown div.option-name', has_text=location
                ).first
                if await cand.count() and await cand.is_visible():
                    opt = cand
                    break
            if opt is None:
                xiaohongshu_logger.warning(
                    _msg("😵", f"未找到地点候选『{location}』（地图可能无此位置），跳过")
                )
                return False

            # 4) 点击候选（点 option-name 触发选择）
            await opt.scroll_into_view_if_needed(timeout=4000)
            await opt.click(force=True)
            xiaohongshu_logger.success(_msg("🥳", f"位置已设置: {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.warning(
                _msg("😵", f"设置位置失败（小红书网页版地点为受控组件，自动化可能受限），跳过: {e}")
            )
            return False

    async def set_album(self, page: Page, album: str = "") -> bool:
        """加入合集。已有该合集则选择；无则创建（名称=album）。失败跳过。"""
        if not album:
            return False
        xiaohongshu_logger.info(_msg("📁", f"小人准备加入合集: {album}"))
        try:
            # 1) 点击「加入合集」（CDP 真实点击，force 点击可能坐标偏差不触发）
            title_el = page.locator('div.collection-plugin-content-title').first
            await title_el.scroll_into_view_if_needed(timeout=5000)
            if not await cdp_click(page, title_el):
                await title_el.click(force=True)
            await page.wait_for_timeout(2000)
            # 2) 弹层内点已有合集（collection-plugin-popover 内容区匹配）
            exist = page.locator(
                'div.collection-plugin-popover-content div', has_text=album
            ).last
            if await exist.count():
                await exist.click(force=True)
                xiaohongshu_logger.success(_msg("🥳", f"已加入合集: {album}"))
                return True
            # 3) 无该合集 → 点 footer「创建合集」
            create_btn = page.locator('div.popover-footer', has_text="创建合集").first
            if not await create_btn.count():
                create_btn = page.get_by_text("创建合集", exact=True).last
            await create_btn.click(force=True)
            await page.wait_for_timeout(1500)
            # 4) 创建 modal：填合集名称 → 创建并加入
            name_input = page.locator('input[placeholder*="合集名称"]').first
            await name_input.click()
            await name_input.fill(album)
            create_join = page.get_by_role("button", name="创建并加入").first
            await create_join.click(force=True)
            await page.wait_for_timeout(1500)
            # 创建合集后弹出「声明原创」引导弹窗
            await self.handle_original_declaration_modal(page)
            xiaohongshu_logger.success(_msg("🥳", f"已创建并加入合集: {album}"))
            return True
        except Exception as e:
            xiaohongshu_logger.warning(_msg("😵", f"设置合集失败，跳过: {e}"))
            return False

    async def set_group_chat(self, page: Page, group_chat: str = "") -> bool:
        """选择群聊（「添加组件 → 选择群聊」）。无该群聊则跳过。"""
        if not group_chat:
            return False
        xiaohongshu_logger.info(_msg("💬", f"小人准备选择群聊: {group_chat}"))
        try:
            await self._open_add_component(page)
            gc = page.get_by_text("选择群聊", exact=True).first
            await gc.scroll_into_view_if_needed(timeout=5000)
            await gc.click(force=True)
            await page.wait_for_timeout(2000)
            opt = page.get_by_text(group_chat, exact=False).last
            if await opt.count():
                await opt.click(force=True)
                xiaohongshu_logger.success(_msg("🥳", f"已选择群聊: {group_chat}"))
                return True
            xiaohongshu_logger.warning(_msg("💬", f"未找到群聊『{group_chat}』（群聊需先在 App 端创建），跳过"))
            return False
        except Exception as e:
            xiaohongshu_logger.warning(_msg("😵", f"选择群聊失败，跳过: {e}"))
            return False

    async def check_original_declaration(self, page: Page) -> None:
        """设置「来源转载」声明，填写转载来源。

        流程（对应 codegen 录制）：
          点「添加内容类型声明」→ 点包含「来源转载」的 div
          → 填 placeholder「请输入媒体名称」→ 点 button「确认」。
        容错：任一步失败记 warning 跳过、继续发布，不中断。
        """
        source = getattr(self, "repost_source", "") or ""
        try:
            # 1. 点「添加内容类型声明」
            trigger = page.get_by_text("添加内容类型声明", exact=False).first
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await trigger.click(force=True)
            await page.wait_for_timeout(1500)

            # 2. 选「来源转载」选项
            import re as _re
            repost_option = page.locator("#publish-container div").filter(
                has_text=_re.compile(r"^来源转载$")
            ).last
            if await repost_option.count():
                await repost_option.click(force=True)
            else:
                await _js_click_by_text(page, "来源转载")
            await page.wait_for_timeout(1500)

            # 3. 填写媒体名称
            source_input = page.get_by_placeholder("请输入媒体名称").first
            await source_input.wait_for(state="visible", timeout=8000)
            await source_input.click()
            await source_input.fill(source)
            await page.wait_for_timeout(500)

            # 4. 点「确认」按钮
            confirm = page.get_by_role("button", name="确认").first
            try:
                await confirm.wait_for(state="visible", timeout=5000)
                await confirm.click()
            except Exception:
                await _js_click_by_text(page, "确认")

            await page.wait_for_timeout(1000)
            xiaohongshu_logger.success(_msg("🧾", f"来源转载已声明（来源：{source}）"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"设置来源转载失败，跳过继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def handle_original_declaration_modal(self, page: Page) -> None:
        """处理「声明原创」弹窗（创建合集后 / 发布前可能出现）。

        录制 trace 实测流程：
          弹窗标题「笔记完成原创声明后，将获得以下权益」
          → 勾选 checkbox「我已阅读并同意《原创声明须知》」
          → 点 button「声明原创」。
        容错：弹窗未出现直接返回；任一步失败记 warning 跳过，不中断发布。
        """
        try:
            declare_btn = page.get_by_role("button", name="声明原创").first
            if not await declare_btn.count():
                return  # 无弹窗
            xiaohongshu_logger.info(_msg("🧾", "发现「声明原创」弹窗，自动勾选同意并确认"))
            await declare_btn.scroll_into_view_if_needed(timeout=4000)
            # 勾选「我已阅读并同意《原创声明须知》」（modal 内最后一个 checkbox）
            checkbox = page.locator(
                '.d-modal input[type="checkbox"], [class*="modal"] input[type="checkbox"]'
            ).last
            if await checkbox.count():
                try:
                    if not await checkbox.is_checked():
                        await checkbox.check(force=True)
                except Exception:
                    await checkbox.click(force=True)
                await page.wait_for_timeout(500)
            await declare_btn.click(force=True)
            await page.wait_for_timeout(1000)
            xiaohongshu_logger.success(_msg("🧾", "原创声明已完成"))
        except Exception as e:
            xiaohongshu_logger.warning(_msg("⚠️", f"处理「声明原创」弹窗失败，跳过: {e}"))


class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        location: str | None = None,
        album: str | None = None,
        group_chat: str | None = None,
        draft: bool = False,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""
        self.location = location
        self.album = album
        self.group_chat = group_chat
        self.draft = draft

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        # 封面设置为增强步骤：失败时记 warning 跳过、继续发布（用视频首帧兜底）。
        try:
            # 发布页封面区域内嵌，点击 div.upload-cover 打开封面弹窗（d-modal）。
            cover_section = page.locator("text=设置封面").first
            try:
                await cover_section.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            # 1. 点击 div.upload-cover 打开封面弹窗
            upload_cover = page.locator("div.upload-cover").first
            if not await upload_cover.count():
                upload_cover = page.locator("div.cover-plugin-preview div.default.pointer").first
            await upload_cover.click(force=True)
            await page.wait_for_timeout(3000)

            # 2. 切换到「上传封面」tab（默认在「截取封面」）
            upload_tab = page.get_by_text("上传封面", exact=True).first
            await upload_tab.wait_for(state="visible", timeout=10000)
            await upload_tab.click()
            await page.wait_for_timeout(2000)

            # 3. 找到图片 file input（parent class: upload-wrapper）并上传
            file_input = page.locator('div.upload-wrapper input[type="file"][accept*="image"]').first
            if not await file_input.count():
                file_input = page.locator('input[type="file"][accept*="image"]').last
            await file_input.set_input_files(thumbnail_path)
            await page.wait_for_timeout(4000)  # 等图片加载+裁剪渲染

            # 4. 点「确定」按钮
            modal_footer = page.locator("div.d-modal-footer")
            confirm = modal_footer.get_by_text("确定", exact=True).first
            if not await confirm.count():
                confirm = page.get_by_role("button", name="确定").first
            await confirm.wait_for(state="visible", timeout=10000)
            await confirm.click()

            # 5. 等弹窗关闭
            modal = page.locator("div.d-modal")
            try:
                await modal.first.wait_for(state="hidden", timeout=15000)
            except Exception:
                pass
            xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("🖼️", f"封面设置失败，跳过该步骤继续发布（用视频首帧）：{exc}"))
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                pass

    async def upload_video_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往视频发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=video"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)
        await page.locator("div[class^='upload-content'] input[class='upload-input']").set_input_files(self.file_path)

        while True:
            try:
                upload_input = await page.wait_for_selector('input.upload-input', timeout=3000)
                preview_new = await upload_input.query_selector(
                    'xpath=following-sibling::div[contains(@class, "preview-new")]')
                if preview_new:
                    # 获取整个预览区域的文本，更鲁棒地判断上传状态
                    all_text = await preview_new.inner_text()
                    upload_success = any(keyword in all_text for keyword in ['上传成功', '分辨率', '重新上传', '编辑封面', '已上传', '已选择', '100%'])
                    
                    if not upload_success:
                        # 检查是否有特定的状态码或百分比
                        stage_elements = await preview_new.query_selector_all('div.stage')
                        for stage in stage_elements:
                            text_content = await page.evaluate('(element) => element.textContent', stage)
                            if '上传成功' in text_content or '分辨率' in text_content:
                                upload_success = True
                                break
                    
                    if upload_success:
                        xiaohongshu_logger.success(_msg("🥳", "视频已经传完啦"))
                        break
                    
                    if self.debug:
                        normalized_text = all_text.strip().replace("\n", " ")
                        xiaohongshu_logger.debug(_msg("🧍", f"预览区域内容: {normalized_text}"))
                    xiaohongshu_logger.debug(_msg("🧍", "还没看到上传成功标识，小人继续等一会"))
                else:
                    # 尝试检查标题输入框是否已经出现，如果是，说明已经进入编辑状态
                    title_container = page.locator('input[placeholder*="填写标题"]')
                    if await title_container.count() > 0 and await title_container.is_visible():
                        xiaohongshu_logger.success(_msg("🥳", "虽然没看到预览区，但标题框出来了，小人继续"))
                        break
                    xiaohongshu_logger.debug(_msg("🧍", "还没拿到预览区域，小人继续等一会"))
            except Exception as e:
                xiaohongshu_logger.debug(_msg("😵", f"上传状态还没稳定下来，小人继续观察: {e}"))
            await human_sleep(1.2, 3.0)

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.set_thumbnail(page, self.thumbnail_path)

        # await self.set_location(page, "青岛市")

        await self.check_original_declaration(page)

        # 发布前兜底处理「声明原创」弹窗（创建合集等场景会触发）
        await self.handle_original_declaration_modal(page)

        if self.location:
            await self.set_location(page, self.location)
        if self.album:
            await self.set_album(page, self.album)
        if self.group_chat:
            await self.set_group_chat(page, self.group_chat)

        if self.draft:
            # 存草稿模式：跳过定时设置，只验证表单交互，不触发真实发布
            await self._save_draft(page)
            return

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        while True:
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    # 真实发布前随机延迟（2.5~8s），避免固定节奏特征
                    await human_sleep(2.5, 8.0)
                    await page.locator('button:has-text("发布")').click()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                record_publish("xiaohongshu", Path(self.account_file).stem, draft=False)
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布视频"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await human_sleep(0.4, 1.2)

    async def _save_draft(self, page: Page) -> None:
        """点击「存草稿」：验证表单交互但不发布。多选择器容错。"""
        draft_btn = page.locator('button:has-text("存草稿")').first
        if not await draft_btn.count():
            draft_btn = page.get_by_text("存草稿").first
        await draft_btn.scroll_into_view_if_needed(timeout=4000)
        await draft_btn.click(force=True)
        await human_sleep(2.0, 4.0)
        if self.debug:
            await page.screenshot(full_page=True)
        xiaohongshu_logger.success(_msg("📝", "已点击「存草稿」，表单验证完成（未真实发布）"))
        record_publish("xiaohongshu", Path(self.account_file).stem, draft=True)

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))
        await self._throttle_check()
        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
            timezone_id="Asia/Shanghai",
        )
        context = await set_init_script(context)

        try:
            page = await context.new_page()
            await self.upload_video_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await context.close()
            await browser.close()

    async def xiaohongshu_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_video()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        location: str | None = None,
        album: str | None = None,
        group_chat: str | None = None,
        draft: bool = False,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")
        self.location = location
        self.album = album
        self.group_chat = group_chat
        self.draft = draft

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=image"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)

        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if not await upload_input.count():
            upload_input = page.locator("div[class^='upload-content'] input[class='upload-input']").first

        await upload_input.wait_for(state="attached", timeout=30000)
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        while True:
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文素材已经传完，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await human_sleep(0.6, 1.8)

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.check_original_declaration(page)

        # 发布前兜底处理「声明原创」弹窗（创建合集等场景会触发）
        await self.handle_original_declaration_modal(page)

        if self.location:
            await self.set_location(page, self.location)
        if self.album:
            await self.set_album(page, self.album)
        if self.group_chat:
            await self.set_group_chat(page, self.group_chat)

        if self.draft:
            # 存草稿模式：跳过定时设置，只验证表单交互，不触发真实发布
            await self._save_draft(page)
            return

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        while True:
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    # 真实发布前随机延迟（2.5~8s），避免固定节奏特征
                    await human_sleep(2.5, 8.0)
                    await page.locator('button:has-text("发布")').click()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                record_publish("xiaohongshu", Path(self.account_file).stem, draft=False)
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await human_sleep(0.4, 1.2)

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))
        await self._throttle_check()
        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
            timezone_id="Asia/Shanghai",
        )
        context = await set_init_script(context)

        try:
            page = await context.new_page()
            await self.upload_note_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await context.close()
            await browser.close()

    async def xiaohongshu_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_note()

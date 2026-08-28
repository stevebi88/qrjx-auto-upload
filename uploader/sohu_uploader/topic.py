# -*- coding: utf-8 -*-
"""搜狐号图文发布（搜狐视频创作者中心 #/upload/topic）。

复用 sohu_uploader.main 的登录态、反检测、定时发布组件：
  - 正文内容（富文本，0/2000 字符，必填）
  - 话题 / 图片（最多9张）/ 圈子（均可选）
  - 更多选项 → 定时发布（指定时间发布 + 日期时间）→ 创作内容声明（无需标注）
  - 发布图文按钮
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, Playwright, async_playwright

from conf import LOCAL_CHROME_HEADLESS
from uploader.sohu_uploader.main import (
    _build_launch_kwargs,
    _msg,
    _resolve_account_file,
    _save_debug,
    _stealth_context,
    sohu_logger,
)

SOHU_TOPIC_URL = "https://tv.sohu.com/s/center/index.html#/upload/topic"


class SohuTopic:
    """搜狐号图文发布。"""

    def __init__(
        self,
        content,
        account_file,
        topic: str | None = None,
        images: list[str] | None = None,
        publish_date=0,
        debug: bool = True,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.content = str(content or "").strip()
        self.topic = (topic or "").strip()
        self.images = [str(i) for i in (images or []) if i]
        self.account_file = _resolve_account_file(account_file)
        self.publish_date = publish_date
        self.debug = debug
        self.headless = headless

    async def validate_upload_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成搜狐号登录: {self.account_file}")
        if not self.content:
            raise ValueError("图文正文不能为空")
        if len(self.content) > 2000:
            raise ValueError(f"图文正文超过 2000 字（当前 {len(self.content)}）")

    async def upload(self, playwright: Playwright) -> None:
        sohu_logger.info(_msg("🧍", "先检查 cookie"))
        await self.validate_upload_args()

        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=self.headless))
        context = await _stealth_context(await browser.new_context(storage_state=self.account_file))
        try:
            page = await context.new_page()
            await page.goto(SOHU_TOPIC_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            # 1) 正文内容（富文本）
            editor = page.locator("div[contenteditable=true]").first
            await editor.wait_for(state="visible", timeout=30000)
            await editor.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            await editor.fill(self.content)
            sohu_logger.info(_msg("📝", f"正文已填写（{len(self.content)}字）"))

            # 2) 图片（可选，最多9张）
            if self.images:
                img_input = page.locator('input[type="file"][class*="uploadImg"], input[type="file"][accept*="image"]').first
                if not await img_input.count():
                    img_input = page.locator('input[type="file"]').first
                await img_input.set_input_files([p for p in self.images[:9]])
                sohu_logger.info(_msg("🖼️", f"已选择 {len(self.images[:9])} 张图片"))
                await page.wait_for_timeout(2000)

            # 3) 话题（可选）
            if self.topic:
                topic_inp = page.locator('input[placeholder*="话题"]').first
                if await topic_inp.count():
                    await topic_inp.click()
                    await topic_inp.fill(self.topic.lstrip("#"))
                    await page.keyboard.press("Enter")
                    sohu_logger.info(_msg("🏷️", f"话题已填写: {self.topic}"))

            # 4) 展开「更多选项」
            mo = page.get_by_text("更多选项", exact=True).first
            if await mo.count():
                await mo.click(timeout=5000)
                await page.wait_for_timeout(1200)
                sohu_logger.info(_msg("🏃", "已展开「更多选项」"))

            # 5) 定时发布（顶部下拉，与视频同款）
            if self.publish_date and not isinstance(self.publish_date, int):
                await self._set_top_schedule(page)

            # 6) 创作内容声明：无需标注
            decl = page.locator('.radio_item:has-text("无需标注")').first
            if await decl.count():
                await decl.scroll_into_view_if_needed()
                await decl.click(timeout=5000)
                sohu_logger.success(_msg("✅", "已勾选「无需标注」"))

            # 7) 发布图文（注意：按钮初始带 disable class，正文填好才解除禁用，需等 disable 消失）
            pub = page.locator('span.button-red:has-text("发布图文")').first
            if not await pub.count():
                pub = page.locator('span.button-red:has-text("发布"), button:has-text("发布图文"), button:has-text("发布")').first
            if not await pub.count():
                raise RuntimeError("未找到「发布图文」按钮")
            await pub.wait_for(state="attached", timeout=15000)
            # 等待 disable 解除（最多 30s；正文必填，若未填正文会一直 disable）
            for _ in range(30):
                cls = await pub.get_attribute("class") or ""
                if "disable" not in cls:
                    break
                await page.wait_for_timeout(1000)
            else:
                sohu_logger.warning(_msg("⚠️", "「发布图文」按钮 30s 内仍为禁用态（正文可能未填写成功）"))
            try:
                await pub.scroll_into_view_if_needed()
            except Exception:
                pass
            await pub.click(force=True)
            sohu_logger.info(_msg("🏃", "已点击「发布图文」"))

            # 8) 兜底弹窗（缺字段时会弹创作声明等）
            await self._handle_publish_modal(page)

            # 9) 等待成功（toast / URL 跳到内容列表 / 关闭的确认弹窗 都算成功）
            start = time.monotonic()
            while time.monotonic() - start < 30:
                try:
                    toast = page.locator(
                        '[class*="message"]:has-text("成功"), [class*="toast"]:has-text("成功"), text="发布成功", text="提交成功", text="已发布"'
                    ).first
                    if await toast.count() and await toast.is_visible():
                        sohu_logger.success(_msg("🥳", "图文发布成功"))
                        return
                    cur_url = page.url.lower()
                    if "content" in cur_url or "list" in cur_url or "管理" in cur_url:
                        sohu_logger.success(_msg("🥳", "图文发布成功（已跳到内容列表）"))
                        return
                except Exception:
                    pass
                await page.wait_for_timeout(1000)
            sohu_logger.warning(_msg("⚠️", "发布后 30s 内未捕获成功提示，请人工确认；已保存快照"))
            await _save_debug(page, "topic_publish")

            await context.storage_state(path=self.account_file)
            sohu_logger.success(_msg("🥳", "cookie 更新完毕"))
        except Exception as exc:
            await _save_debug(page, "topic_error")
            raise
        finally:
            await context.close()
            await browser.close()

    async def _set_top_schedule(self, page: Page) -> None:
        """顶部「定时发布」：切「指定时间发布」→ 选日期 → 箭头翻页选时/分 → 确定（JS 驱动，与视频共用 .form-item-issue 结构）。"""
        if not self.publish_date or isinstance(self.publish_date, int):
            return
        try:
            js_click = (
                "() => { const i=document.querySelector('%s'); if(i) i.dispatchEvent(new MouseEvent('click',{bubbles:true})); }"
            )

            # 0) 展开「更多选项」（button.form-more-button）
            await self._js_click(page, "document.querySelector('button.form-more-button')?.click()")
            await page.wait_for_timeout(1200)

            # 1) 切「指定时间发布」：直接 dispatch click li（dropdown 常驻 DOM，先点 input 反而破坏 Vue 状态）
            await self._js_click(
                page,
                "() => { const lis=[...document.querySelectorAll('.form-item-issue .select-issue li.select-item')]; "
                "const t=lis.find(l=>l.textContent.includes('指定时间发布')); if(t) t.dispatchEvent(new MouseEvent('click',{bubbles:true})); }",
            )
            await page.wait_for_timeout(800)

            # 2) 日期：视频页带前导零(2026-08-28)、图文页无前导零(2026-8-28)，两种格式都试
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

            # 3) 时间：找"时间"label后的 .select 容器 → 点 input 打开浮层（视频页/图文页均兼容）
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
            await self._js_click(page, js_click % ".form-item-issue .time-pan-foot .confirm")
            await page.wait_for_timeout(600)
            sohu_logger.success(_msg("⏰", f"定时发布已设：{target_date} {target_hour}:{target_minute}（时={h_ok} 分={m_ok}）"))
        except Exception as exc:
            sohu_logger.warning(_msg("⚠️", f"设置定时发布失败: {exc}"))

    async def _js_click(self, page: Page, js_expr: str) -> None:
        # js_expr 可能是裸语句（无箭头）或已是完整箭头函数，避免双重包裹导致不执行
        s = js_expr.strip()
        if not s.startswith("() =>"):
            s = "() => { %s }" % s
        try:
            await page.evaluate(s)
        except Exception:
            pass

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
        for _ in range(70):
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

    async def _handle_publish_modal(self, page: Page) -> None:
        """兜底：发布弹窗（创作声明等缺字段时）。"""
        try:
            modal = page.locator(
                ".alertBox:visible, [class*='dialog']:visible, [class*='modal']:visible"
            ).first
            await modal.wait_for(state="visible", timeout=5000)
        except Exception:
            return
        try:
            body = await modal.inner_text()
        except Exception:
            body = ""
        if any(k in body for k in ("声明", "原创", "标注")):
            item = modal.locator('.radio_item:has-text("无需标注")').first
            if await item.count():
                await item.click(timeout=4000)
                sohu_logger.success(_msg("✅", "弹窗里已勾选「无需标注」"))
                await page.wait_for_timeout(800)
        try:
            btn = modal.locator('a.btn-confirm, a:has-text("确认发布"), a:has-text("确定")').first
            if await btn.count():
                for _ in range(10):
                    cls = await btn.get_attribute("class") or ""
                    if "disabled" not in cls:
                        break
                    await page.wait_for_timeout(1000)
                await btn.click(timeout=5000)
                sohu_logger.info(_msg("🏃", "已点击「确认发布」"))
                return
        except Exception:
            pass
        for confirm_text in ("确定", "确认", "完成"):
            try:
                btn = modal.locator(f'button:has-text("{confirm_text}"), span:has-text("{confirm_text}")').first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=5000)
                    sohu_logger.info(_msg("🏃", f"已点击弹窗「{confirm_text}」"))
                    return
            except Exception:
                continue

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

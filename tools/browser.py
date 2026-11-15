"""Headless Chrome browser automation via Playwright with SSRF route guard."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile

from tools.base import ToolContext, tool
from tools.web import HEADERS, check_url_safe

_pw = None
_browser = None
_page = None
_browser_lock = asyncio.Lock()


async def _get_page():
    global _pw, _browser, _page
    if _page is None or _page.is_closed():
        from playwright.async_api import async_playwright

        if _pw is None:
            _pw = await async_playwright().start()
        if _browser is None or not _browser.is_connected():
            _browser = await _pw.chromium.launch(headless=True)
        context = await _browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=HEADERS["User-Agent"],
        )

        async def guard(route):
            try:
                await check_url_safe(route.request.url)
            except ValueError:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", guard)
        _page = await context.new_page()
    return _page


async def close_browser() -> None:
    global _pw, _browser, _page
    async with _browser_lock:
        try:
            if _browser:
                await _browser.close()
            if _pw:
                await _pw.stop()
        finally:
            _pw = _browser = _page = None


async def _snapshot(pg) -> str:
    try:
        await pg.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        text = await pg.inner_text("body", timeout=5000)
    except Exception:
        text = ""
    try:
        elements = await pg.evaluate(
            """() => [...document.querySelectorAll(
                'a[href],button,input,textarea,select,[role=button]')]
              .filter(e => e.offsetParent !== null).slice(0, 40)
              .map(e => {
                const t = (e.innerText || e.value || e.placeholder ||
                           e.getAttribute('aria-label') || '').trim().slice(0, 60);
                const n = e.name ? '[name=' + e.name + ']' : '';
                return e.tagName.toLowerCase() + n + ': ' + t;
              })"""
        )
    except Exception:
        elements = []
    text = re.sub(r"\n{3,}", "\n\n", text)[:3500]
    try:
        title = await pg.title()
    except Exception:
        title = ""
    return (
        f"URL: {pg.url}\nTITLE: {title}\n\nTEXT:\n{text}\n\n"
        "CLICKABLE/INPUT ELEMENTS:\n" + "\n".join(elements)
    )


@tool(
    "browser_open",
    "Open a URL in a real headless Chrome browser (runs JavaScript). Returns page text "
    "and a list of clickable elements. Use this to actually visit and interact with sites.",
    {"url": {"type": "string"}},
    ["url"],
)
async def browser_open(ctx: ToolContext, url: str) -> str:
    await check_url_safe(url)
    async with _browser_lock:
        pg = await _get_page()
        await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        return await _snapshot(pg)


@tool(
    "browser_click",
    "Click something on the current page, by its visible text or a CSS selector.",
    {"target": {"type": "string", "description": "Visible text or CSS selector."}},
    ["target"],
)
async def browser_click(ctx: ToolContext, target: str) -> str:
    async with _browser_lock:
        pg = await _get_page()
        try:
            await pg.get_by_text(target, exact=False).first.click(timeout=4000)
        except Exception:
            await pg.locator(target).first.click(timeout=4000)
        await asyncio.sleep(1.2)
        return await _snapshot(pg)


@tool(
    "browser_type",
    "Type into an input on the current page (e.g. a search box).",
    {
        "selector": {"type": "string", "description": "CSS selector, e.g. input[name=q]"},
        "text": {"type": "string"},
        "press_enter": {"type": "boolean"},
    },
    ["selector", "text"],
)
async def browser_type(
    ctx: ToolContext, selector: str, text: str, press_enter: bool = False
) -> str:
    async with _browser_lock:
        pg = await _get_page()
        loc = pg.locator(selector).first
        await loc.fill(text, timeout=5000)
        if press_enter:
            await loc.press("Enter")
            await asyncio.sleep(1.8)
        return await _snapshot(pg)


@tool(
    "browser_scroll",
    "Scroll the current browser page up or down.",
    {"direction": {"type": "string", "enum": ["down", "up"]}},
)
async def browser_scroll(ctx: ToolContext, direction: str = "down") -> str:
    async with _browser_lock:
        pg = await _get_page()
        delta = 700 if direction == "down" else -700
        await pg.mouse.wheel(0, delta)
        await asyncio.sleep(0.5)
        return await _snapshot(pg)


@tool("browser_back", "Navigate back to the previous page in the browser.")
async def browser_back(ctx: ToolContext) -> str:
    async with _browser_lock:
        pg = await _get_page()
        await pg.go_back(wait_until="domcontentloaded", timeout=15000)
        return await _snapshot(pg)


@tool(
    "browser_screenshot",
    "Take a screenshot of the current browser page and send it to the user in Telegram.",
)
async def browser_screenshot(ctx: ToolContext) -> str:
    async with _browser_lock:
        pg = await _get_page()
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            await pg.screenshot(path=path)
            with open(path, "rb") as f:
                await ctx.bot.send_photo(ctx.chat_id, f, caption=pg.url[:200])
        finally:
            if os.path.exists(path):
                os.unlink(path)
        return "Screenshot sent to the user."

"""Web search, price lookup, and safe page reading with SSRF protection."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import re
import socket
import time
from urllib.parse import urlparse

import httpx

from tools.base import ToolContext, tool

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

_WEB_CACHE: dict[tuple[str, str, int], tuple[float, str]] = {}
_WEB_CACHE_MAX = 80


async def check_url_safe(url: str) -> None:
    """Block localhost and private/reserved network targets (SSRF protection)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only http(s) URLs are allowed.")
    infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("Refusing to open a private/local network address.")


async def safe_get(url: str, *, headers: dict | None = None, timeout: float = 20.0) -> httpx.Response:
    """Fetch a URL while validating every redirect destination against SSRF."""
    current = url
    async with httpx.AsyncClient(headers=headers, follow_redirects=False, timeout=timeout) as client:
        for _ in range(6):
            await check_url_safe(current)
            response = await client.get(current)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("location")
            if not location:
                return response
            current = str(response.url.join(location))
    raise ValueError("Too many redirects.")


def _cache_get(kind: str, query: str, count: int, ttl: int) -> str | None:
    cached = _WEB_CACHE.get((kind, query.casefold().strip(), count))
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    return None


def _cache_put(kind: str, query: str, count: int, value: str) -> str:
    if len(_WEB_CACHE) >= _WEB_CACHE_MAX:
        _WEB_CACHE.pop(next(iter(_WEB_CACHE)))
    _WEB_CACHE[(kind, query.casefold().strip(), count)] = (time.monotonic(), value)
    return value


def _ddgs_text(query: str, max_results: int) -> list[dict]:
    """Run one DDGS metasearch with a hard wall-clock timeout."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    def search() -> list[dict]:
        try:
            return list(DDGS(timeout=7).text(query, max_results=max_results, backend="auto"))
        except Exception:
            return []

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(search)
    try:
        return future.result(timeout=8.0)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return []
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


@tool(
    "web_search",
    "Search the web. Use for anything current, factual, or that you are unsure about.",
    {
        "query": {"type": "string"},
        "max_results": {"type": "integer", "description": "1 to 5; default 5."},
    },
    ["query"],
)
async def web_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    max_results = max(1, min(int(max_results), 5))
    cached = _cache_get("web", query, max_results, ttl=300)
    if cached is not None:
        return cached
    results = await asyncio.to_thread(_ddgs_text, query, max_results)
    if not results:
        return "No results."
    formatted = []
    for result in results[:max_results]:
        title = re.sub(r"\s+", " ", str(result.get("title") or "Untitled result")).strip()
        url = str(result.get("href") or "").strip()
        snippet = re.sub(r"\s+", " ", str(result.get("body") or "")).strip()
        formatted.append(f"{title[:180]}\n{url}\n{snippet[:420]}")
    return _cache_put("web", query, max_results, "\n\n".join(formatted))


@tool(
    "price_lookup",
    "Fast product-price lookup. Use first whenever the user asks what something costs, "
    "where it is cheapest, or wants a current price. Returns store/result snippets and links; "
    "do not open retailer pages unless the user asks for deeper verification.",
    {
        "product": {
            "type": "string",
            "description": "Exact product, model, and market/country if the user gave one.",
        },
    },
    ["product"],
)
async def price_lookup(ctx: ToolContext, product: str) -> str:
    product = product.strip()
    if not product:
        return "Please specify a product to look up."
    cached = _cache_get("price", product, 4, ttl=90)
    if cached is not None:
        return cached
    results = await asyncio.to_thread(_ddgs_text, f"{product} price buy", 4)
    if not results:
        return "No current price results found."
    lines = []
    for result in results:
        title = result.get("title", "Untitled result")
        url = result.get("href", "")
        snippet = re.sub(r"\s+", " ", result.get("body", "")).strip()
        lines.append(f"{title}\n{snippet[:280]}\n{url}")
    return _cache_put("price", product, 4, "\n\n".join(lines))


@tool(
    "read_page",
    "Fast fetch of a web page's readable text (no JavaScript). "
    "Use browser_open instead for JS-heavy sites or anything interactive.",
    {"url": {"type": "string"}},
    ["url"],
)
async def read_page(ctx: ToolContext, url: str) -> str:
    r = await safe_get(url, headers=HEADERS)
    ctype = r.headers.get("content-type", "")
    if not any(k in ctype for k in ("html", "text", "json", "xml")):
        return f"Not a text page ({ctype})."
    text = None
    try:
        import trafilatura

        text = trafilatura.extract(r.text)
    except Exception:
        pass
    if not text:
        stripped = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", r.text)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", stripped))
    return f"{r.url}\n\n{text[:5500]}"

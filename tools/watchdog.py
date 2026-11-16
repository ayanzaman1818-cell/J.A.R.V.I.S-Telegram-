"""Automated web price-drop and stock watchdog tools backed by SQLite."""

from __future__ import annotations

import logging
import re
from typing import Any

from database.repository import db
from tools.base import ToolContext, tool
from tools.web import safe_get

log = logging.getLogger("jarvis.tools.watchdog")


def extract_price_from_html(text: str) -> float | None:
    """Extract a numeric product price from HTML meta tags, JSON-LD, or visible currency strings."""
    if not text:
        return None

    # 1. Check JSON-LD or meta price attributes first
    meta_patterns = [
        r'property=["\'](?:product|og):price:amount["\'][^>]*content=["\']([0-9]+(?:\.[0-9]{1,2})?)["\']',
        r'content=["\']([0-9]+(?:\.[0-9]{1,2})?)["\'][^>]*property=["\'](?:product|og):price:amount["\']',
        r'itemprop=["\']price["\'][^>]*content=["\']([0-9]+(?:\.[0-9]{1,2})?)["\']',
        r'"(?:lowPrice|price)"\s*:\s*["\']?([0-9]+(?:\.[0-9]{1,2})?)["\']?',
    ]
    for pat in meta_patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    return val
            except ValueError:
                continue

    # 2. Check visible currency symbols ($199.99, BDT 1,450, ৳1200, £49.99, €79.00)
    curr_match = re.search(
        r"(?:USD|BDT|EUR|GBP|\$|৳|£|€)\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)",
        text,
        flags=re.IGNORECASE,
    )
    if curr_match:
        try:
            val = float(curr_match.group(1).replace(",", ""))
            if val > 0:
                return val
        except ValueError:
            pass
    return None


async def fetch_current_price(url: str) -> float | None:
    try:
        html = await safe_get(url)
        return extract_price_from_html(html)
    except Exception:
        log.debug("Could not fetch price from %s", url, exc_info=True)
        return None


@tool(
    "watch_price",
    "Track a product URL and alert the user in Telegram when its price drops to or below target_price.",
    {
        "url": {"type": "string", "description": "Direct product page URL (https://...)."},
        "product_name": {"type": "string", "description": "Short name of the product."},
        "target_price": {
            "type": "number",
            "description": "Alert threshold price (must be > 0).",
        },
        "currency": {"type": "string", "description": "Currency label (default 'USD')."},
    },
    ["url", "product_name", "target_price"],
)
async def watch_price(
    ctx: ToolContext,
    url: str,
    product_name: str,
    target_price: float,
    currency: str = "USD",
) -> str:
    if target_price <= 0:
        return "Target price must be greater than 0."
    current = await fetch_current_price(url.strip())
    curr = (currency or "USD").strip().upper()
    wid = db.add_price_watch(
        chat_id=ctx.chat_id,
        url=url,
        product_name=product_name,
        target_price=target_price,
        currency=curr,
        last_price=current,
        user_id=ctx.user_id,
    )
    now_str = f" (current detected price: {current:.2f} {curr})" if current is not None else ""
    return (
        f"Price watch #{wid} created for '{product_name.strip()}'. "
        f"Target: <= {target_price:.2f} {curr}{now_str}."
    )


@tool(
    "list_price_watches",
    "List all active product price watches.",
)
async def list_price_watches(ctx: ToolContext) -> str:
    items = db.list_price_watches(active_only=True, user_id=ctx.user_id)
    if not items:
        return "No active price watches."
    lines = ["Active Price Watches:"]
    for w in items:
        last_s = (
            f"last: {w['last_price']:.2f} {w['currency']}"
            if w.get("last_price") is not None
            else "not checked yet"
        )
        lines.append(
            f"#{w['id']} {w['product_name']} - target <= {w['target_price']:.2f} {w['currency']} ({last_s})\n  {w['url']}"
        )
    return "\n".join(lines)


@tool(
    "cancel_price_watch",
    "Cancel an active price watch by its ID.",
    {"watch_id": {"type": "integer"}},
    ["watch_id"],
)
async def cancel_price_watch(ctx: ToolContext, watch_id: int) -> str:
    if db.cancel_price_watch(watch_id, user_id=ctx.user_id):
        return f"Cancelled price watch #{watch_id}."
    return f"No active price watch found with ID #{watch_id}."


@tool(
    "check_price_watches",
    "Immediately check all active product price watches for the user and report any price drops.",
)
async def check_price_watches(ctx: ToolContext) -> str:
    items = db.list_price_watches(active_only=True, user_id=ctx.user_id)
    if not items:
        return "No active price watches to check."
    lines = ["Price Watch Check:"]
    for w in items:
        price = await fetch_current_price(w["url"])
        if price is None:
            db.update_price_watch(w["id"], w.get("last_price"), active=True)
            lines.append(f"- #{w['id']} {w['product_name']}: price could not be read right now.")
            continue
        hit = price <= float(w["target_price"])
        db.update_price_watch(w["id"], price, active=not hit)
        if hit:
            lines.append(
                f"🎯 DEAL HIT #{w['id']} {w['product_name']}: {price:.2f} {w['currency']} "
                f"(target <= {w['target_price']:.2f} {w['currency']}) -> {w['url']}"
            )
        else:
            lines.append(
                f"- #{w['id']} {w['product_name']}: {price:.2f} {w['currency']} "
                f"(target <= {w['target_price']:.2f} {w['currency']})"
            )
    return "\n".join(lines)


async def evaluate_all_price_watches(bot: Any) -> int:
    """Background task helper: check all active watches across users and send alerts on target hits."""
    items = db.list_price_watches(active_only=True)
    alerts_sent = 0
    for w in items:
        price = await fetch_current_price(w["url"])
        if price is None:
            continue
        target = float(w["target_price"])
        if price <= target:
            db.update_price_watch(w["id"], price, active=False)
            if bot:
                try:
                    await bot.send_message(
                        w["chat_id"],
                        f"🎯 Price Drop Alert: '{w['product_name']}' is now {price:.2f} {w['currency']} "
                        f"(target <= {target:.2f} {w['currency']})!\n{w['url']}",
                    )
                    alerts_sent += 1
                except Exception:
                    log.exception("Failed sending price watch alert #%s", w["id"])
        else:
            db.update_price_watch(w["id"], price, active=True)
    return alerts_sent

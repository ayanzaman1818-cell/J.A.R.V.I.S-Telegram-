"""Visual PNG chart generator (dark-mode expense breakdowns and habit streak heatmaps) for Telegram."""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import zlib
from typing import Any

from database.repository import db
from tools.base import ToolContext, tool

# 5x7 bitmap font for crisp pure-Python rendering (no Pillow/Matplotlib required)
_FONT_5X7: dict[str, list[str]] = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    ",": ["00000", "00000", "00000", "00000", "00110", "00100", "01000"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "/": ["00001", "00010", "00100", "00100", "01000", "10000", "00000"],
    "%": ["11001", "11010", "00100", "00100", "01011", "10011", "00000"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    "$": ["00100", "01111", "10100", "01110", "00101", "11110", "00100"],
    "#": ["01010", "11111", "01010", "01010", "11111", "01010", "00000"],
    "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}

_PALETTE: list[tuple[int, int, int]] = [
    (56, 189, 248),   # Sky blue
    (52, 211, 153),   # Emerald
    (251, 191, 36),   # Amber
    (244, 114, 182),  # Pink
    (167, 139, 250),  # Violet
    (251, 113, 133),  # Rose
    (45, 212, 191),   # Teal
]


class _Canvas:
    """Fast pure-Python RGB canvas with PNG encoding."""

    def __init__(self, width: int, height: int, bg: tuple[int, int, int] = (15, 23, 42)) -> None:
        self.width = width
        self.height = height
        row_bytes = bytes(bg) * width
        self.buf = bytearray(row_bytes * height)

    def fill_rect(
        self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]
    ) -> None:
        x0 = max(0, int(x))
        y0 = max(0, int(y))
        x1 = min(self.width, int(x + w))
        y1 = min(self.height, int(y + h))
        if x1 <= x0 or y1 <= y0:
            return
        span_len = x1 - x0
        row_patch = bytes(color) * span_len
        stride = self.width * 3
        for py in range(y0, y1):
            start = py * stride + x0 * 3
            self.buf[start : start + span_len * 3] = row_patch

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        color: tuple[int, int, int] = (241, 245, 249),
        scale: int = 2,
    ) -> None:
        cx = int(x)
        for ch in text.upper():
            glyph = _FONT_5X7.get(ch, _FONT_5X7[" "])
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        self.fill_rect(
                            cx + gx * scale,
                            y + gy * scale,
                            scale,
                            scale,
                            color,
                        )
            cx += 6 * scale

    def save_png(self, path: str) -> None:
        stride = self.width * 3
        raw = bytearray()
        for y in range(self.height):
            raw.append(0)  # Filter type 0 (None)
            start = y * stride
            raw.extend(self.buf[start : start + stride])

        def _chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        compressed = zlib.compress(bytes(raw), level=6)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", compressed)
            + _chunk(b"IEND", b"")
        )
        with open(path, "wb") as f:
            f.write(png_bytes)


def render_expense_chart(
    days: int = 30,
    user_id: int | None = None,
    summary_data: dict[str, Any] | None = None,
    daily_series: list[dict[str, Any]] | None = None,
) -> str:
    """Render a dark-mode PNG chart showing category breakdown and daily spending bars."""
    summary = summary_data or db.expense_summary(days=days, user_id=user_id)
    daily = daily_series or db.daily_expense_series(days=min(days, 14), user_id=user_id)

    width, height = 760, 480
    c = _Canvas(width, height, bg=(15, 23, 42))

    # Header card
    c.fill_rect(20, 20, width - 40, 64, (30, 41, 59))
    c.draw_text(38, 34, f"JARVIS EXPENSE REPORT ({days}D)", (56, 189, 248), scale=2)

    totals = summary.get("totals") or []
    total_str = (
        " + ".join(f"{t['total']:.0f} {t['currency']}" for t in totals)
        if totals
        else "0 BDT"
    )
    c.draw_text(38, 58, f"TOTAL: {total_str}", (226, 232, 240), scale=2)

    # Category panel
    c.fill_rect(20, 98, width - 40, 200, (30, 41, 59))
    c.draw_text(38, 112, "BY CATEGORY", (148, 163, 184), scale=2)

    by_cat = summary.get("by_category") or []
    if not by_cat:
        c.draw_text(38, 160, "NO EXPENSES LOGGED IN THIS WINDOW", (100, 116, 139), scale=2)
    else:
        max_cat_val = max(float(item["total"]) for item in by_cat) or 1.0
        grand_total = sum(float(item["total"]) for item in by_cat) or 1.0
        for idx, item in enumerate(by_cat[:5]):
            y = 140 + idx * 30
            cat_name = str(item["category"])[:12]
            val = float(item["total"])
            curr = str(item["currency"])
            pct = (val / grand_total) * 100.0
            color = _PALETTE[idx % len(_PALETTE)]

            c.draw_text(38, y + 3, cat_name, (241, 245, 249), scale=2)
            bar_x = 200
            bar_max_w = 310
            c.fill_rect(bar_x, y, bar_max_w, 18, (51, 65, 85))
            fill_w = max(6, int((val / max_cat_val) * bar_max_w))
            c.fill_rect(bar_x, y, fill_w, 18, color)
            label = f"{val:.0f} {curr} ({pct:.0f}%)"
            c.draw_text(bar_x + bar_max_w + 14, y + 3, label[:18], (203, 213, 225), scale=2)

    # Daily spending bar chart panel
    c.fill_rect(20, 312, width - 40, 148, (30, 41, 59))
    c.draw_text(38, 324, "RECENT DAILY TREND", (148, 163, 184), scale=2)

    if daily:
        max_day = max((float(d["total"]) for d in daily), default=0.0) or 1.0
        n_bars = len(daily)
        chart_x = 42
        chart_w = width - 84
        slot_w = max(20, chart_w // max(1, n_bars))
        bar_w = max(12, slot_w - 10)
        base_y = 425
        max_bar_h = 72
        for i, d in enumerate(daily):
            val = float(d["total"])
            bx = chart_x + i * slot_w
            c.fill_rect(bx, base_y - max_bar_h, bar_w, max_bar_h, (39, 52, 73))
            if val > 0:
                bh = max(4, int((val / max_day) * max_bar_h))
                c.fill_rect(bx, base_y - bh, bar_w, bh, (56, 189, 248))
            day_lbl = str(d["date"])[-2:]
            c.draw_text(bx + 2, base_y + 8, day_lbl, (148, 163, 184), scale=1)

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    c.save_png(path)
    return path


def render_habit_chart(
    days: int = 14,
    user_id: int | None = None,
    matrix_data: dict[str, Any] | None = None,
) -> str:
    """Render a dark-mode PNG habit consistency grid and streak dashboard."""
    matrix = matrix_data or db.daily_habit_matrix(days=days, user_id=user_id)
    dates: list[str] = matrix.get("dates") or []
    habits: list[dict[str, Any]] = matrix.get("habits") or []

    width, height = 760, 420
    c = _Canvas(width, height, bg=(15, 23, 42))

    # Header card
    c.fill_rect(20, 20, width - 40, 60, (30, 41, 59))
    c.draw_text(38, 34, f"JARVIS HABIT STREAKS ({len(dates)} DAYS)", (52, 211, 153), scale=2)
    c.draw_text(38, 56, f"ACTIVE HABITS: {len(habits)}", (203, 213, 225), scale=2)

    # Grid panel
    c.fill_rect(20, 94, width - 40, height - 114, (30, 41, 59))

    if not habits:
        c.draw_text(38, 180, "NO HABITS LOGGED YET", (100, 116, 139), scale=2)
    else:
        grid_x = 220
        cell_size = 22
        cell_gap = 6
        # Date headers (day numbers)
        for di, d_str in enumerate(dates[:14]):
            cx = grid_x + di * (cell_size + cell_gap)
            c.draw_text(cx + 4, 112, d_str[-2:], (148, 163, 184), scale=1)

        c.draw_text(620, 110, "STREAK", (148, 163, 184), scale=2)

        for hi, h in enumerate(habits[:6]):
            row_y = 136 + hi * 42
            name = str(h["habit"])[:13]
            color = _PALETTE[hi % len(_PALETTE)]
            c.draw_text(38, row_y + 4, name, (241, 245, 249), scale=2)

            day_map = h.get("days") or {}
            for di, d_str in enumerate(dates[:14]):
                cx = grid_x + di * (cell_size + cell_gap)
                done = float(day_map.get(d_str, 0.0)) > 0
                box_color = color if done else (51, 65, 85)
                c.fill_rect(cx, row_y, cell_size, cell_size, box_color)

            streak = int(h.get("streak", 0))
            c.draw_text(620, row_y + 4, f"{streak}D", (251, 191, 36), scale=2)

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    c.save_png(path)
    return path


@tool(
    "expense_chart",
    "Generate a visual PNG chart of recent expenses by category and daily trend, and send it to Telegram.",
    {
        "days": {
            "type": "integer",
            "description": "Number of days to include in the expense chart (default 30).",
        }
    },
)
async def expense_chart(ctx: ToolContext, days: int = 30) -> str:
    n_days = max(1, min(int(days), 365))
    path = await asyncio.to_thread(render_expense_chart, n_days, ctx.user_id)
    try:
        if ctx.bot:
            with open(path, "rb") as f:
                await ctx.bot.send_photo(
                    ctx.chat_id,
                    f,
                    caption=f"📊 Expense Analytics ({n_days}-day breakdown)",
                )
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return f"Sent {n_days}-day expense chart to Telegram."


@tool(
    "habit_chart",
    "Generate a visual PNG habit streak heatmap for recent days and send it to Telegram.",
    {
        "days": {
            "type": "integer",
            "description": "Number of days to display on the habit grid (default 14).",
        }
    },
)
async def habit_chart(ctx: ToolContext, days: int = 14) -> str:
    n_days = max(7, min(int(days), 30))
    path = await asyncio.to_thread(render_habit_chart, n_days, ctx.user_id)
    try:
        if ctx.bot:
            with open(path, "rb") as f:
                await ctx.bot.send_photo(
                    ctx.chat_id,
                    f,
                    caption=f"🔥 Habit Streaks & Consistency ({n_days} days)",
                )
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return f"Sent {n_days}-day habit streak chart to Telegram."

"""Modular PySide6 custom vector widgets for The J.A.R.V.I.S Notch.

Includes (100% hand-drawn QPainter vector icons, zero Windows emojis, pitch-black borderless aesthetic):
- WeatherBadgeWidget: Hand-drawn weather icon + live temperature pill
- NotchToolsPage: 4-tab Utility Hub (Drag-and-Drop File Shelf + Ask JARVIS, Clipboard History, Live Squircle Webcam Mirror, Quick Pomodoro Timer)
- NotchLyricsPage: Synced live lyrics view (lrclib.net) + Focus Player
- NotchToastPage: Live notification preview pill with 1-click Reply / Dismiss
- Idle HUD & status painters: Volume/Brightness HUD, Battery/Charging pill, Downloads ring/checkmark, Mic/Cam privacy dots
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Callable, Optional


def build_notch_extra_widgets(make_squircle_fn: Callable[..., Any]) -> dict[str, Any]:
    """Build and return PySide6 widget classes & idle painters sharing the overlay's squircle helper."""
    from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QPointF, QRect, QRectF, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QCursor,
        QDrag,
        QFont,
        QFontMetricsF,
        QGuiApplication,
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
        QPolygonF,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QScrollArea,
        QStackedWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    # ------------------------------------------------------------------
    # 1. Idle Notch Vector Painters (HUD, Battery, Downloads, Privacy)
    # ------------------------------------------------------------------

    def draw_hud_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        kind: str,
        percent: int,
        muted: bool = False,
    ) -> None:
        """Draw sleek live Volume or Brightness HUD inside the idle notch (196x26)."""
        cy = nh / 2.0
        ix = left + 16.0

        # Left custom vector icon (speaker or sun)
        if kind == "brightness":
            col = QColor("#fde047")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(ix, cy), 3.1, 3.1)
            p.setPen(QPen(col, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for k in range(8):
                ang = k * (math.pi / 4.0)
                p.drawLine(
                    QPointF(ix + math.cos(ang) * 4.5, cy + math.sin(ang) * 4.5),
                    QPointF(ix + math.cos(ang) * 6.4, cy + math.sin(ang) * 6.4),
                )
        else:
            col = QColor("#f87171") if muted else QColor("#f8fafc")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            spk = QPainterPath()
            spk.moveTo(ix - 5.0, cy - 2.1)
            spk.lineTo(ix - 2.5, cy - 2.1)
            spk.lineTo(ix + 0.6, cy - 4.7)
            spk.lineTo(ix + 0.6, cy + 4.7)
            spk.lineTo(ix - 2.5, cy + 2.1)
            spk.lineTo(ix - 5.0, cy + 2.1)
            spk.closeSubpath()
            p.drawPath(spk)
            if muted:
                p.setPen(QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(ix + 2.2, cy - 2.2), QPointF(ix + 5.6, cy + 2.2))
                p.drawLine(QPointF(ix + 5.6, cy - 2.2), QPointF(ix + 2.2, cy + 2.2))
            else:
                p.setPen(QPen(col, 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                if percent > 0:
                    p.drawArc(QRectF(ix - 0.6, cy - 2.8, 4.2, 5.6), -50 * 16, 100 * 16)
                if percent > 45:
                    p.drawArc(QRectF(ix + 0.7, cy - 4.4, 5.4, 8.8), -50 * 16, 100 * 16)

        # Center sleek level bar
        bar_l = left + 30.0
        bar_r = right - 38.0
        bar_w = max(24.0, bar_r - bar_l)
        bar_h = 4.6
        bar_y = cy - bar_h / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 36))
        p.drawRoundedRect(QRectF(bar_l, bar_y, bar_w, bar_h), 2.3, 2.3)

        ratio = 0.0 if muted else max(0.0, min(1.0, percent / 100.0))
        fill_w = bar_w * ratio
        if fill_w > 1.5:
            fg = QLinearGradient(bar_l, bar_y, bar_l + bar_w, bar_y)
            if kind == "brightness":
                fg.setColorAt(0.0, QColor("#fde047"))
                fg.setColorAt(1.0, QColor("#fb923c"))
            else:
                fg.setColorAt(0.0, QColor("#60a5fa"))
                fg.setColorAt(1.0, QColor("#ffffff"))
            p.setBrush(QBrush(fg))
            p.drawRoundedRect(QRectF(bar_l, bar_y, fill_w, bar_h), 2.3, 2.3)

        # Right percentage text
        p.setPen(QColor("#f87171") if muted else QColor("#f1f5f9"))
        p.setFont(QFont("SF Pro Display", 8, QFont.Weight.Bold))
        txt = "Mute" if muted else f"{percent}%"
        p.drawText(QRectF(bar_r + 4.0, 0.0, right - bar_r - 11.0, nh), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)

    def draw_battery_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        percent: int,
        charging: bool,
    ) -> None:
        """Draw sleek Charging Pulse or Low-Battery micro-pill in the idle notch."""
        cy = nh / 2.0
        accent = QColor("#30d158") if charging else (QColor("#ff453a") if percent <= 12 else QColor("#ff9f0a"))
        # Left vector battery silhouette
        bx = left + 12.0
        by = cy - 4.5
        bw, bh = 16.0, 9.0
        p.setPen(QPen(accent, 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(bx, by, bw, bh), 2.2, 2.2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(bx + bw + 1.0, cy - 2.0, 1.8, 4.0), 0.9, 0.9)
        fill_w = max(1.5, (bw - 3.6) * max(0.0, min(1.0, percent / 100.0)))
        p.drawRoundedRect(QRectF(bx + 1.8, by + 1.8, fill_w, bh - 3.6), 1.0, 1.0)

        # Label & percentage
        p.setPen(accent)
        p.setFont(QFont("SF Pro Display", 8, QFont.Weight.Bold))
        label = "Charging" if charging else "Low Battery"
        p.drawText(QRectF(bx + bw + 8.0, 0.0, nw - 70.0, nh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        p.drawText(QRectF(right - 42.0, 0.0, 32.0, nh), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{percent}%")

    def draw_download_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        state: str,
        filename: str,
        detail: str,
        spin_phase: float,
    ) -> None:
        """Draw animated 60fps Download Progress Ring or Emerald Checkmark inside the idle notch."""
        cy = nh / 2.0
        ix = left + 15.0
        r = 5.4
        if state == "done":
            col = QColor("#30d158")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(48, 209, 88, 42))
            p.drawEllipse(QPointF(ix, cy), r + 1.2, r + 1.2)
            p.setPen(QPen(col, 1.75, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            chk = QPainterPath()
            chk.moveTo(ix - 3.0, cy + 0.2)
            chk.lineTo(ix - 0.7, cy + 2.6)
            chk.lineTo(ix + 3.4, cy - 2.3)
            p.drawPath(chk)
        else:
            col = QColor("#38bdf8")
            ring_r = QRectF(ix - r, cy - r, r * 2.0, r * 2.0)
            p.setPen(QPen(QColor(56, 189, 248, 55), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ring_r)
            start_deg = int((-spin_phase * 220.0) % 360.0)
            p.setPen(QPen(col, 1.85, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(ring_r, start_deg * 16, 130 * 16)
            # Down arrow in center
            p.setPen(QPen(col, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(ix, cy - 2.4), QPointF(ix, cy + 2.2))
            p.drawLine(QPointF(ix - 1.9, cy + 0.4), QPointF(ix, cy + 2.3))
            p.drawLine(QPointF(ix + 1.9, cy + 0.4), QPointF(ix, cy + 2.3))

        # Filename & right detail
        f_name = QFont("SF Pro Display", 8, QFont.Weight.DemiBold)
        p.setFont(f_name)
        fm = QFontMetricsF(f_name)
        detail_w = fm.horizontalAdvance(detail) + 12.0
        name_l = left + 27.0
        name_w = max(24.0, (right - detail_w - 10.0) - name_l)
        elided = fm.elidedText(filename, Qt.TextElideMode.ElideMiddle, name_w)
        p.setPen(QColor("#f1f5f9"))
        p.drawText(QRectF(name_l, 0.0, name_w, nh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        p.setPen(QColor("#30d158") if state == "done" else QColor("#38bdf8"))
        p.setFont(QFont("SF Pro Display", 7, QFont.Weight.Bold))
        p.drawText(QRectF(right - detail_w - 8.0, 0.0, detail_w, nh), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, detail)

    def draw_privacy_dots(
        p: QPainter,
        cx: float,
        cy: float,
        mic_active: bool,
        cam_active: bool,
        with_labels: bool = False,
    ) -> None:
        """Draw crisp iOS/macOS orange (mic) and green (camera) privacy indicator dots (optionally with MIC/CAM labels)."""
        items: list[tuple[QColor, str]] = []
        if mic_active:
            items.append((QColor("#ff9f0a"), "MIC"))
        if cam_active:
            items.append((QColor("#30d158"), "CAM"))
        if not items:
            return
        if with_labels:
            item_w = 42.0
            total_w = len(items) * item_w
            start_x = cx - total_w / 2.0
            p.setFont(QFont("SF Pro Display", 7, QFont.Weight.ExtraBold))
            for i, (col, lbl) in enumerate(items):
                bx = start_x + i * item_w
                dot_x = bx + 9.0
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 95))
                p.drawEllipse(QPointF(dot_x, cy), 4.8, 4.8)
                p.setBrush(col)
                p.drawEllipse(QPointF(dot_x, cy), 2.8, 2.8)
                p.setPen(col)
                p.drawText(QRectF(dot_x + 6.0, cy - 7.0, 28.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)
            return

        spacing = 10.0
        start_x = cx - ((len(items) - 1) * spacing) / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        for i, (col, _lbl) in enumerate(items):
            dx = start_x + i * spacing
            p.setBrush(QColor(col.red(), col.green(), col.blue(), 95))
            p.drawEllipse(QPointF(dx, cy), 4.8, 4.8)
            p.setBrush(col)
            p.drawEllipse(QPointF(dx, cy), 2.9, 2.9)

    def draw_bluetooth_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        device_name: str,
        connected: bool,
        battery_pct: int = -1,
    ) -> None:
        """Draw animated Bluetooth connect/disconnect pill with vector BT rune & optional battery %."""
        cy = nh / 2.0
        ix = left + 15.0
        accent = QColor("#38bdf8") if connected else QColor("#94a3b8")
        p.setPen(QPen(accent, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        bt = QPainterPath()
        bt.moveTo(ix - 2.6, cy - 2.4)
        bt.lineTo(ix + 2.6, cy + 2.4)
        bt.lineTo(ix, cy + 4.8)
        bt.lineTo(ix, cy - 4.8)
        bt.lineTo(ix + 2.6, cy - 2.4)
        bt.lineTo(ix - 2.6, cy + 2.4)
        p.drawPath(bt)

        status_txt = f"{battery_pct}%" if (connected and 0 <= battery_pct <= 100) else ("Connected" if connected else "Disconnected")
        f_st = QFont("SF Pro Display", 7, QFont.Weight.Bold)
        p.setFont(f_st)
        st_w = QFontMetricsF(f_st).horizontalAdvance(status_txt) + 12.0

        f_nm = QFont("SF Pro Display", 8, QFont.Weight.DemiBold)
        p.setFont(f_nm)
        nm_l = left + 26.0
        nm_w = max(24.0, (right - st_w - 8.0) - nm_l)
        elided = QFontMetricsF(f_nm).elidedText(device_name or "Bluetooth", Qt.TextElideMode.ElideRight, nm_w)
        p.setPen(QColor("#f8fafc"))
        p.drawText(QRectF(nm_l, 0.0, nm_w, nh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        p.setFont(f_st)
        p.setPen(QColor("#30d158") if (connected and battery_pct > 20) else (QColor("#ff9f0a") if connected and 0 <= battery_pct <= 20 else accent))
        p.drawText(QRectF(right - st_w - 8.0, 0.0, st_w, nh), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, status_txt)

    def draw_network_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        online: bool,
    ) -> None:
        """Draw sleek Wi-Fi / Internet drop (amber) or reconnect (emerald) pill."""
        cy = nh / 2.0
        ix = left + 16.0
        col = QColor("#30d158") if online else QColor("#ff9f0a")
        p.setPen(QPen(col, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(ix - 5.5, cy - 3.6, 11.0, 11.0), 40 * 16, 100 * 16)
        p.drawArc(QRectF(ix - 3.4, cy - 1.5, 6.8, 6.8), 40 * 16, 100 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(ix, cy + 3.2), 1.3, 1.3)
        if not online:
            p.setPen(QPen(QColor("#ff453a"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(ix - 5.0, cy + 4.2), QPointF(ix + 5.0, cy - 4.2))

        p.setPen(col)
        p.setFont(QFont("SF Pro Display", 8, QFont.Weight.Bold))
        txt = "Wi-Fi Reconnected" if online else "No Internet"
        p.drawText(QRectF(left + 28.0, 0.0, nw - 38.0, nh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)

    def draw_lockkey_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        key_name: str,
        is_on: bool,
    ) -> None:
        """Draw crisp 1.2s Caps Lock / Num Lock micro-pill."""
        cy = nh / 2.0
        ix = left + 15.0
        col = QColor("#38bdf8") if is_on else QColor("#94a3b8")
        p.setPen(QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        up = QPainterPath()
        up.moveTo(ix, cy - 4.2)
        up.lineTo(ix - 4.0, cy + 0.5)
        up.lineTo(ix - 1.7, cy + 0.5)
        up.lineTo(ix - 1.7, cy + 3.8)
        up.lineTo(ix + 1.7, cy + 3.8)
        up.lineTo(ix + 1.7, cy + 0.5)
        up.lineTo(ix + 4.0, cy + 0.5)
        up.closeSubpath()
        if is_on:
            p.setBrush(QColor(56, 189, 248, 70))
        p.drawPath(up)

        p.setPen(col)
        p.setFont(QFont("SF Pro Display", 8, QFont.Weight.ExtraBold))
        lbl = f"{key_name} {'ON' if is_on else 'OFF'}"
        p.drawText(QRectF(left + 26.0, 0.0, nw - 34.0, nh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)

    def draw_call_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        app_name: str,
        dur_text: str,
        muted: bool,
        phase: float,
        hovered: bool = False,
        mute_hit: bool = False,
        voice_bars: Optional[list[float]] = None,
    ) -> Optional[QRectF]:
        """Draw iPhone Dynamic Island-style Active Call pill:
        Left: Green phone handset icon + live duration | Right: Pulsing green waveform (+ Mute Mic button on hover).
        """
        cy = nh / 2.0
        col = QColor("#ff453a") if muted else QColor("#30d158")

        # 1. Left: Crisp Apple-style vector phone handset icon
        px = left + 15.0
        p.save()
        p.translate(px, cy)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        handset = QPainterPath()
        # Smooth tilted classic phone receiver silhouette (-5..+5)
        handset.moveTo(-4.4, -2.6)
        handset.cubicTo(-4.8, -3.6, -3.6, -4.8, -2.4, -4.4)
        handset.lineTo(-1.1, -3.1)
        handset.cubicTo(-0.7, -2.7, -0.8, -2.0, -1.3, -1.6)
        handset.cubicTo(-0.6, -0.4, 0.4, 0.6, 1.6, 1.3)
        handset.cubicTo(2.0, 0.8, 2.7, 0.7, 3.1, 1.1)
        handset.lineTo(4.4, 2.4)
        handset.cubicTo(4.8, 3.6, 3.6, 4.8, 2.6, 4.4)
        handset.cubicTo(-1.0, 3.3, -3.3, 1.0, -4.4, -2.6)
        handset.closeSubpath()
        p.drawPath(handset)
        p.restore()

        # 2. Left-center: Live call duration in green right beside the phone icon
        p.setFont(QFont("SF Pro Display", 9 if hovered else 8, QFont.Weight.Bold))
        p.setPen(col)
        p.drawText(
            QRectF(left + 25.0, 0.0, 62.0, nh),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            dur_text or "0:00",
        )

        # 3. Right: Exact 6-bar Music Notch visualizer style in green (+ circular Mute Mic button on hover)
        mute_rect: Optional[QRectF] = None
        bars = voice_bars if (voice_bars and len(voice_bars) >= 6) else [0.12] * 6
        bar_w = 2.6
        gap = 2.1
        n_bars = len(bars)
        wave_w = n_bars * bar_w + (n_bars - 1) * gap

        if hovered and nh >= 28:
            btn_sz = 20.0
            btn_x = right - btn_sz - 9.0
            btn_y = (nh - btn_sz) / 2.0
            mute_rect = QRectF(btn_x, btn_y, btn_sz, btn_sz)
            wave_x = btn_x - wave_w - 9.0

            # Circular Mic Mute button
            p.setPen(Qt.PenStyle.NoPen)
            bg_c = QColor(255, 69, 58, 165 if mute_hit else 95) if muted else QColor(255, 255, 255, 68 if mute_hit else 30)
            p.setBrush(bg_c)
            p.drawEllipse(mute_rect)
            mcx, mcy = mute_rect.center().x(), mute_rect.center().y()
            fg_m = QColor("#ffffff") if (muted or mute_hit) else QColor("#cbd5e1")
            p.setPen(QPen(fg_m, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(mcx - 2.0, mcy - 4.4, 4.0, 6.2), 2.0, 2.0)
            p.drawArc(QRectF(mcx - 3.6, mcy - 2.0, 7.2, 6.2), 180 * 16, 180 * 16)
            p.drawLine(QPointF(mcx, mcy + 4.2), QPointF(mcx, mcy + 5.6))
            if muted:
                p.setPen(QPen(QColor("#ffffff"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(mcx - 4.5, mcy + 4.5), QPointF(mcx + 4.5, mcy - 4.5))
        else:
            wave_x = right - wave_w - 10.0

        max_bar_h = max(10.0, nh - 6.0)
        # Dual-voice unified 6-bar visualizer:
        # Bars 0..2 = Green (your voice, or red/flat when muted) | Bars 3..5 = Orange (other person's voice)
        my_grad = QLinearGradient(wave_x, cy - max_bar_h / 2.0, wave_x, cy + max_bar_h / 2.0)
        if muted:
            my_grad.setColorAt(0.0, QColor("#ff453a"))
            my_grad.setColorAt(1.0, QColor("#ff6961"))
            my_glow = QColor(255, 69, 58, 70)
        else:
            my_grad.setColorAt(0.0, QColor("#4ade80"))
            my_grad.setColorAt(0.5, QColor("#30d158"))
            my_grad.setColorAt(1.0, QColor("#22c55e"))
            my_glow = QColor(48, 209, 88, 85)

        other_grad = QLinearGradient(wave_x, cy - max_bar_h / 2.0, wave_x, cy + max_bar_h / 2.0)
        other_grad.setColorAt(0.0, QColor("#ffb340"))
        other_grad.setColorAt(0.5, QColor("#ff9f0a"))
        other_grad.setColorAt(1.0, QColor("#f97316"))
        other_glow = QColor(255, 159, 10, 88)

        p.setPen(Qt.PenStyle.NoPen)
        for i, amp in enumerate(bars):
            is_mine = i < 3
            bx = wave_x + i * (bar_w + gap)
            bh = 2.2 if (is_mine and muted) else max(2.2, min(max_bar_h, float(amp) * max_bar_h))
            by = cy - bh / 2.0
            # Soft blurry color halo + crisp inner bar (identical to Music Notch visualizer!)
            p.setBrush(my_glow if is_mine else other_glow)
            p.drawRoundedRect(QRectF(bx - 1.0, by - 1.0, bar_w + 2.0, bh + 2.0), 1.9, 1.9)
            p.setBrush(QBrush(my_grad if is_mine else other_grad))
            p.drawRoundedRect(QRectF(bx, by, bar_w, bh), 1.3, 1.3)

        return mute_rect

    def draw_color_swatch_pill(
        p: QPainter,
        left: float,
        right: float,
        nw: float,
        nh: float,
        hex_code: str,
    ) -> None:
        """Draw a colored circle swatch + copied #HEX pill after using the Eyedropper."""
        cy = nh / 2.0
        sw_col = QColor(hex_code) if QColor(hex_code).isValid() else QColor("#38bdf8")
        p.setPen(QPen(QColor(255, 255, 255, 110), 1.0))
        p.setBrush(sw_col)
        p.drawEllipse(QPointF(left + 15.0, cy), 5.6, 5.6)

        p.setPen(QColor("#ffffff"))
        p.setFont(QFont("SF Pro Display", 8, QFont.Weight.Bold))
        p.drawText(QRectF(left + 27.0, 0.0, nw - 76.0, nh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, hex_code.upper())
        p.setPen(QColor("#30d158"))
        p.setFont(QFont("SF Pro Display", 7, QFont.Weight.Bold))
        p.drawText(QRectF(right - 48.0, 0.0, 40.0, nh), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "Copied")

    # ------------------------------------------------------------------
    # 2. Custom Vector Weather Badge Widget (for Dashboard Calendar)
    # ------------------------------------------------------------------

    class WeatherBadgeWidget(QWidget):
        """Hand-drawn vector weather icon + live temperature (e.g., sun + 27°) — zero emojis."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self._condition = "clear_day"
            self._label = ""
            self.setFixedSize(46, 20)
            self.hide()

        def set_weather(self, ok: bool, condition: str, label: str) -> None:
            if not ok or not label:
                self.hide()
                return
            self._condition = condition or "clear_day"
            self._label = label
            self.show()
            self.update()

        def paintEvent(self, _event: Any) -> None:
            if not self._label:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            h = float(self.height())
            cy = h / 2.0
            ix = 9.0

            cond = self._condition
            if cond == "clear_day":
                c = QColor("#fbbf24")
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(QPointF(ix, cy), 3.3, 3.3)
                p.setPen(QPen(c, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                for k in range(8):
                    a = k * (math.pi / 4.0)
                    p.drawLine(QPointF(ix + math.cos(a) * 4.7, cy + math.sin(a) * 4.7), QPointF(ix + math.cos(a) * 6.2, cy + math.sin(a) * 6.2))
            elif cond == "clear_night":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#93c5fd"))
                moon = QPainterPath()
                moon.addEllipse(QPointF(ix, cy), 4.8, 4.8)
                cut = QPainterPath()
                cut.addEllipse(QPointF(ix + 2.4, cy - 1.5), 4.1, 4.1)
                p.drawPath(moon.subtracted(cut))
            else:
                # Cloud silhouette
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#cbd5e1") if cond == "cloudy" else QColor("#94a3b8"))
                p.drawEllipse(QPointF(ix - 2.6, cy + 0.6), 3.1, 2.7)
                p.drawEllipse(QPointF(ix + 1.8, cy + 0.9), 2.8, 2.4)
                p.drawEllipse(QPointF(ix - 0.3, cy - 1.4), 3.4, 3.0)
                if cond == "rain":
                    p.setPen(QPen(QColor("#38bdf8"), 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    for dx in (-2.2, 0.4, 3.0):
                        p.drawLine(QPointF(ix + dx, cy + 4.0), QPointF(ix + dx - 0.9, cy + 6.4))
                elif cond == "storm":
                    p.setPen(QPen(QColor("#facc15"), 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    p.drawLine(QPointF(ix + 0.6, cy + 2.2), QPointF(ix - 1.2, cy + 5.0))
                    p.drawLine(QPointF(ix - 1.2, cy + 5.0), QPointF(ix + 1.0, cy + 5.0))
                    p.drawLine(QPointF(ix + 1.0, cy + 5.0), QPointF(ix - 0.6, cy + 7.4))

            p.setPen(QColor("#cbd5e1"))
            p.setFont(QFont("SF Pro Display", 9, QFont.Weight.Bold))
            p.drawText(QRectF(18.0, 0.0, self.width() - 18.0, h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

    # ------------------------------------------------------------------
    # 3. Draggable File Shelf Chip (Drag-Out to Chrome/Discord/Explorer)
    # ------------------------------------------------------------------

    class _ShelfFileChip(QFrame):
        removed = Signal(str)

        def __init__(self, file_path: str, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.file_path = file_path
            self._drag_start: Optional[QPointF] = None
            self.setFixedSize(116, 44)
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
            self.setToolTip(f"Drag out to any app:\n{file_path}")
            self.setStyleSheet(
                "QFrame { background: rgba(255,255,255,0.07); border: none; border-radius: 12px; }"
                "QFrame:hover { background: rgba(255,255,255,0.12); }"
            )
            hl = QHBoxLayout(self)
            hl.setContentsMargins(8, 4, 6, 4)
            hl.setSpacing(6)

            ext = Path(file_path).suffix.upper().lstrip(".")[:4] or "FILE"
            badge = QLabel(ext)
            badge.setFixedSize(28, 28)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                "background: rgba(56,189,248,0.20); color: #7dd3fc; border-radius: 8px; "
                "font-size: 8px; font-weight: 800;"
            )
            hl.addWidget(badge)

            name_lbl = QLabel(Path(file_path).name[:14])
            name_lbl.setStyleSheet("color: #f1f5f9; font-size: 10px; font-weight: 700; background: transparent;")
            hl.addWidget(name_lbl, stretch=1)

            btn_x = QPushButton("×")
            btn_x.setFixedSize(16, 16)
            btn_x.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_x.setStyleSheet(
                "QPushButton { background: transparent; color: #64748b; border: none; font-size: 13px; font-weight: 700; }"
                "QPushButton:hover { color: #f87171; }"
            )
            btn_x.clicked.connect(lambda: self.removed.emit(self.file_path))
            hl.addWidget(btn_x)

        def mousePressEvent(self, event: Any) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_start = event.position()
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event: Any) -> None:
            if self._drag_start is not None and (event.position() - self._drag_start).manhattanLength() > 8:
                self._drag_start = None
                drag = QDrag(self)
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(self.file_path)])
                drag.setMimeData(mime)
                drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
                return
            super().mouseMoveEvent(event)

    class _AskJarvisDropZone(QFrame):
        file_dropped = Signal(str)

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setAcceptDrops(True)
            self.setFixedWidth(164)
            self._hot = False
            self._update_style()
            vl = QVBoxLayout(self)
            vl.setContentsMargins(10, 10, 10, 10)
            vl.setSpacing(4)
            vl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t = QLabel("Ask JARVIS")
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setStyleSheet("color: #38bdf8; font-size: 13px; font-weight: 800; background: transparent;")
            s = QLabel("Drop file or image\nto analyze instantly")
            s.setAlignment(Qt.AlignmentFlag.AlignCenter)
            s.setStyleSheet("color: #94a3b8; font-size: 10px; font-weight: 600; background: transparent;")
            vl.addWidget(t)
            vl.addWidget(s)

        def _update_style(self) -> None:
            if self._hot:
                self.setStyleSheet("QFrame { background: rgba(56,189,248,0.22); border-radius: 16px; border: none; }")
            else:
                self.setStyleSheet("QFrame { background: rgba(56,189,248,0.09); border-radius: 16px; border: none; }")

        def dragEnterEvent(self, event: Any) -> None:
            if event.mimeData().hasUrls():
                self._hot = True
                self._update_style()
                event.acceptProposedAction()

        def dragLeaveEvent(self, event: Any) -> None:
            self._hot = False
            self._update_style()

        def dropEvent(self, event: Any) -> None:
            self._hot = False
            self._update_style()
            urls = event.mimeData().urls()
            if urls:
                local_p = urls[0].toLocalFile()
                if local_p:
                    self.file_dropped.emit(local_p)
                    event.acceptProposedAction()

    # ------------------------------------------------------------------
    # 4. Live Squircle Webcam Mirror Widget (PySide6.QtMultimedia)
    # ------------------------------------------------------------------

    class _SquircleMirrorWidget(QWidget):
        """Horizontally-mirrored squircle live webcam preview using native PySide6.QtMultimedia."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setMinimumSize(320, 140)
            self._frame_img: Optional[QImage] = None
            self._status_msg = "Camera idle"
            self._camera: Any = None
            self._session: Any = None
            self._sink: Any = None
            self._active = False

        def start_camera(self) -> None:
            if self._active:
                return
            try:
                from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink

                cams = QMediaDevices.videoInputs()
                if not cams:
                    self._status_msg = "No webcam detected"
                    self.update()
                    return
                self._camera = QCamera(cams[0], self)
                self._sink = QVideoSink(self)
                self._sink.videoFrameChanged.connect(self._on_frame)
                self._session = QMediaCaptureSession(self)
                self._session.setCamera(self._camera)
                self._session.setVideoSink(self._sink)
                self._camera.start()
                self._active = True
                self._status_msg = "Starting camera…"
                self.update()
            except Exception as exc:
                self._status_msg = f"Camera error"
                self.update()

        def stop_camera(self) -> None:
            if self._camera is not None:
                try:
                    self._camera.stop()
                except Exception:
                    pass
                self._camera = None
            self._session = None
            self._sink = None
            self._active = False
            self._frame_img = None
            self.update()

        def _on_frame(self, video_frame: Any) -> None:
            if not self._active or not video_frame.isValid():
                return
            img = video_frame.toImage()
            if not img.isNull():
                # Mirror horizontally like a real mirror
                self._frame_img = img.mirrored(True, False)
                self.update()

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
            clip = QPainterPath()
            clip.addRoundedRect(rect, 18.0, 18.0)

            p.save()
            p.setClipPath(clip)
            if self._frame_img is not None and not self._frame_img.isNull():
                iw = float(self._frame_img.width())
                ih = float(self._frame_img.height())
                target_ar = rect.width() / max(1.0, rect.height())
                img_ar = iw / max(1.0, ih)
                # True aspect-fill center crop so the webcam fills the full notch with zero stretching
                if img_ar > target_ar:
                    crop_h = ih
                    crop_w = ih * target_ar
                else:
                    crop_w = iw
                    crop_h = iw / target_ar
                sx = (iw - crop_w) / 2.0
                sy = (ih - crop_h) / 2.0
                p.drawImage(rect, self._frame_img, QRectF(sx, sy, crop_w, crop_h))
            else:
                p.fillPath(clip, QColor(255, 255, 255, 12))
                p.setPen(QColor("#94a3b8"))
                p.setFont(QFont("SF Pro Display", 11, QFont.Weight.DemiBold))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._status_msg)
            p.restore()

    # ------------------------------------------------------------------
    # 4b. iPhone-Style 3-Dial 2D Timer Wheel with Mechanical Tick Sound
    # ------------------------------------------------------------------

    def _build_ios_tick_wav() -> bytes:
        """Synthesize a crisp ~8ms iOS picker wheel mechanical tick as an in-memory PCM WAV."""
        import struct
        sr = 22050
        n_samples = int(sr * 0.0085)
        pcm = bytearray()
        for i in range(n_samples):
            t = i / sr
            # Crisp tactile woody click: damped 1450Hz transient + soft 420Hz body
            env_hi = math.exp(-t / 0.0012)
            env_lo = math.exp(-t / 0.0022)
            s = 0.62 * math.sin(2.0 * math.pi * 1450.0 * t) * env_hi + 0.38 * math.sin(2.0 * math.pi * 420.0 * t) * env_lo
            val = max(-32767, min(32767, int(s * 9600.0)))
            pcm.extend(struct.pack("<h", val))
        data_sz = len(pcm)
        hdr = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_sz, b"WAVE",
            b"fmt ", 16, 1, 1, sr, sr * 2, 2, 16,
            b"data", data_sz,
        )
        return hdr + bytes(pcm)

    _IOS_TICK_WAV = _build_ios_tick_wav()

    def _play_ios_tick() -> None:
        if os.name != "nt":
            return
        try:
            import winsound
            winsound.PlaySound(
                _IOS_TICK_WAV,
                winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except Exception:
            pass

    class _IPhoneTimerWheel(QWidget):
        """iPhone-style 2D 3-dial scroll picker (hours | min | sec) with crisp tick sounds per step."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setFixedSize(416, 140)
            self.setMouseTracking(True)
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
            # (label, max_val, wrap, default_val)
            self._specs = (
                ("hours", 23, False),
                ("min", 59, True),
                ("sec", 59, True),
            )
            self._vals: list[int] = [0, 15, 0]
            self._disp: list[float] = [0.0, 15.0, 0.0]
            self._drag_col: int = -1
            self._drag_last_y: float = 0.0
            self._drag_accum: float = 0.0
            self._drag_moved: bool = False

            self._anim_timer = QTimer(self)
            self._anim_timer.setInterval(16)
            self._anim_timer.timeout.connect(self._tick_anim)

        def total_seconds(self) -> int:
            return self._vals[0] * 3600 + self._vals[1] * 60 + self._vals[2]

        def formatted_label(self) -> str:
            h, m, s = self._vals
            parts = []
            if h > 0:
                parts.append(f"{h}h")
            if m > 0:
                parts.append(f"{m}m")
            if s > 0 or not parts:
                parts.append(f"{s}s")
            return " ".join(parts) + " Timer"

        def _col_at_x(self, x: float) -> int:
            w = max(1.0, float(self.width()))
            pill_w = min(w - 16.0, 400.0)
            pad = (w - pill_w) / 2.0
            col_w = pill_w / 3.0
            idx = int((x - pad) / max(1.0, col_w))
            return max(0, min(2, idx))

        def _step_col(self, col: int, delta: int) -> None:
            if delta == 0:
                return
            _lbl, max_v, wrap = self._specs[col]
            old_v = self._vals[col]
            if wrap:
                span = max_v + 1
                new_v = (old_v + delta) % span
                # Adjust display value smoothly across wrap boundary
                self._disp[col] += (new_v - old_v) - delta
            else:
                new_v = max(0, min(max_v, old_v + delta))
            if new_v != old_v:
                self._vals[col] = new_v
                _play_ios_tick()
                if not self._anim_timer.isActive():
                    self._anim_timer.start()
                self.update()

        def _tick_anim(self) -> None:
            moving = False
            for i in range(3):
                diff = float(self._vals[i]) - self._disp[i]
                if abs(diff) > 0.004:
                    self._disp[i] += diff * 0.30
                    moving = True
                else:
                    self._disp[i] = float(self._vals[i])
            self.update()
            if not moving:
                self._anim_timer.stop()

        def wheelEvent(self, ev: Any) -> None:
            dy = ev.angleDelta().y()
            if dy == 0:
                return
            col = self._col_at_x(float(ev.position().x()))
            # Scroll down -> next number (+1), scroll up -> previous number (-1)
            step = -1 if dy > 0 else 1
            self._step_col(col, step)
            ev.accept()

        def mousePressEvent(self, ev: Any) -> None:
            if ev.button() == Qt.MouseButton.LeftButton:
                self._drag_col = self._col_at_x(float(ev.position().x()))
                self._drag_last_y = float(ev.position().y())
                self._drag_accum = 0.0
                self._drag_moved = False
                self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
                ev.accept()
                return
            super().mousePressEvent(ev)

        def mouseMoveEvent(self, ev: Any) -> None:
            if self._drag_col >= 0:
                y = float(ev.position().y())
                dy = y - self._drag_last_y
                self._drag_last_y = y
                self._drag_accum += dy
                step_px = 24.0
                while self._drag_accum <= -step_px:
                    self._drag_accum += step_px
                    self._drag_moved = True
                    self._step_col(self._drag_col, 1)
                while self._drag_accum >= step_px:
                    self._drag_accum -= step_px
                    self._drag_moved = True
                    self._step_col(self._drag_col, -1)
                ev.accept()
                return
            super().mouseMoveEvent(ev)

        def mouseReleaseEvent(self, ev: Any) -> None:
            if self._drag_col >= 0:
                col = self._drag_col
                was_drag = self._drag_moved
                self._drag_col = -1
                self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
                if not was_drag:
                    # Single click above or below the center pill steps by -1 / +1
                    cy = self.height() / 2.0
                    y = float(ev.position().y())
                    if y < cy - 17.0:
                        self._step_col(col, -1)
                    elif y > cy + 17.0:
                        self._step_col(col, 1)
                ev.accept()
                return
            super().mouseReleaseEvent(ev)

        def paintEvent(self, _ev: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            w = float(self.width())
            h = float(self.height())
            cy = h / 2.0

            pill_w = min(w - 16.0, 400.0)
            pill_l = (w - pill_w) / 2.0
            pill_h = 34.0
            pill_r = QRectF(pill_l, cy - pill_h / 2.0, pill_w, pill_h)

            # Iconic iPhone translucent rounded selection bar across all 3 dials
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 26))
            p.drawRoundedRect(pill_r, 12.0, 12.0)

            col_w = pill_w / 3.0
            row_step = 28.0

            for ci, (unit_lbl, max_v, wrap) in enumerate(self._specs):
                col_l = pill_l + ci * col_w
                # Generous spacing: right-aligned 2-digit number + crisp gap + unit label
                num_w = 46.0
                num_x = col_l + 14.0
                unit_x = num_x + num_w + 7.0
                unit_w = max(44.0, col_w - (unit_x - col_l) - 6.0)

                disp_v = self._disp[ci]
                center_int = int(round(disp_v))
                span = max_v + 1

                p.save()
                p.setClipRect(QRectF(col_l, 2.0, col_w, h - 4.0))
                for offset in (-2, -1, 0, 1, 2):
                    raw_i = center_int + offset
                    if wrap:
                        val_i = raw_i % span
                    else:
                        if raw_i < 0 or raw_i > max_v:
                            continue
                        val_i = raw_i
                    delta = float(raw_i) - disp_v
                    abs_d = abs(delta)
                    if abs_d > 2.35:
                        continue
                    ny = cy + delta * row_step
                    # Smooth iOS 2D vertical roll-off in opacity & size
                    alpha_f = max(0.0, 1.0 - (abs_d / 2.25) ** 1.35)
                    alpha_i = int(round(255.0 * alpha_f))
                    f_sz = max(11.0, 18.5 - abs_d * 2.8)
                    weight = QFont.Weight.DemiBold if abs_d < 0.45 else QFont.Weight.Medium
                    f = QFont("SF Pro Display")
                    f.setPointSizeF(f_sz)
                    f.setWeight(weight)
                    p.setFont(f)
                    if abs_d < 0.45:
                        p.setPen(QColor(255, 255, 255, alpha_i))
                    else:
                        p.setPen(QColor(186, 196, 212, int(alpha_i * 0.72)))
                    p.drawText(
                        QRectF(num_x, ny - 15.0, num_w, 30.0),
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        str(val_i),
                    )
                p.restore()

                # Fixed crisp unit label ('hours' / 'min' / 'sec') inside the center selection bar
                uf = QFont("SF Pro Display", 11, QFont.Weight.DemiBold)
                p.setFont(uf)
                p.setPen(QColor(241, 245, 249, 235))
                p.drawText(
                    QRectF(unit_x, cy - 15.0, unit_w, 30.0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    unit_lbl,
                )

    class _CircularTimerActionBtn(QWidget):
        """Hand-drawn circular iOS Play (green) or Cancel/Stop (x) vector button."""

        clicked = Signal()

        def __init__(self, kind: str = "play", size: int = 34, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self._kind = kind
            self._hovered = False
            self.setFixedSize(size, size)
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        def enterEvent(self, ev: Any) -> None:
            self._hovered = True
            self.update()
            super().enterEvent(ev)

        def leaveEvent(self, ev: Any) -> None:
            self._hovered = False
            self.update()
            super().leaveEvent(ev)

        def mousePressEvent(self, ev: Any) -> None:
            if ev.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()
                return
            super().mousePressEvent(ev)

        def paintEvent(self, _ev: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            r = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
            cx, cy = r.center().x(), r.center().y()
            p.setPen(Qt.PenStyle.NoPen)
            if self._kind == "play":
                p.setBrush(QColor(48, 209, 88, 92 if self._hovered else 54))
                p.drawEllipse(r)
                p.setBrush(QColor("#ffffff" if self._hovered else "#30d158"))
                tri = QPolygonF([
                    QPointF(cx - 4.0, cy - 6.2),
                    QPointF(cx - 4.0, cy + 6.2),
                    QPointF(cx + 6.8, cy),
                ])
                p.drawPolygon(tri)
            else:
                p.setBrush(QColor(255, 69, 58, 92 if self._hovered else 42))
                p.drawEllipse(r)
                p.setPen(QPen(QColor("#ffffff" if self._hovered else "#ff6961"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(cx - 4.5, cy - 4.5), QPointF(cx + 4.5, cy + 4.5))
                p.drawLine(QPointF(cx + 4.5, cy - 4.5), QPointF(cx - 4.5, cy + 4.5))

    # ------------------------------------------------------------------
    # 5. Notch Utility Hub Page (Shelf | Clip | Mirror | Timer | Notes | Tools)
    # ------------------------------------------------------------------

    class NotchToolsPage(QWidget):
        """6-tab pitch-black borderless Utility Hub inside the Notch."""

        go_home = Signal()
        ask_jarvis_file = Signal(str)
        start_quick_timer = Signal(int, str)
        stop_quick_timer = Signal()
        launch_color_picker = Signal()
        launch_screen_snip = Signal()
        toggle_focus_mode = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self._shelf_files: list[str] = []
            self._clip_items: list[str] = []
            self._tab_btns: list[QPushButton] = []
            self._note_save_timer = QTimer(self)
            self._note_save_timer.setSingleShot(True)
            self._note_save_timer.timeout.connect(self._save_scratchpad_disk)

            root = QVBoxLayout(self)
            root.setContentsMargins(6, 2, 6, 4)
            root.setSpacing(8)

            # Top Tab Pill Bar + Back Button
            nav = QHBoxLayout()
            nav.setSpacing(5)
            btn_back = QPushButton("← Home")
            btn_back.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_back.setFixedHeight(26)
            btn_back.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.07); color: #cbd5e1; border: none; "
                "border-radius: 13px; padding: 0 10px; font-size: 10.5px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(255,255,255,0.14); color: #ffffff; }"
            )
            btn_back.clicked.connect(self.go_home.emit)
            nav.addWidget(btn_back)
            nav.addStretch(1)

            for idx, title in enumerate(("Shelf", "Clip", "Mirror", "Timer", "Notes", "Tools")):
                b = QPushButton(title)
                b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                b.setFixedHeight(26)
                b.clicked.connect(lambda _c=False, i=idx: self.select_tab(i))
                nav.addWidget(b)
                self._tab_btns.append(b)
            nav.addStretch(1)
            root.addLayout(nav)

            self._tabs = QStackedWidget()
            self._tabs.addWidget(self._build_shelf_tab())
            self._tabs.addWidget(self._build_clipboard_tab())
            self._tabs.addWidget(self._build_mirror_tab())
            self._tabs.addWidget(self._build_timer_tab())
            self._tabs.addWidget(self._build_notes_tab())
            self._tabs.addWidget(self._build_quick_tools_tab())
            root.addWidget(self._tabs, stretch=1)
            self.select_tab(0)

            # Hook live Windows clipboard changes
            cb = QApplication.clipboard()
            if cb is not None:
                cb.dataChanged.connect(self._on_clipboard_changed)
                self._on_clipboard_changed()

        def select_tab(self, idx: int) -> None:
            prev = self._tabs.currentIndex()
            if prev == 2 and idx != 2:
                self._mirror.stop_camera()
            self._tabs.setCurrentIndex(idx)
            for i, b in enumerate(self._tab_btns):
                if i == idx:
                    accent = "#ff9f0a" if i == 3 else "#38bdf8"
                    b.setStyleSheet(
                        f"QPushButton {{ background: rgba(255,255,255,0.14); color: {accent}; border: none; "
                        "border-radius: 13px; padding: 0 13px; font-size: 11px; font-weight: 800; }"
                    )
                else:
                    b.setStyleSheet(
                        "QPushButton { background: transparent; color: #94a3b8; border: none; "
                        "border-radius: 13px; padding: 0 13px; font-size: 11px; font-weight: 700; }"
                        "QPushButton:hover { color: #f8fafc; background: rgba(255,255,255,0.06); }"
                    )
            if idx == 2 and self.isVisible():
                self._mirror.start_camera()

        def on_hidden(self) -> None:
            """Always turn off webcam immediately when closing or leaving Tools."""
            self._mirror.stop_camera()

        # --- Tab 0: Drag-and-Drop File Shelf + Ask JARVIS ---
        def _build_shelf_tab(self) -> QWidget:
            w = QWidget()
            hl = QHBoxLayout(w)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(10)

            # Left: Parked Files Tray
            self._shelf_box = QFrame()
            self._shelf_box.setAcceptDrops(True)
            self._shelf_box.setStyleSheet("QFrame { background: rgba(255,255,255,0.04); border: none; border-radius: 16px; }")
            s_vl = QVBoxLayout(self._shelf_box)
            s_vl.setContentsMargins(10, 8, 10, 8)
            s_vl.setSpacing(6)
            self._shelf_hint = QLabel("Drop files here to park them — drag back out to any app")
            self._shelf_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._shelf_hint.setStyleSheet("color: #64748b; font-size: 11px; font-weight: 600; background: transparent;")
            s_vl.addWidget(self._shelf_hint)

            self._shelf_row_widget = QWidget()
            self._shelf_row_widget.setStyleSheet("background: transparent;")
            self._shelf_row = QHBoxLayout(self._shelf_row_widget)
            self._shelf_row.setContentsMargins(0, 0, 0, 0)
            self._shelf_row.setSpacing(8)
            self._shelf_row.addStretch(1)

            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            sc.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sc.setStyleSheet("background: transparent; border: none;")
            sc.setWidget(self._shelf_row_widget)
            s_vl.addWidget(sc, stretch=1)
            hl.addWidget(self._shelf_box, stretch=1)

            # Right: Ask JARVIS Drop Zone
            self._jarvis_drop = _AskJarvisDropZone()
            self._jarvis_drop.file_dropped.connect(self.ask_jarvis_file.emit)
            hl.addWidget(self._jarvis_drop)
            return w

        def add_shelf_files(self, paths: list[str]) -> None:
            for p in paths:
                if p and os.path.exists(p) and p not in self._shelf_files:
                    self._shelf_files.insert(0, p)
            self._shelf_files = self._shelf_files[:10]
            self._rebuild_shelf_ui()

        def _remove_shelf_file(self, path: str) -> None:
            if path in self._shelf_files:
                self._shelf_files.remove(path)
                self._rebuild_shelf_ui()

        def _rebuild_shelf_ui(self) -> None:
            while self._shelf_row.count():
                item = self._shelf_row.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._shelf_hint.setVisible(len(self._shelf_files) == 0)
            for p in self._shelf_files:
                chip = _ShelfFileChip(p)
                chip.removed.connect(self._remove_shelf_file)
                self._shelf_row.addWidget(chip)
            self._shelf_row.addStretch(1)

        # --- Tab 1: Clipboard History ---
        def _build_clipboard_tab(self) -> QWidget:
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(6)

            self._clip_status = QLabel("Click any copied item to restore it to your clipboard")
            self._clip_status.setStyleSheet("color: #64748b; font-size: 10.5px; font-weight: 600; background: transparent;")
            vl.addWidget(self._clip_status)

            self._clip_list_widget = QWidget()
            self._clip_list_widget.setStyleSheet("background: transparent;")
            self._clip_list_layout = QVBoxLayout(self._clip_list_widget)
            self._clip_list_layout.setContentsMargins(0, 0, 0, 0)
            self._clip_list_layout.setSpacing(4)
            self._clip_list_layout.addStretch(1)

            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            sc.setStyleSheet("background: transparent; border: none;")
            sc.setWidget(self._clip_list_widget)
            vl.addWidget(sc, stretch=1)
            return w

        def _on_clipboard_changed(self) -> None:
            cb = QApplication.clipboard()
            if cb is None:
                return
            txt = (cb.text() or "").strip()
            if not txt:
                return
            if txt in self._clip_items:
                self._clip_items.remove(txt)
            self._clip_items.insert(0, txt)
            self._clip_items = self._clip_items[:8]
            self._rebuild_clipboard_ui()

        def _copy_back(self, txt: str) -> None:
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(txt)
            self._clip_status.setText("Copied to clipboard!")
            self._clip_status.setStyleSheet("color: #30d158; font-size: 10.5px; font-weight: 700; background: transparent;")
            QTimer.singleShot(
                1400,
                lambda: (
                    self._clip_status.setText("Click any copied item to restore it to your clipboard"),
                    self._clip_status.setStyleSheet("color: #64748b; font-size: 10.5px; font-weight: 600; background: transparent;"),
                ),
            )

        def _rebuild_clipboard_ui(self) -> None:
            while self._clip_list_layout.count():
                item = self._clip_list_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for txt in self._clip_items:
                preview = " ".join(txt.splitlines())[:72]
                b = QPushButton(preview)
                b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                b.setFixedHeight(28)
                b.setStyleSheet(
                    "QPushButton { background: rgba(255,255,255,0.05); color: #e2e8f0; border: none; "
                    "border-radius: 10px; padding: 0 10px; text-align: left; font-size: 11px; font-weight: 600; }"
                    "QPushButton:hover { background: rgba(56,189,248,0.16); color: #ffffff; }"
                )
                b.clicked.connect(lambda _c=False, t=txt: self._copy_back(t))
                self._clip_list_layout.addWidget(b)
            self._clip_list_layout.addStretch(1)

        # --- Tab 2: Full-Notch Live Webcam Mirror ---
        def _build_mirror_tab(self) -> QWidget:
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(0)

            self._mirror = _SquircleMirrorWidget()
            vl.addWidget(self._mirror, stretch=1)
            return w

        # --- Tab 3: iPhone-Style 3-Dial 2D Timer Wheel + Bottom Play / Stop Controls ---
        def _build_timer_tab(self) -> QWidget:
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(6, 0, 6, 4)
            vl.setSpacing(6)
            vl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            self._timer_wheel = _IPhoneTimerWheel()
            vl.addWidget(self._timer_wheel)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(14)
            btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

            btn_stop = _CircularTimerActionBtn("stop", size=34)
            btn_stop.setToolTip("Cancel active timer")
            btn_stop.clicked.connect(self.stop_quick_timer.emit)
            btn_row.addWidget(btn_stop)

            btn_play = _CircularTimerActionBtn("play", size=36)
            btn_play.setToolTip("Start timer")
            btn_play.clicked.connect(self._on_wheel_start)
            btn_row.addWidget(btn_play)

            vl.addLayout(btn_row)
            return w

        def _on_wheel_start(self) -> None:
            secs = self._timer_wheel.total_seconds()
            if secs <= 0:
                return
            self.start_quick_timer.emit(secs, self._timer_wheel.formatted_label())

        # --- Tab 4: Persistent Quick Sticky Note / Scratchpad ---
        def _scratchpad_path(self) -> Path:
            p = Path(__file__).resolve().parent.parent / ".cache" / "notch_scratchpad.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        def _build_notes_tab(self) -> QWidget:
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(2, 0, 2, 0)
            vl.setSpacing(6)

            self._note_edit = QTextEdit()
            self._note_edit.setPlaceholderText("Quick scratchpad — jot a link, snippet, or idea (auto-saved)...")
            self._note_edit.setStyleSheet(
                "QTextEdit { background: rgba(255,255,255,0.05); color: #f1f5f9; border: none; "
                "border-radius: 12px; padding: 8px 10px; font-size: 11.5px; }"
            )
            try:
                sp = self._scratchpad_path()
                if sp.exists():
                    self._note_edit.setPlainText(sp.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                pass
            self._note_edit.textChanged.connect(self._on_note_changed)
            vl.addWidget(self._note_edit, stretch=1)

            bar = QHBoxLayout()
            bar.setSpacing(6)
            self._note_status = QLabel("Auto-saved")
            self._note_status.setStyleSheet("color: #64748b; font-size: 10px; font-weight: 600; background: transparent;")
            bar.addWidget(self._note_status)
            bar.addStretch(1)

            b_copy = QPushButton("Copy All")
            b_copy.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b_copy.setFixedHeight(24)
            b_copy.setStyleSheet(
                "QPushButton { background: rgba(56,189,248,0.16); color: #38bdf8; border: none; "
                "border-radius: 10px; padding: 0 10px; font-size: 10px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(56,189,248,0.30); color: #ffffff; }"
            )
            b_copy.clicked.connect(self._copy_scratchpad)
            bar.addWidget(b_copy)

            b_clr = QPushButton("Clear")
            b_clr.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b_clr.setFixedHeight(24)
            b_clr.setStyleSheet(
                "QPushButton { background: rgba(248,113,113,0.14); color: #f87171; border: none; "
                "border-radius: 10px; padding: 0 10px; font-size: 10px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(248,113,113,0.28); color: #ffffff; }"
            )
            b_clr.clicked.connect(lambda: self._note_edit.clear())
            bar.addWidget(b_clr)
            vl.addLayout(bar)
            return w

        def _on_note_changed(self) -> None:
            n = len(self._note_edit.toPlainText())
            self._note_status.setText(f"{n} chars · saving...")
            self._note_save_timer.start(450)

        def _save_scratchpad_disk(self) -> None:
            try:
                txt = self._note_edit.toPlainText()
                self._scratchpad_path().write_text(txt, encoding="utf-8")
                self._note_status.setText(f"{len(txt)} chars · saved")
            except Exception:
                self._note_status.setText("Save failed")

        def _copy_scratchpad(self) -> None:
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(self._note_edit.toPlainText())
                self._note_status.setText("Copied to clipboard!")

        # --- Tab 5: Quick Tools (Color Picker | Region Snip -> Ask JARVIS | Focus Mode) ---
        def _build_quick_tools_tab(self) -> QWidget:
            w = QWidget()
            hl = QHBoxLayout(w)
            hl.setContentsMargins(4, 6, 4, 6)
            hl.setSpacing(10)

            cards = (
                (
                    "Color Picker",
                    "Zoomed magnifier loupe\nCopies #HEX to clipboard",
                    "#38bdf8",
                    "rgba(56,189,248,0.13)",
                    "rgba(56,189,248,0.26)",
                    self.launch_color_picker.emit,
                ),
                (
                    "Snip → JARVIS",
                    "Drag any screen region\nVision AI analyzes it live",
                    "#a855f7",
                    "rgba(168,85,247,0.14)",
                    "rgba(168,85,247,0.28)",
                    self.launch_screen_snip.emit,
                ),
                (
                    "Focus Mode",
                    "25m Pomodoro + Quiet\nMutes notch distractions",
                    "#ff9f0a",
                    "rgba(255,159,10,0.14)",
                    "rgba(255,159,10,0.28)",
                    self.toggle_focus_mode.emit,
                ),
            )
            for title, sub, fg, bg, bg_h, cb in cards:
                btn = QPushButton(f"{title}\n\n{sub}")
                btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                btn.setMinimumHeight(136)
                btn.setStyleSheet(
                    f"QPushButton {{ background: {bg}; color: {fg}; border: none; "
                    "border-radius: 16px; padding: 12px; font-size: 11.5px; font-weight: 800; }"
                    f"QPushButton:hover {{ background: {bg_h}; color: #ffffff; }}"
                )
                btn.clicked.connect(cb)
                hl.addWidget(btn, stretch=1)
            return w

    # ------------------------------------------------------------------
    # 5b. Fullscreen Eyedropper Color Picker & Region Snip Overlays
    # ------------------------------------------------------------------

    class ScreenColorPickerOverlay(QWidget):
        """Fullscreen magnifier loupe that copies the #HEX color under the cursor."""

        color_picked = Signal(str)
        cancelled = Signal()

        def __init__(self) -> None:
            super().__init__(None)
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setMouseTracking(True)
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            scr = QGuiApplication.primaryScreen()
            geo = scr.geometry() if scr else QRect(0, 0, 1920, 1080)
            self._screen_pm = scr.grabWindow(0) if scr else QPixmap(geo.size())
            self._screen_img = self._screen_pm.toImage()
            self._dpr = float(self._screen_pm.devicePixelRatio() or 1.0)
            self.setGeometry(geo)
            self._cur_pt = self.mapFromGlobal(QCursor.pos())
            self._cur_hex = "#000000"

        def _sample_hex(self, pt: QPointF) -> tuple[QColor, str]:
            ix = max(0, min(self._screen_img.width() - 1, int(round(pt.x() * self._dpr))))
            iy = max(0, min(self._screen_img.height() - 1, int(round(pt.y() * self._dpr))))
            col = self._screen_img.pixelColor(ix, iy)
            return col, col.name(QColor.NameFormat.HexRgb).upper()

        def mouseMoveEvent(self, ev: Any) -> None:
            self._cur_pt = ev.position()
            _c, self._cur_hex = self._sample_hex(self._cur_pt)
            self.update()

        def mousePressEvent(self, ev: Any) -> None:
            if ev.button() == Qt.MouseButton.LeftButton:
                _c, hx = self._sample_hex(ev.position())
                cb = QApplication.clipboard()
                if cb is not None:
                    cb.setText(hx)
                self.close()
                self.color_picked.emit(hx)
                return
            self.close()
            self.cancelled.emit()

        def keyPressEvent(self, ev: Any) -> None:
            if ev.key() == Qt.Key.Key_Escape:
                self.close()
                self.cancelled.emit()

        def paintEvent(self, _ev: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.drawPixmap(self.rect(), self._screen_pm)

            col, hx = self._sample_hex(self._cur_pt)
            cx = float(self._cur_pt.x())
            cy = float(self._cur_pt.y())
            loupe_r = 46.0
            lx = cx + 62.0 if cx + 130.0 < self.width() else cx - 62.0
            ly = cy - 62.0 if cy - 120.0 > 0 else cy + 62.0

            # Draw zoomed 9x9 pixel grid inside circular loupe
            p.save()
            clip = QPainterPath()
            clip.addEllipse(QPointF(lx, ly), loupe_r, loupe_r)
            p.setClipPath(clip)
            cell = (loupe_r * 2.0) / 9.0
            base_ix = int(round(cx * self._dpr))
            base_iy = int(round(cy * self._dpr))
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    sx = max(0, min(self._screen_img.width() - 1, base_ix + dx))
                    sy = max(0, min(self._screen_img.height() - 1, base_iy + dy))
                    p.fillRect(
                        QRectF(lx + (dx - 0.5) * cell, ly + (dy - 0.5) * cell, cell + 0.5, cell + 0.5),
                        self._screen_img.pixelColor(sx, sy),
                    )
            # Center pixel highlight box
            p.setPen(QPen(QColor("#ffffff"), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(lx - cell / 2.0, ly - cell / 2.0, cell, cell))
            p.restore()

            p.setPen(QPen(QColor("#07080b"), 3.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(lx, ly), loupe_r, loupe_r)
            p.setPen(QPen(col, 1.8))
            p.drawEllipse(QPointF(lx, ly), loupe_r - 1.4, loupe_r - 1.4)

            # Live #HEX & RGB pill under loupe
            pill_r = QRectF(lx - 58.0, ly + loupe_r + 8.0, 116.0, 26.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#07080b"))
            p.drawRoundedRect(pill_r, 11.0, 11.0)
            p.setBrush(col)
            p.drawEllipse(QPointF(pill_r.left() + 14.0, pill_r.center().y()), 5.5, 5.5)
            p.setPen(QColor("#ffffff"))
            p.setFont(QFont("SF Pro Display", 9, QFont.Weight.Bold))
            p.drawText(
                QRectF(pill_r.left() + 26.0, pill_r.top(), pill_r.width() - 32.0, pill_r.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{hx}  ({col.red()},{col.green()},{col.blue()})",
            )

    class ScreenSnipOverlay(QWidget):
        """Fullscreen drag-to-snip overlay — copies region to clipboard and sends to Ask JARVIS."""

        region_snipped = Signal(bytes)
        cancelled = Signal()

        def __init__(self) -> None:
            super().__init__(None)
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            scr = QGuiApplication.primaryScreen()
            geo = scr.geometry() if scr else QRect(0, 0, 1920, 1080)
            self._screen_pm = scr.grabWindow(0) if scr else QPixmap(geo.size())
            self._dpr = float(self._screen_pm.devicePixelRatio() or 1.0)
            self.setGeometry(geo)
            self._start_pt: Optional[QPointF] = None
            self._end_pt: Optional[QPointF] = None

        def mousePressEvent(self, ev: Any) -> None:
            if ev.button() == Qt.MouseButton.LeftButton:
                self._start_pt = ev.position()
                self._end_pt = ev.position()
                self.update()
                return
            self.close()
            self.cancelled.emit()

        def mouseMoveEvent(self, ev: Any) -> None:
            if self._start_pt is not None:
                self._end_pt = ev.position()
                self.update()

        def mouseReleaseEvent(self, ev: Any) -> None:
            if self._start_pt is None or self._end_pt is None:
                self.close()
                self.cancelled.emit()
                return
            r = QRectF(self._start_pt, self._end_pt).normalized()
            self.close()
            if r.width() < 12 or r.height() < 12:
                self.cancelled.emit()
                return
            crop_rect = QRect(
                int(round(r.left() * self._dpr)),
                int(round(r.top() * self._dpr)),
                max(1, int(round(r.width() * self._dpr))),
                max(1, int(round(r.height() * self._dpr))),
            )
            cropped = self._screen_pm.copy(crop_rect)
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setPixmap(cropped)
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            cropped.save(buf, "JPG", 88)
            jpg_bytes = bytes(buf.data())
            buf.close()
            self.region_snipped.emit(jpg_bytes)

        def keyPressEvent(self, ev: Any) -> None:
            if ev.key() == Qt.Key.Key_Escape:
                self.close()
                self.cancelled.emit()

        def paintEvent(self, _ev: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.drawPixmap(self.rect(), self._screen_pm)
            p.fillRect(self.rect(), QColor(5, 7, 11, 130))

            if self._start_pt is not None and self._end_pt is not None:
                r = QRectF(self._start_pt, self._end_pt).normalized()
                if r.width() > 2 and r.height() > 2:
                    src = QRectF(r.left() * self._dpr, r.top() * self._dpr, r.width() * self._dpr, r.height() * self._dpr)
                    p.drawPixmap(r, self._screen_pm, src)
                    p.setPen(QPen(QColor("#38bdf8"), 2.0))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRect(r)
                    dim_txt = f"{int(r.width())} × {int(r.height())}"
                    badge = QRectF(r.left(), max(8.0, r.top() - 26.0), 92.0, 22.0)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor("#07080b"))
                    p.drawRoundedRect(badge, 8.0, 8.0)
                    p.setPen(QColor("#38bdf8"))
                    p.setFont(QFont("SF Pro Display", 9, QFont.Weight.Bold))
                    p.drawText(badge, Qt.AlignmentFlag.AlignCenter, dim_txt)
            else:
                hint = QRectF((self.width() - 290.0) / 2.0, 32.0, 290.0, 30.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#07080b"))
                p.drawRoundedRect(hint, 15.0, 15.0)
                p.setPen(QColor("#f8fafc"))
                p.setFont(QFont("SF Pro Display", 10, QFont.Weight.DemiBold))
                p.drawText(hint, Qt.AlignmentFlag.AlignCenter, "Drag to snip region → Ask J.A.R.V.I.S. (Esc cancels)")

    # ------------------------------------------------------------------
    # 6. Synced Live Lyrics Card Page (Click Album Cover)
    # ------------------------------------------------------------------

    class _KaraokeLyricsCanvas(QWidget):
        """60fps Apple Music-style word-by-word animated karaoke lyrics canvas with smooth slide-up transitions."""

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setMinimumHeight(98)
            self._prev_line: str = ""
            self._cur_line: str = "♪"
            self._next_line: str = ""
            self._progress: float = 0.0
            self._audio_energy: float = 0.35
            self._smooth_energy: float = 0.35
            self._slide_t: float = 1.0  # 0.0 -> 1.0 smooth OutCubic slide-up transition on line change
            self._slide_start_ts: float = 0.0

        @staticmethod
        def _phonetic_word_weights(words: list[str]) -> list[float]:
            """Estimate natural sung duration per word using syllable count, vowel sustain, punctuation pauses, and phrase-end hold."""
            import re as _re

            weights: list[float] = []
            n = len(words)
            for idx, raw_w in enumerate(words):
                low = raw_w.lower()
                # 1. Syllable count via vowel clusters
                syls = max(1, len(_re.findall(r"[aeiouy]+", low)))
                w_dur = float(syls) * 1.15 + min(4, len(low)) * 0.14
                # 2. Elongated vowels ("ooh", "yeah", "aaah") get extra vocal sustain
                if _re.search(r"([aeiou])\1", low) or low.rstrip(".,!?") in ("oh", "ooh", "yeah", "whoa", "hey", "la", "na"):
                    w_dur += 0.95
                # 3. Natural breath pauses on punctuation
                if raw_w.endswith((",", ";")):
                    w_dur += 0.55
                elif raw_w.endswith(("...", "—", "-", "!", "?")):
                    w_dur += 0.85
                # 4. Phrase-final word is typically held/inflected slightly longer
                if idx == n - 1 and n > 1:
                    w_dur += 0.65
                weights.append(max(0.9, w_dur))
            return weights

        def update_lyrics(self, active_line: str, next_line: str, progress: float, audio_energy: float = 0.35) -> None:
            import time as _time

            line = active_line or "♪"
            nxt = next_line or ""
            now = _time.perf_counter()
            if line != self._cur_line:
                self._prev_line = self._cur_line
                self._cur_line = line
                self._slide_start_ts = now
                self._slide_t = 0.0
            self._next_line = nxt
            self._progress = max(0.0, min(1.0, float(progress)))
            self._audio_energy = max(0.0, min(1.0, float(audio_energy)))
            # Smooth vocal inflection envelope (fast attack on swells, silky decay)
            if self._audio_energy > self._smooth_energy:
                self._smooth_energy = self._smooth_energy * 0.55 + self._audio_energy * 0.45
            else:
                self._smooth_energy = self._smooth_energy * 0.82 + self._audio_energy * 0.18
            if self._slide_t < 1.0:
                u = min(1.0, (now - self._slide_start_ts) / 0.24)
                # Smooth OutCubic slide-up easing
                self._slide_t = 1.0 - (1.0 - u) ** 3
            self.update()

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            w, h = float(self.width()), float(self.height())
            st = self._slide_t

            # 1. Previous line smoothly sliding up & fading out during line transition
            if self._prev_line and st < 0.99:
                f_prev = QFont("SF Pro Display", 12, QFont.Weight.Bold)
                p.setFont(f_prev)
                alpha_prev = int(round(135.0 * (1.0 - st)))
                if alpha_prev > 2:
                    p.setPen(QColor(255, 255, 255, alpha_prev))
                    py = 14.0 - st * 18.0
                    p.drawText(QRectF(8.0, py, w - 16.0, 24.0), Qt.AlignmentFlag.AlignCenter, self._prev_line)

            # 2. Active line: silky 60fps word-by-word progressive sweep + gentle active-word float!
            f_cur = QFont("SF Pro Display", 15, QFont.Weight.ExtraBold)
            p.setFont(f_cur)
            fm = QFontMetricsF(f_cur)
            space_w = fm.horizontalAdvance(" ")
            words = self._cur_line.split()
            slide_dy = (1.0 - st) * 18.0
            base_alpha = 0.45 + 0.55 * st

            if not words or self._cur_line.startswith("Searching") or self._cur_line.startswith("No synced"):
                p.setPen(QColor(241, 245, 249, int(round(230 * base_alpha))))
                p.drawText(QRectF(8.0, 16.0 + slide_dy, w - 16.0, 48.0), Qt.AlignmentFlag.AlignCenter, self._cur_line)
            else:
                # Word-wrap into 1 or 2 centered rows cleanly
                max_row_w = w - 24.0
                rows: list[list[tuple[int, str, float]]] = [[]]
                cur_w = 0.0
                for i, wd in enumerate(words):
                    ww = fm.horizontalAdvance(wd)
                    add_w = ww if not rows[-1] else (space_w + ww)
                    if rows[-1] and (cur_w + add_w > max_row_w) and len(rows) < 2:
                        rows.append([])
                        cur_w = 0.0
                        add_w = ww
                    rows[-1].append((i, wd, ww))
                    cur_w += add_w

                # Compute phonetic syllable + vowel-sustain + punctuation-pause word windows
                weights = self._phonetic_word_weights(words)
                total_w = sum(weights)
                word_bounds: list[tuple[float, float]] = []
                acc = 0.0
                for wt in weights:
                    s0 = acc / total_w
                    acc += wt
                    s1 = acc / total_w
                    word_bounds.append((s0, s1))

                row_h = 26.0
                block_top = (34.0 if len(rows) == 1 else 19.0) + slide_dy
                inflect = self._smooth_energy  # 0.0 (whisper) .. 1.0 (vocal swell / chorus belt)

                for r_idx, row_words in enumerate(rows):
                    rw = sum(x[2] for x in row_words) + space_w * max(0, len(row_words) - 1)
                    x = (w - rw) / 2.0
                    y = block_top + r_idx * row_h
                    for w_idx, wd, ww in row_words:
                        s0, s1 = word_bounds[w_idx]
                        span = max(0.01, s1 - s0)
                        # Smooth, steady linear 60fps word sweep (zero jerky per-word acceleration!)
                        wf = max(0.0, min(1.0, (self._progress - s0) / span))
                        is_active = 0.0 < wf < 1.0
                        # Subtle, calm Apple Music-style upward float (-1.2px .. -2.0px with gentle vocal inflection)
                        lift = -(1.1 + 0.9 * inflect) * math.sin(wf * math.pi) if is_active else (-0.5 if wf >= 1.0 else 0.0)

                        # Base muted upcoming word
                        p.setPen(QColor(255, 255, 255, int(round(78 * base_alpha))))
                        p.drawText(QRectF(x, y + lift, ww + 2.0, row_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, wd)

                        # Clean glowing white progressive sweep across the word at 60fps
                        if wf > 0.0:
                            p.save()
                            p.setClipRect(QRectF(x - 1.0, y + lift - 3.0, (ww + 2.0) * wf + 1.0, row_h + 6.0))
                            p.setPen(QColor(255, 255, 255, int(round(255 * base_alpha))))
                            p.drawText(QRectF(x, y + lift, ww + 2.0, row_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, wd)
                            p.restore()
                        x += ww + space_w

            # 3. Upcoming next line softly fading in below
            if self._next_line:
                f_nxt = QFont("SF Pro Display", 11, QFont.Weight.DemiBold)
                p.setFont(f_nxt)
                nxt_alpha = int(round(105.0 * (0.35 + 0.65 * st)))
                p.setPen(QColor(148, 163, 184, nxt_alpha))
                ny = h - 24.0 + (1.0 - st) * 8.0
                p.drawText(QRectF(12.0, ny, w - 24.0, 22.0), Qt.AlignmentFlag.AlignCenter, self._next_line)

    class NotchLyricsPage(QWidget):
        """Sleek 60fps word-by-word karaoke synced lyrics view (lrclib.net) + sync nudge + playback controls."""

        go_home = Signal()
        focus_player = Signal()
        media_action = Signal(str)

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.sync_offset_sec: float = 0.0
            self._ambient_bg_pm: Optional[QPixmap] = None
            root = QVBoxLayout(self)
            root.setContentsMargins(8, 4, 8, 6)
            root.setSpacing(5)

            top = QHBoxLayout()
            top.setSpacing(6)
            btn_back = QPushButton("← Back")
            btn_back.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_back.setFixedHeight(24)
            btn_back.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.08); color: #cbd5e1; border: none; "
                "border-radius: 12px; padding: 0 10px; font-size: 10.5px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(255,255,255,0.16); color: #ffffff; }"
            )
            btn_back.clicked.connect(self.go_home.emit)
            top.addWidget(btn_back)

            self._track_lbl = QLabel("Live Lyrics")
            self._track_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 700; background: transparent;")
            top.addWidget(self._track_lbl, stretch=1)

            # Fine-tune sync nudge (-0.25s / +0.25s) in case a track's LRC is slightly early/late
            for delta, txt, tip in ((-0.25, "−", "Delay lyrics by 0.25s"), (0.25, "+", "Advance lyrics by 0.25s")):
                nb = QPushButton(txt)
                nb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                nb.setFixedSize(22, 22)
                nb.setToolTip(tip)
                nb.setStyleSheet(
                    "QPushButton { background: rgba(255,255,255,0.07); color: #94a3b8; border: none; "
                    "border-radius: 11px; font-size: 12px; font-weight: 800; }"
                    "QPushButton:hover { background: rgba(255,255,255,0.16); color: #ffffff; }"
                )
                nb.clicked.connect(lambda _c=False, d=delta: self._nudge_offset(d))
                top.addWidget(nb)

            btn_focus = QPushButton("Open Player ↗")
            btn_focus.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_focus.setFixedHeight(24)
            btn_focus.setStyleSheet(
                "QPushButton { background: rgba(29,185,84,0.18); color: #4ade80; border: none; "
                "border-radius: 12px; padding: 0 10px; font-size: 10.5px; font-weight: 800; }"
                "QPushButton:hover { background: rgba(29,185,84,0.32); color: #ffffff; }"
            )
            btn_focus.clicked.connect(self.focus_player.emit)
            top.addWidget(btn_focus)
            root.addLayout(top)

            self._canvas = _KaraokeLyricsCanvas()
            root.addWidget(self._canvas, stretch=1)

            # Compact built-in Prev / Play-Pause / Next bar right inside the Lyrics card
            ctrl_row = QHBoxLayout()
            ctrl_row.setSpacing(8)
            ctrl_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
            for act, txt in (("prev_track", "Prev"), ("play_pause", "Play / Pause"), ("next_track", "Next")):
                mb = QPushButton(txt)
                mb.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                mb.setFixedHeight(24)
                mb.setStyleSheet(
                    "QPushButton { background: rgba(255,255,255,0.08); color: #e2e8f0; border: none; "
                    "border-radius: 11px; padding: 0 12px; font-size: 10.5px; font-weight: 700; }"
                    "QPushButton:hover { background: rgba(255,255,255,0.18); color: #ffffff; }"
                )
                mb.clicked.connect(lambda _c=False, a=act: self.media_action.emit(a))
                ctrl_row.addWidget(mb)
            root.addLayout(ctrl_row)

        def _nudge_offset(self, delta: float) -> None:
            self.sync_offset_sec = max(-5.0, min(5.0, self.sync_offset_sec + delta))

        def set_album_pixmap(self, pm: Optional[QPixmap]) -> None:
            """Create an Apple Music-style softly blurred ultra-dark ambient album art backdrop."""
            if pm is None or pm.isNull():
                self._ambient_bg_pm = None
                self.update()
                return
            try:
                # Downscale to 24x24 then smooth-upscale -> instant, zero-cost Gaussian-like ambient blur!
                tiny = pm.scaled(24, 24, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self._ambient_bg_pm = tiny.scaled(240, 120, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            except Exception:
                self._ambient_bg_pm = None
            self.update()

        def paintEvent(self, ev: Any) -> None:
            if self._ambient_bg_pm is not None and not self._ambient_bg_pm.isNull():
                p = QPainter(self)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(self.rect().adjusted(2, 0, -2, 0)), 18.0, 18.0)
                p.setClipPath(clip)
                p.setOpacity(0.13)
                p.drawPixmap(self.rect(), self._ambient_bg_pm)
                p.setOpacity(1.0)
            super().paintEvent(ev)

        def set_lyrics(
            self,
            header: str,
            active_line: str,
            next_line: str,
            progress: float = 1.0,
            audio_energy: float = 0.35,
        ) -> None:
            if abs(self.sync_offset_sec) >= 0.05:
                sign = "+" if self.sync_offset_sec > 0 else ""
                self._track_lbl.setText(f"{header} ({sign}{self.sync_offset_sec:.2f}s)")
            else:
                self._track_lbl.setText(header)
            self._canvas.update_lyrics(active_line, next_line, progress, audio_energy=audio_energy)

    # ------------------------------------------------------------------
    # 7. Live Notification Toast Pill Page (Telegram / Reminder / Timer)
    # ------------------------------------------------------------------

    class NotchToastPage(QWidget):
        """Clean 2-line notification preview pill with 1-click Reply and Dismiss."""

        reply_clicked = Signal(str)
        dismiss_clicked = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self._context_title = ""
            hl = QHBoxLayout(self)
            hl.setContentsMargins(8, 4, 8, 4)
            hl.setSpacing(10)

            self._dot = QFrame()
            self._dot.setFixedSize(10, 10)
            self._dot.setStyleSheet("background: #38bdf8; border-radius: 5px;")
            hl.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignVCenter)

            col = QVBoxLayout()
            col.setSpacing(1)
            self._title_lbl = QLabel("Notification")
            self._title_lbl.setStyleSheet("color: #ffffff; font-size: 12px; font-weight: 800; background: transparent;")
            self._body_lbl = QLabel("")
            self._body_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 600; background: transparent;")
            col.addWidget(self._title_lbl)
            col.addWidget(self._body_lbl)
            hl.addLayout(col, stretch=1)

            btn_reply = QPushButton("Reply")
            btn_reply.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_reply.setFixedHeight(26)
            btn_reply.setStyleSheet(
                "QPushButton { background: rgba(56,189,248,0.20); color: #38bdf8; border: none; "
                "border-radius: 13px; padding: 0 10px; font-size: 10.5px; font-weight: 800; }"
                "QPushButton:hover { background: rgba(56,189,248,0.35); color: #ffffff; }"
            )
            btn_reply.clicked.connect(lambda: self.reply_clicked.emit(self._context_title))
            hl.addWidget(btn_reply, alignment=Qt.AlignmentFlag.AlignVCenter)

            btn_dismiss = QPushButton("×")
            btn_dismiss.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_dismiss.setFixedSize(24, 24)
            btn_dismiss.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.08); color: #94a3b8; border: none; "
                "border-radius: 12px; font-size: 14px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(255,255,255,0.16); color: #ffffff; }"
            )
            btn_dismiss.clicked.connect(self.dismiss_clicked.emit)
            hl.addWidget(btn_dismiss, alignment=Qt.AlignmentFlag.AlignVCenter)

        def set_toast(self, title: str, body: str, source: str = "JARVIS") -> None:
            self._context_title = title
            color = "#ff9f0a" if "timer" in source.lower() else ("#30d158" if "telegram" in source.lower() else "#38bdf8")
            self._dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
            self._title_lbl.setText(title[:42])
            self._body_lbl.setText(" ".join((body or "").splitlines())[:56])

    return {
        "draw_hud_pill": draw_hud_pill,
        "draw_battery_pill": draw_battery_pill,
        "draw_download_pill": draw_download_pill,
        "draw_privacy_dots": draw_privacy_dots,
        "draw_bluetooth_pill": draw_bluetooth_pill,
        "draw_network_pill": draw_network_pill,
        "draw_lockkey_pill": draw_lockkey_pill,
        "draw_call_pill": draw_call_pill,
        "draw_color_swatch_pill": draw_color_swatch_pill,
        "WeatherBadgeWidget": WeatherBadgeWidget,
        "NotchToolsPage": NotchToolsPage,
        "NotchLyricsPage": NotchLyricsPage,
        "NotchToastPage": NotchToastPage,
        "ScreenColorPickerOverlay": ScreenColorPickerOverlay,
        "ScreenSnipOverlay": ScreenSnipOverlay,
    }

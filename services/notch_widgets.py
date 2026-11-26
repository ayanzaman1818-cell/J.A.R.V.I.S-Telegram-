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
    from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QCursor,
        QDrag,
        QFont,
        QFontMetricsF,
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QScrollArea,
        QStackedWidget,
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
    # 5. Notch Utility Hub Page (Shelf | Clipboard | Mirror | Timer)
    # ------------------------------------------------------------------

    class NotchToolsPage(QWidget):
        """4-tab pitch-black borderless Utility Hub inside the Notch."""

        go_home = Signal()
        ask_jarvis_file = Signal(str)
        start_quick_timer = Signal(int, str)
        stop_quick_timer = Signal()

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self._shelf_files: list[str] = []
            self._clip_items: list[str] = []
            self._tab_btns: list[QPushButton] = []

            root = QVBoxLayout(self)
            root.setContentsMargins(6, 2, 6, 4)
            root.setSpacing(8)

            # Top Tab Pill Bar + Back Button
            nav = QHBoxLayout()
            nav.setSpacing(6)
            btn_back = QPushButton("← Dashboard")
            btn_back.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_back.setFixedHeight(26)
            btn_back.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.07); color: #cbd5e1; border: none; "
                "border-radius: 13px; padding: 0 11px; font-size: 11px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(255,255,255,0.14); color: #ffffff; }"
            )
            btn_back.clicked.connect(self.go_home.emit)
            nav.addWidget(btn_back)
            nav.addStretch(1)

            for idx, title in enumerate(("Shelf", "Clipboard", "Mirror", "Timer")):
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

        # --- Tab 3: Quick Pomodoro / Notch Timer Creator ---
        def _build_timer_tab(self) -> QWidget:
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(6, 6, 6, 6)
            vl.setSpacing(10)
            vl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            hdr = QLabel("Quick Focus Timer — runs live with the 60fps orange notch dial")
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr.setStyleSheet("color: #ff9f0a; font-size: 11.5px; font-weight: 700; background: transparent;")
            vl.addWidget(hdr)

            presets_row = QHBoxLayout()
            presets_row.setSpacing(8)
            presets_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
            for mins, lbl in ((5, "5m Break"), (10, "10m Quick"), (15, "15m Sprint"), (25, "25m Focus"), (45, "45m Deep")):
                b = QPushButton(lbl)
                b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                b.setFixedHeight(36)
                b.setStyleSheet(
                    "QPushButton { background: rgba(255,159,10,0.14); color: #ff9f0a; border: none; "
                    "border-radius: 12px; padding: 0 12px; font-size: 11.5px; font-weight: 800; }"
                    "QPushButton:hover { background: rgba(255,159,10,0.28); color: #ffffff; }"
                )
                b.clicked.connect(lambda _c=False, m=mins, lb=lbl: self.start_quick_timer.emit(m * 60, lb))
                presets_row.addWidget(b)
            vl.addLayout(presets_row)

            btn_stop = QPushButton("Stop Active Notch Timer")
            btn_stop.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn_stop.setFixedSize(186, 28)
            btn_stop.setStyleSheet(
                "QPushButton { background: rgba(248,113,113,0.14); color: #f87171; border: none; "
                "border-radius: 12px; font-size: 11px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(248,113,113,0.28); color: #ffffff; }"
            )
            btn_stop.clicked.connect(self.stop_quick_timer.emit)
            vl.addWidget(btn_stop, alignment=Qt.AlignmentFlag.AlignHCenter)
            return w

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
                u = min(1.0, (now - self._slide_start_ts) / 0.34)
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
                        # Smooth 0..1 fill for this specific word (gentle S-curve for natural vocal attack)
                        raw_wf = max(0.0, min(1.0, (self._progress - s0) / span))
                        wf = raw_wf * raw_wf * (3.0 - 2.0 * raw_wf)
                        # Vocal-inflection-reactive float lift (-1.4px on quiet words -> -3.8px on loud vocal swells!)
                        is_active = 0.0 < wf < 1.0
                        lift = -(1.4 + 2.4 * inflect) * math.sin(wf * math.pi) if is_active else (-0.8 if wf >= 1.0 else 0.0)

                        # Soft luminous inflection halo behind the active word when the singer swells
                        if is_active and inflect > 0.25:
                            glow_a = int(round(min(72.0, (inflect - 0.20) * 85.0 * math.sin(wf * math.pi) * base_alpha)))
                            if glow_a > 2:
                                p.setPen(Qt.PenStyle.NoPen)
                                p.setBrush(QColor(125, 211, 252, glow_a))
                                p.drawRoundedRect(QRectF(x - 3.0, y + lift + 2.0, ww + 6.0, row_h - 4.0), 7.0, 7.0)

                        # Base muted upcoming word
                        p.setPen(QColor(255, 255, 255, int(round(76 * base_alpha))))
                        p.drawText(QRectF(x, y + lift, ww + 2.0, row_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, wd)

                        # Progressive glowing sweep across the word at 60fps synced to syllable cadence!
                        if wf > 0.0:
                            p.save()
                            p.setClipRect(QRectF(x - 1.0, y + lift - 3.0, (ww + 2.0) * wf + 1.0, row_h + 6.0))
                            if is_active:
                                # Shift subtly brighter/warmer on higher vocal inflection
                                b_tint = int(round(255 - 35 * (1.0 - inflect)))
                                p.setPen(QColor(b_tint, 245, 255, int(round(255 * base_alpha))))
                            else:
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
        "WeatherBadgeWidget": WeatherBadgeWidget,
        "NotchToolsPage": NotchToolsPage,
        "NotchLyricsPage": NotchLyricsPage,
        "NotchToastPage": NotchToastPage,
    }

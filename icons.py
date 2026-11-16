"""Drawn icons (v0.10.0-beta.2): every emoji / unicode glyph of the UI is replaced by these.

The shapes are the line icons of the approved design canvas (24-unit view box, round caps and joins, stroke 1.8;
direction arrows and the crossed disc 2.2), rendered with Qt's SVG renderer straight into the painter, so they are
crisp at any DPI (the build PC runs at 150 %). MiniCRM = a drawn "M" in a rounded square, Google = a drawn "G" in a
circle (final, user 2026-11-13).

    paint(painter, name, rect, color)      draw into a list row / any painter (vector, nothing cached on disk)
    icon(name, color, px, disabled=None)   QIcon for a button (normal + optional disabled colour)

`color` is a theme role ("text", "green", ...) or a #hex. Renderers are cached per (name, colour) and the cache is
dropped on a theme switch. If QtSvg is missing in a build, a log line says so once and the old glyph is drawn as
text instead (UNVERIFIED until the first exe build of beta.2: PyInstaller should bundle QtSvg because it is imported).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPen, QPixmap

import theme

log = logging.getLogger("callcompanion.icons")

try:
    from PySide6.QtSvg import QSvgRenderer
    _SVG_OK = True
except Exception:                                # pragma: no cover - only if a build left QtSvg out
    QSvgRenderer = None
    _SVG_OK = False
    log.warning("icons: QtSvg not available - text glyphs are used instead of the drawn icons")

# name -> (svg body, stroke width). Paths copied from the design canvas boards (Kártya / Főablak / Sorgombok 0.10).
SHAPES = {
    "phone":   ('<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/>',
                1.8),
    "hangup":  ('<path d="M3 15.5c5-5 13-5 18 0l-2.5 2.5-3-1.5v-2.5a10 10 0 0 0-7 0v2.5l-3 1.5z"/>', 1.8),
    "pause":   ('<path d="M9 5v14"/><path d="M15 5v14"/>', 1.8),
    "play":    ('<path d="M7 5l12 7-12 7z"/>', 1.8),
    "copy":    ('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>', 1.8),
    "check":   ('<path d="M5 12.5l4.5 4.5L19 7.5"/>', 1.8),
    "close":   ('<path d="M6 6l12 12"/><path d="M18 6L6 18"/>', 1.8),
    "missed":  ('<path d="M6 6l12 12"/><path d="M18 6L6 18"/>', 2.2),
    "arrow_in":  ('<path d="M17 7L7 17"/><path d="M16 17H7V8"/>', 2.2),
    "arrow_out": ('<path d="M7 17L17 7"/><path d="M8 7h9v9"/>', 2.2),
    "chev_down":  ('<path d="M6 9l6 6 6-6"/>', 1.8),
    "chev_right": ('<path d="M9 6l6 6-6 6"/>', 1.8),
    "chev_up":    ('<path d="M6 15l6-6 6 6"/>', 1.8),
    "search":  ('<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>', 1.8),
    "transfer": ('<path d="M4 5v6a3 3 0 0 0 3 3h12"/><path d="M15 10l4 4-4 4"/>', 1.8),
    "pencil":  ('<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>', 1.8),
    "code":    ('<path d="M8 8l-4 4 4 4"/><path d="M16 8l4 4-4 4"/>', 1.8),
    "gear":    ('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1'
                'a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0'
                '-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7'
                ' 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 '
                '0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1'
                '.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>', 1.8),
}
LETTERS = {"crm": ("M", "square"), "google": ("G", "circle")}      # drawn letter marks
FALLBACK = {"phone": "📞", "hangup": "✕", "pause": "⏸", "play": "▶", "copy": "📋", "check": "✓", "close": "✕",
            "missed": "✕", "arrow_in": "↙", "arrow_out": "↗", "chev_down": "▾", "chev_right": "▸", "chev_up": "▲",
            "search": "🔍", "transfer": "↪", "pencil": "✎", "code": "</>", "gear": "⚙", "crm": "M", "google": "G"}
NAMES = tuple(SHAPES) + tuple(LETTERS)

_RENDERERS: dict = {}


def _hex(color: str) -> str:
    return color if color.startswith("#") else theme.color(color)


def _renderer(name: str, hexcol: str):
    key = (name, hexcol)
    r = _RENDERERS.get(key)
    if r is None:
        body, sw = SHAPES[name]
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{hexcol}" '
               f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
        r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        _RENDERERS[key] = r
    return r


def _clear_cache():
    _RENDERERS.clear()


theme.changed.connect(_clear_cache)


def _paint_letter(p: QPainter, name: str, rect: QRectF, col: QColor):
    """MiniCRM "M" / Google "G": 15-unit mark of the canvas (border 1.6, 9 px bold letter), scaled to rect."""
    letter, shape = LETTERS[name]
    s = min(rect.width(), rect.height())
    box = QRectF(rect.center().x() - s / 2, rect.center().y() - s / 2, s, s)
    k = s / 15.0
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(col, 1.6 * k))
    p.setBrush(Qt.BrushStyle.NoBrush)
    inner = box.adjusted(0.8 * k, 0.8 * k, -0.8 * k, -0.8 * k)
    if shape == "square":
        p.drawRoundedRect(inner, 4 * k - 0.8 * k, 4 * k - 0.8 * k)
    else:
        p.drawEllipse(inner)
    f = QFont("Segoe UI")
    f.setPixelSize(max(6, round(9 * k)))
    f.setWeight(QFont.Weight.ExtraBold)
    p.setFont(f)
    p.drawText(box, Qt.AlignmentFlag.AlignCenter, letter)
    p.restore()


def paint(p: QPainter, name: str, rect: QRectF, color: str = "text") -> None:
    """Draw icon `name` centred in `rect` (square area = min side) in `color` (role or #hex)."""
    rect = QRectF(rect)
    hexcol = _hex(color)
    if name in LETTERS:
        _paint_letter(p, name, rect, QColor(hexcol))
        return
    if not _SVG_OK or name not in SHAPES:
        p.save()
        p.setPen(QColor(hexcol))
        f = QFont("Segoe UI")
        f.setFamilies(["Segoe UI Emoji", "Segoe UI"])
        f.setPixelSize(max(8, int(rect.height() * 0.8)))
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, FALLBACK.get(name, "?"))
        p.restore()
        return
    s = min(rect.width(), rect.height())
    box = QRectF(rect.center().x() - s / 2, rect.center().y() - s / 2, s, s)
    _renderer(name, hexcol).render(p, box)


def pixmap(name: str, color: str, px: int, dpr: float | None = None) -> QPixmap:
    if dpr is None:
        scr = QGuiApplication.primaryScreen()
        dpr = scr.devicePixelRatio() if scr is not None else 1.0
    pm = QPixmap(max(1, round(px * dpr)), max(1, round(px * dpr)))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint(p, name, QRectF(0, 0, px, px), color)
    p.end()
    return pm


def icon(name: str, color: str = "text", px: int = 15, disabled: str | None = "disabled") -> QIcon:
    """Button icon; build it again after a theme switch (the colours are resolved now)."""
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, px), QIcon.Mode.Normal)
    ic.addPixmap(pixmap(name, color, px), QIcon.Mode.Active)
    if disabled:
        ic.addPixmap(pixmap(name, disabled, px), QIcon.Mode.Disabled)
    return ic


def size(px: int = 15) -> QSize:
    return QSize(px, px)


def available() -> bool:
    return _SVG_OK


def logo_pixmap(px: int) -> QPixmap:
    """v1.0.0: the app logo (assets/logo.svg, the user's design) for the Névjegy page; falls back to app.ico."""
    import config
    scr = QGuiApplication.primaryScreen()
    dpr = scr.devicePixelRatio() if scr is not None else 1.0
    path = config.resource_path("assets/logo.svg")
    if _SVG_OK and path.exists():
        r = QSvgRenderer(str(path))
        if r.isValid():
            pm = QPixmap(max(1, round(px * dpr)), max(1, round(px * dpr)))
            pm.fill(Qt.GlobalColor.transparent)
            pm.setDevicePixelRatio(dpr)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r.render(p, QRectF(0, 0, px, px))
            p.end()
            return pm
    log.info("icons: logo.svg not usable, app.ico is shown")
    return QIcon(str(config.resource_path("assets/app.ico"))).pixmap(px, px)

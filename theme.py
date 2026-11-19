"""Theme system (v0.10.0): dark / light palette, "Rendszer szerint" (Windows app theme), live switching.

Every colour of the app comes from here: the main window, the settings dialog, the extension editor, the developer
tools, the tray menu and the call card. Nothing is restarted on a switch: `changed` is emitted and every open window
re-applies its style sheet (built by the qss_* functions below from the current palette) and repaints.

    mode        "dark" | "light" | "system"   (settings.json "theme", default "system")
    system      HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize  AppsUseLightTheme (1 = light)

Dark = Catppuccin Mocha (the look of 0.9). Light = Catppuccin Latte base with darker state colours (>= 4.5:1 on white),
as drawn on the approved design canvas (2026-11-13). The call card keeps its per-state colours in both.

Colours are addressed by ROLE names (text, subtext, green, blue, ...); palette()[role] gives the hex of the current
theme, so painters that ask at paint time follow a switch by themselves. Widgets whose text colour depends on a
state use the "tone" property (set_tone(widget, role)); the style sheets carry a [tone="..."] rule for every role.

UNVERIFIED (2026-11-16): that Qt reports a change of the Windows app theme while the app runs
(QStyleHints.colorSchemeChanged). The registry is also re-read whenever one of our windows is shown, and every switch
is logged as `theme: ...`.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass, fields

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication

log = logging.getLogger("callcompanion.theme")

MODES = ("dark", "light", "system")
DEFAULT_MODE = "system"


@dataclass(frozen=True)
class Palette:
    name: str
    light: bool
    # surfaces
    base: str
    mantle: str
    crust: str
    surface: str        # status card, tables, tiles
    surface2: str       # buttons, selected pill
    overlay: str        # separators, footer version, disabled text
    edge: str           # 1 px border of cards / rows ("transparent" in dark)
    # text
    text: str
    subtext: str
    subtext1: str       # card company line, log view
    dim: str            # card number line, muted hints on the card
    disabled: str
    # states
    green: str
    yellow: str
    red: str
    blue: str
    blue_hover: str
    mauve: str
    lavender: str       # company tag
    peach: str          # on hold (amber)
    burgundy: str       # missed calls
    grey: str           # ended card
    missed_text: str    # "N nem fogadott" in the day header
    missed_status: str  # header text of the missed card
    missed_cross: str   # cross on the burgundy disc of the missed card icon
    on_accent: str      # text on a filled blue / green button
    # history rows
    row: str
    row_hover: str
    row_sel: str
    row_missed: str
    row_missed_hover: str
    done_bg: str        # green "✓ Másolva" button
    danger_bg: str
    danger_hover: str
    tip_bg: str         # tooltips (inverse of the window)
    tip_text: str
    # rgba tuples for painters / the card
    hover_rgba: tuple   # stronger background under the hovered row action
    soft_rgba: tuple    # v0.10.0-beta.2: soft background of all row actions while the row is hovered
    green_hover: str    # filled green "Visszahívás" button, hovered
    card_bg: tuple
    card_missed_bg: tuple
    card_edge: tuple    # neutral card border (ended / no state)
    card_btn: int       # alpha of the card button background (white in dark, slate in light)
    card_btn_hover: int

    def __getitem__(self, role: str) -> str:
        return getattr(self, role)


DARK = Palette(
    name="dark", light=False,
    base="#1e1e2e", mantle="#181825", crust="#11111b", surface="#313244", surface2="#45475a", overlay="#585b70",
    edge="transparent",
    text="#cdd6f4", subtext="#a6adc8", subtext1="#bac2de", dim="#9399b2", disabled="#6c7086",
    green="#a6e3a1", yellow="#f9e2af", red="#f38ba8", blue="#89b4fa", blue_hover="#a6c8ff", mauve="#cba6f7",
    lavender="#b4befe", peach="#fab387", burgundy="#8c2f45", grey="#7f849c",
    missed_text="#e0607e", missed_status="#f5a3b5", missed_cross="#f5c2cf", on_accent="#1e1e2e",
    row="#313244", row_hover="#3a3b4f", row_sel="#45475a", row_missed="#33232d", row_missed_hover="#3a2733",
    done_bg="#2f4a3a", danger_bg="#5a2a38", danger_hover="#6e3344", tip_bg="#cdd6f4", tip_text="#1e1e2e",
    hover_rgba=(255, 255, 255, 36), soft_rgba=(255, 255, 255, 20), green_hover="#bfeeba",
    card_bg=(30, 30, 46, 238), card_missed_bg=(52, 24, 34, 242),
    card_edge=(255, 255, 255, 30), card_btn=20, card_btn_hover=42,
)

LIGHT = Palette(
    name="light", light=True,
    base="#eff1f5", mantle="#e6e9ef", crust="#dce0e8", surface="#ffffff", surface2="#ccd0da", overlay="#9ca0b0",
    edge="#dce0e8",
    text="#4c4f69", subtext="#5c5f77", subtext1="#5c5f77", dim="#6c6f85", disabled="#9ca0b0",
    green="#2e7d1f", yellow="#a15c07", red="#d20f39", blue="#1e66f5", blue_hover="#4a85f7", mauve="#8839ef",
    lavender="#4f5fd8", peach="#c2410c", burgundy="#b4294a", grey="#6c6f85",
    missed_text="#b4294a", missed_status="#b4294a", missed_cross="#ffffff", on_accent="#ffffff",
    row="#ffffff", row_hover="#f4f5f9", row_sel="#e6e9ef", row_missed="#fcf0f3", row_missed_hover="#f8e6eb",
    done_bg="#dde9dc", danger_bg="#f9dde3", danger_hover="#f3c9d2", tip_bg="#4c4f69", tip_text="#eff1f5",
    hover_rgba=(76, 79, 105, 36), soft_rgba=(76, 79, 105, 18), green_hover="#3a8f2a",
    card_bg=(255, 255, 255, 248), card_missed_bg=(253, 240, 243, 250),
    card_edge=(76, 79, 105, 41), card_btn=18, card_btn_hover=36,
)

# roles that may be used as a "tone" (text colour by state)
TONES = ("text", "subtext", "subtext1", "dim", "green", "yellow", "red", "blue", "mauve", "lavender", "peach",
         "burgundy", "grey", "missed_status")


class _Theme(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.mode = DEFAULT_MODE
        self.pal = DARK
        self._app = None


_T = _Theme()
changed = _T.changed            # theme.changed.connect(slot)


# ------------------------------------------------------------------------------------------------------- state ----
def palette() -> Palette:
    return _T.pal


def mode() -> str:
    return _T.mode


def color(role: str) -> str:
    return getattr(_T.pal, role)


def qcolor(role: str, alpha: int | None = None) -> QColor:
    c = QColor(getattr(_T.pal, role))
    if alpha is not None:
        c.setAlpha(max(0, min(255, int(alpha))))
    return c


def system_is_light() -> bool:
    """Windows app theme (Settings > Personalisation > Colours > app mode). Missing value = light (Windows'
    default); not Windows = dark."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _t = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(value)
    except FileNotFoundError:
        return True
    except OSError:
        log.warning("theme: AppsUseLightTheme unreadable, using dark")
        return False


def _resolve(m: str) -> Palette:
    if m == "light":
        return LIGHT
    if m == "dark":
        return DARK
    return LIGHT if system_is_light() else DARK


def _apply(reason: str) -> None:
    new = _resolve(_T.mode)
    if new is _T.pal:
        return
    _T.pal = new
    log.info("theme: %s -> %s (mode %s)", reason, new.name, _T.mode)
    for w in _top_windows():
        set_title_bar(w)
    _T.changed.emit()


def set_mode(m: str) -> None:
    """Switch at once (live, no restart). Unknown values fall back to "system"."""
    m = m if m in MODES else DEFAULT_MODE
    if m != _T.mode:
        _T.mode = m
        log.info("theme: mode set to %s", m)
    _apply("mode " + m)


def recheck_system() -> None:
    """Re-read the Windows app theme (cheap registry read); used when a window is shown."""
    if _T.mode == "system":
        _apply("system changed")


class _ShowWatcher(QObject):
    """App-wide filter: a window being shown gets the matching title bar, and in "system" mode the Windows theme is
    re-read (safety net in case Qt does not report the change). Only Show events of windows do any work."""
    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Show and getattr(obj, "isWindow", None) and obj.isWindow() \
                and obj.windowType() in (Qt.WindowType.Window, Qt.WindowType.Dialog):   # not tooltips / menus
            try:
                recheck_system()
                set_title_bar(obj)
            except Exception:                       # never break a window over the theme
                log.exception("theme: show hook failed")
        return False


_WATCHER = None


def init(app, m: str = DEFAULT_MODE) -> None:
    """Call once after QApplication exists (main.py and the previews)."""
    global _WATCHER
    _T._app = app
    _T.mode = m if m in MODES else DEFAULT_MODE
    _T.pal = _resolve(_T.mode)
    log.info("theme: start, mode %s -> %s", _T.mode, _T.pal.name)
    try:
        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(lambda *_a: _apply("system changed (Qt)"))   # Qt 6.5+
    except Exception:
        log.info("theme: Qt reports no colour scheme changes here; re-read when a window opens")
    if _WATCHER is None:
        _WATCHER = _ShowWatcher()
        app.installEventFilter(_WATCHER)


def _top_windows():
    try:
        from PySide6.QtWidgets import QApplication
        return [w for w in QApplication.topLevelWidgets() if w.isWindow()]
    except Exception:
        return []


# ---------------------------------------------------------------------------------------------- title bar ----
_TITLE_FAILED = False


def set_title_bar(widget) -> None:
    """Dark or light Windows title bar to match the theme (DWMWA_USE_IMMERSIVE_DARK_MODE = 20, 19 on old
    Windows 10). Frameless windows (the call card) have none. Failure is logged once and ignored."""
    global _TITLE_FAILED
    if sys.platform != "win32" or _TITLE_FAILED:
        return
    try:
        if widget.windowFlags() & Qt.WindowType.FramelessWindowHint or not widget.testAttribute(
                Qt.WidgetAttribute.WA_WState_Created):
            return
        hwnd = ctypes.c_void_p(int(widget.winId()))
        dark = ctypes.c_int(0 if _T.pal.light else 1)
        dwm = ctypes.windll.dwmapi
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                return
        _TITLE_FAILED = True
        log.info("theme: title bar colour not supported by this Windows (DwmSetWindowAttribute)")
    except Exception:
        _TITLE_FAILED = True
        log.info("theme: title bar colour could not be set", exc_info=True)


# ------------------------------------------------------------------------------------------------ helpers ----
def set_tone(widget, role: str | None) -> None:
    """Text colour of a label / button by role, through the [tone] rules of the style sheets (follows a switch)."""
    widget.setProperty("tone", role or "")
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)
    widget.update()


def _rgba(hex_or_role: str, a: float) -> str:
    c = QColor(hex_or_role if hex_or_role.startswith("#") else color(hex_or_role))
    return f"rgba({c.red()},{c.green()},{c.blue()},{int(a)})"


def _tones(p: Palette, selector: str = "*") -> str:
    return "\n".join(f'{selector}[tone="{r}"] {{ color: {p[r]}; }}' for r in TONES)


# --------------------------------------------------------------------------------------------- style sheets ----
def qss_main(p: Palette | None = None) -> str:
    """Main window, settings dialog, extension editor (and the base of the developer tools)."""
    p = p or _T.pal
    return f"""
QWidget {{ background: {p.base}; color: {p.text}; font-family: "Segoe UI"; font-size: 13px; }}
QLabel {{ background: transparent; }}
QFrame#statusCard {{ background: {p.surface}; border: 1px solid {p.edge}; border-radius: 14px; }}
QLabel#statusTitle {{ font-size: 18px; font-weight: 700; }}
QLabel#statusHint {{ color: {p.subtext}; font-size: 12px; }}
QLabel#code {{ color: {p.subtext}; font-size: 11px; font-family: Consolas, "Segoe UI"; }}
QLabel#section {{ color: {p.subtext}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#version {{ color: {p.overlay}; font-size: 11px; }}
QLabel#hint {{ color: {p.subtext}; font-size: 12px; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ background: transparent; border: none; }}
QLineEdit {{ background: {p.mantle if not p.light else '#ffffff'}; border: 1px solid {p.surface2}; border-radius: 8px;
            padding: 6px 10px; }}
QLineEdit:focus {{ border-color: {p.blue}; }}
QPushButton {{ background: {p.surface2}; border: none; border-radius: 8px; padding: 7px 14px;
              font-family: "Segoe UI", "Segoe UI Emoji"; }}
QPushButton:hover {{ background: {p.overlay if not p.light else '#bcc0cc'}; }}
QPushButton:disabled {{ color: {p.overlay}; background: {p.surface if not p.light else p.mantle}; }}
QPushButton#seg {{ background: transparent; color: {p.subtext}; padding: 5px 12px; border-radius: 13px; }}
QPushButton#seg:hover {{ background: {p.surface if not p.light else p.mantle}; color: {p.text}; }}
QPushButton#seg:checked {{ background: {p.surface2}; color: {p.text}; font-weight: 600; }}
QPushButton#gear {{ background: transparent; color: {p.subtext}; padding: 6px; border-radius: 8px; }}
QPushButton#gear:hover {{ background: {p.surface2}; color: {p.text}; }}
QPushButton#done {{ background: {p.done_bg}; color: {p.green}; }}
QPushButton#primary {{ background: {p.blue}; color: {p.on_accent}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {p.blue_hover}; }}
QPushButton#danger {{ background: {p.danger_bg}; color: {p.red}; font-weight: 600; }}
QPushButton#danger:hover {{ background: {p.danger_hover}; }}
QComboBox {{ background: {p.surface2}; border: none; border-radius: 8px; padding: 6px 12px; min-width: 130px; }}
QComboBox:hover {{ background: {p.overlay if not p.light else '#bcc0cc'}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.surface2};
                               selection-background-color: {p.surface2}; selection-color: {p.text}; outline: none; }}
QCheckBox {{ color: {p.subtext}; spacing: 8px; }}
QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: {p.surface2}; }}
QCheckBox::indicator:checked {{ background: {p.blue}; }}
QTableWidget {{ background: {p.surface}; border: 1px solid {p.edge}; border-radius: 10px;
               gridline-color: {p.surface2 if not p.light else p.crust};
               selection-background-color: {p.surface2}; selection-color: {p.text}; }}
QHeaderView::section {{ background: {p.surface}; color: {p.subtext}; border: none; padding: 6px; font-size: 11px; }}
QFrame#form {{ background: {p.mantle}; border-radius: 10px; }}
QLabel#err {{ color: {p.red}; font-size: 11px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.surface2}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.overlay}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QToolTip {{ background: {p.tip_bg}; color: {p.tip_text}; border: none; padding: 4px 8px; border-radius: 6px; }}
{_tones(p)}
{qss_panel(p)}
"""


def qss_dev(p: Palette | None = None) -> str:
    """Developer tools = main + combo box, log view."""
    p = p or _T.pal
    return qss_main(p) + f"""
QFrame#card {{ background: {p.surface}; border: 1px solid {p.edge}; border-radius: 12px; }}
QComboBox {{ background: {p.mantle if not p.light else '#ffffff'}; border: 1px solid {p.surface2}; border-radius: 8px;
            padding: 6px 8px; }}
QComboBox QAbstractItemView {{ background: {p.surface}; selection-background-color: {p.surface2}; }}
QPlainTextEdit {{ background: {p.mantle}; border: none; border-radius: 10px; color: {p.subtext1};
                 font-family: Consolas, "Cascadia Mono", monospace; font-size: 11px; padding: 6px; }}
"""


def qss_menu(p: Palette | None = None) -> str:
    """Tray menu: same entries, only the colours."""
    p = p or _T.pal
    return f"""
QMenu {{ background: {p.base}; color: {p.text}; border: 1px solid {p.surface2}; padding: 4px;
        font-family: "Segoe UI"; font-size: 13px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 6px; background: transparent; }}
QMenu::item:selected {{ background: {p.surface2}; }}
QMenu::item:disabled {{ color: {p.disabled}; }}
QMenu::separator {{ height: 1px; background: {p.surface2}; margin: 4px 8px; }}
"""


def _card_buttons(p: Palette, scope: str = "") -> str:
    """Button rules of the call card; `scope` = "QFrame#callPanel " for the same buttons in the main window's call
    panel (v0.10.0-beta.3)."""
    b = (255, 255, 255) if not p.light else (76, 79, 105)
    btn = f"rgba({b[0]},{b[1]},{b[2]},{p.card_btn})"
    btn_h = f"rgba({b[0]},{b[1]},{b[2]},{p.card_btn_hover})"
    light, S = p.light, scope
    return f"""
{S}QPushButton {{ background:{btn}; border:none; border-radius:8px; color:{p.text};
              padding:7px 10px; font-family:'Segoe UI','Segoe UI Emoji'; font-size:12px; }}
{S}QPushButton:hover {{ background:{btn_h}; }}
{S}QPushButton:disabled {{ color:{p.disabled}; }}
{S}QPushButton#done {{ background:{_rgba(p.green, 55 if not light else 34)}; color:{p.green}; }}
{S}QPushButton#ctl {{ background:{_rgba(p.blue, 34 if not light else 23)}; }}
{S}QPushButton#ctl:hover {{ background:{_rgba(p.blue, 70 if not light else 46)}; }}
{S}QPushButton#ctl:checked {{ background:{_rgba(p.peach, 60 if not light else 36)}; color:{p.peach}; }}
{S}QPushButton#answer {{ background:{_rgba(p.green, 48 if not light else 31)}; color:{p.green}; font-weight:600; }}
{S}QPushButton#answer:hover {{ background:{_rgba(p.green, 95 if not light else 60)}; }}
{S}QPushButton#reject {{ background:{_rgba(p.red, 40 if not light else 26)}; color:{p.red}; font-weight:600; }}
{S}QPushButton#reject:hover {{ background:{_rgba(p.red, 90 if not light else 52)}; }}
{S}QPushButton#ext {{ background:{_rgba(p.mauve, 26 if not light else 20)}; text-align:left; padding:6px 9px;
                  font-size:12px; }}
{S}QPushButton#ext:hover {{ background:{_rgba(p.mauve, 70 if not light else 46)}; }}
{S}QPushButton#callback {{ background:{p.green}; color:{p.on_accent}; font-weight:600; min-height:18px; }}
{S}QPushButton#callback:hover {{ background:{p.green_hover}; }}
{S}QPushButton#callback:disabled {{ background:{_rgba(p.green, 70)}; color:{p.on_accent}; }}
{S}QPushButton#close {{ background:transparent; font-size:14px; padding:2px 6px; }}
{S}QPushButton#close:hover {{ background:{_rgba(p.red, 60 if not light else 40)}; }}
"""


def qss_panel(p: Palette | None = None) -> str:
    """v0.10.0-beta.3 call panel inside the main window (call_panel.CallPanel): labels + the card's buttons.
    Its background, border and accent bar are painted (paintEvent)."""
    p = p or _T.pal
    return _card_buttons(p, "QFrame#callPanel ") + f"""
QFrame#callPanel QLabel {{ background: transparent; }}
QLabel#pStatus {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLabel#pWhy {{ font-size: 11px; color: {p.dim}; }}
QLabel#pTimer {{ font-size: 13px; font-weight: 600; color: {p.blue}; }}
QLabel#pName {{ font-size: 20px; font-weight: 700; color: {p.text}; }}
QLabel#pTag {{ background: {_rgba(p.lavender, 38 if not p.light else 26)}; color: {p.lavender}; font-size: 12px;
              font-weight: 600; padding: 1px 8px; border-radius: 9px; }}
QLabel#pNumber {{ font-size: 12px; color: {p.dim}; }}
QLabel#pHint {{ font-size: 11px; color: {p.red}; }}
{_tones(p, "QLabel#pStatus")}
{_tones(p, "QLabel#pTimer")}
{_tones(p, "QLabel#pHint")}
"""


def qss_card(p: Palette | None = None) -> str:
    """Call card (overlay.CallOverlay). The card's own background, border and glow are painted (paintEvent)."""
    p = p or _T.pal
    return f"""
QLabel {{ color:{p.text}; background:transparent; font-family:'Segoe UI'; }}
#status {{ font-size:11px; font-weight:700; letter-spacing:1px; }}
#name {{ font-size:22px; font-weight:700; }}
#company {{ font-size:15px; font-weight:600; color:{p.subtext1}; }}
#number {{ font-size:12px; color:{p.dim}; }}
#timer {{ font-size:13px; font-weight:600; color:{p.blue}; }}
{_card_buttons(p)}
#hint {{ font-size:11px; color:{p.red}; }}
#empty {{ font-size:11px; color:{p.dim}; }}
QPushButton#busy {{ color:{p.dim}; }}
QToolTip {{ background: {p.tip_bg}; color: {p.tip_text}; border: none; padding: 4px 8px; border-radius: 6px; }}
{_tones(p)}
{_tones(p, "#hint")}
{_tones(p, "#timer")}
{_tones(p, "#status")}
"""


def check_palettes() -> list[str]:
    """Developer self-check: every field of both palettes is filled (used by tests / the stand-in check)."""
    bad = []
    for pal in (DARK, LIGHT):
        for f in fields(pal):
            if getattr(pal, f.name) in (None, ""):
                bad.append(f"{pal.name}.{f.name}")
    return bad

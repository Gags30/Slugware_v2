# Slugware: desktop countdown and alarm timers (PyQt5).
# The release version is VERSION below; keep MyAppVersion in Slugware.iss in sync.


APP_NAME = "Slugware"
VERSION = "2.0"

import sys
import ctypes
import json
import math
import shutil
import time
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from string import Template
import os
from PyQt5.QtCore import (
    Qt, QObject, QPoint, QPointF, QRectF, QSize, QTimer, QEvent, QLockFile, QByteArray, QTemporaryDir
)
from PyQt5.QtGui import QFont, QColor, QIcon, QPainter, QPalette, QPen, QPixmap, QFontDatabase, QFontMetrics
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QPushButton,
    QHBoxLayout, QLineEdit, QSpinBox, QDialog, QDialogButtonBox, QMessageBox,
    QStyle, QInputDialog, QFrame, QCheckBox, QRadioButton, QToolTip,
    QComboBox, QListWidget, QListWidgetItem, QMenu, QScrollArea, QGroupBox, QSizePolicy,
    QStyleOptionViewItem
)

# ───── Code vs. Data directories ─────
# CODE_DIR = where the code/executable lives (for assets like icons)
if getattr(sys, "frozen", False):
    CODE_DIR = Path(sys.executable).parent
else:
    CODE_DIR = Path(__file__).parent

def get_appdata_dir(app_name="Slugware"):
    """Return a per-user data directory for this app, cross-platform."""
    if sys.platform.startswith("win"):
        # %APPDATA%\Slugware  (Roaming)
        root = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / app_name
    elif sys.platform == "darwin":
        # ~/Library/Application Support/Slugware
        return Path.home() / "Library" / "Application Support" / app_name
    else:
        # Linux/BSD: $XDG_CONFIG_HOME/Slugware or ~/.config/Slugware
        xdg = os.getenv("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
        return root / app_name

DATA_DIR = get_appdata_dir(APP_NAME)
DATA_DIR.mkdir(parents=True, exist_ok=True)

TIMERS_FILE = DATA_DIR / "timers.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
ERROR_LOG_FILE = DATA_DIR / "error.log"

# At top of file, define your default icons in one place:
DEFAULT_ICONS = ["🔴","🟠","🟡","🟢","🔵","🟣","🟤","⚫","⚪"]

# Problems found with data files. Queued until the main window is up, then
# shown as they happen.
STARTUP_WARNINGS = []
_warnings_live = False


def _warn(msg):
    if _warnings_live:
        QTimer.singleShot(0, lambda: QMessageBox.warning(None, APP_NAME, msg))
    elif msg not in STARTUP_WARNINGS:
        STARTUP_WARNINGS.append(msg)


# ───── Data-file helpers ─────

def _retry_io(action, attempts=5):
    """
    Run action(), retrying briefly on OSError: antivirus or backup tools can
    hold a data file for a moment. Re-raises if it never succeeds.
    """
    for attempt in range(attempts):
        try:
            return action()
        except FileNotFoundError:
            raise
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


def _read_text(path):
    """A data file's text, or None if it doesn't exist. Raises OSError if it can't be read."""
    try:
        return _retry_io(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None

def _back_up(path, move):
    """
    Copy (or move) a damaged data file aside so a later save can't destroy it.
    Returns the backup path, or None if that failed.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")
    try:
        if move:
            os.replace(path, backup)
        else:
            shutil.copy2(path, backup)
        return backup
    except OSError:
        return None


def _set_aside_damaged(path):
    """
    Move a damaged data file aside and warn about it. Raises OSError if it
    couldn't be moved, so nothing goes on to save over it.
    """
    backup = _back_up(path, move=True)
    if backup is None:
        _warn(f"{path.name} is damaged and couldn't be backed up, so Slugware is "
              "using defaults for it and won't save over it.")
        raise OSError(f"{path.name} is damaged and couldn't be moved aside")
    _warn(f"{path.name} couldn't be read, so Slugware is using defaults for it."
          f"\n\nThe original was moved to:\n{backup}")


def _write_json(path, data):
    """Write via a temp file + rename so a crash mid-save can't leave a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _retry_io(lambda: os.replace(tmp, path))


def _read_settings():
    """
    Return settings.json as a dict: {} if it's missing, or if it was damaged
    (it's then set aside). Raises OSError if it exists but can't be read right
    now, so a save never mistakes a locked file for an empty one.
    """
    raw = _read_text(SETTINGS_FILE)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        _set_aside_damaged(SETTINGS_FILE)
        return {}
    return data


def _settings_for_loading():
    """_read_settings() for loaders, where a file that can't be read just means defaults."""
    try:
        return _read_settings()
    except OSError:
        _warn(f"{SETTINGS_FILE.name} couldn't be opened (another program may be using it), "
              "so Slugware is using default settings for now.")
        return {}


def _as_int(value, default, lo, hi):
    """value as an int clamped to [lo, hi], or default if it isn't a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return max(lo, min(hi, int(value)))
    except (ValueError, OverflowError):  # NaN / infinity
        return default


def _as_bool(value, default):
    return value if isinstance(value, bool) else default


def _as_str_list(value, default):
    """value as a list of unique, non-empty strings, or default if it isn't a list."""
    if not isinstance(value, list):
        return default
    return list(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))


def _clean_timer(entry):
    """
    Return a saved-timer dict with every field present and well-typed,
    or None if it's too broken to use.
    """
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    if entry.get("type") not in ("countdown", "alarm"):
        return None
    entry["filters"] = _as_str_list(entry.get("filters"), [])
    if not isinstance(entry.get("site"), str):
        entry["site"] = ""
    if entry["type"] == "countdown":
        entry["hours"]   = _as_int(entry.get("hours"),   0, 0, 23)
        entry["minutes"] = _as_int(entry.get("minutes"), 0, 0, 59)
    else:
        entry["alarm_hour"]   = _as_int(entry.get("alarm_hour"),   0, 0, 23)
        entry["alarm_minute"] = _as_int(entry.get("alarm_minute"), 0, 0, 59)
    return entry


def seconds_until(hour, minute):
    """
    Seconds from now until the next local hour:minute. Converting through
    timestamp() makes it DST-aware: naive datetime subtraction would be an
    hour off for an alarm set across a clock change.
    """
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, round(target.timestamp() - time.time()))


# Each pop-out's keys in settings.json: (keep it on top, its size and place)
POPOUT_KEYS = {"running": ("ontop", "popout_geometry"), "upcoming": ("upcoming_ontop", "upcoming_geometry")}
MAX_UPCOMING_HOURS = 8  # the most Settings lets the Upcoming card show


def load_settings():
    """
    Returns a tuple:
      (yellow_min, red_min, alert_popup, alert_sound,
       always_on_top, popup_x, popup_y, site_icons_list, upcoming_hours)
    always_on_top is {"running": bool, "upcoming": bool}, one per pop-out.
    Each value falls back to its default on its own if it's missing or bad.
    popup_x/popup_y are where the Running pop-out opens until it has a saved
    geometry (older versions set them in Settings).
    """
    # your app defaults:
    defaults = (30, 5, True, True, False, 100, 100, DEFAULT_ICONS.copy(), 3)
    data = _settings_for_loading()
    return (
        _as_int(data.get("yellow"),   defaults[0], 1, 1440),
        _as_int(data.get("red"),      defaults[1], 1, 1440),
        _as_bool(data.get("popup"),   defaults[2]),
        _as_bool(data.get("sound"),   defaults[3]),
        {kind: _as_bool(data.get(ontop_key), defaults[4]) for kind, (ontop_key, _) in POPOUT_KEYS.items()},
        _as_int(data.get("popup_x"),  defaults[5], -10000, 10000),
        _as_int(data.get("popup_y"),  defaults[6], -10000, 10000),
        # for sites, default to your DEFAULT_ICONS if none saved
        _as_str_list(data.get("sites"), defaults[7]),
        _as_int(data.get("upcoming_hours"), defaults[8], 1, MAX_UPCOMING_HOURS),
    )


def save_settings(
    yellow_min: int,
    red_min: int,
    alert_popup: bool,
    alert_sound: bool,
    always_on_top: dict,
    site_icons: list,
    upcoming_hours: int
):
    """
    Persist all settings into SETTINGS_FILE, merging with any other keys.
    """
    try:
        existing = _read_settings()

        # Overwrite just our keys (preserving e.g. 'filters')
        existing.update({
            "yellow":  yellow_min,
            "red":     red_min,
            "popup":   alert_popup,
            "sound":   alert_sound,
            "sites":   site_icons,
            "upcoming_hours": upcoming_hours
        })
        for kind, ontop in always_on_top.items():
            existing[POPOUT_KEYS[kind][0]] = ontop

        _write_json(SETTINGS_FILE, existing)
    except Exception as e:
        QMessageBox.critical(None, "Error", f"Failed to save settings:\n{e}")

def load_geometry(key):
    """A window geometry saved by save_geometries(), or None if there isn't a usable one."""
    value = _settings_for_loading().get(key)
    if not isinstance(value, str) or not value:
        return None
    try:
        return QByteArray.fromBase64(value.encode("ascii"))
    except UnicodeEncodeError:
        return None


def save_geometries(geometries):
    """
    Merge windows' saveGeometry() results ({settings key: QByteArray}) into
    SETTINGS_FILE, so they reopen where they were left.
    """
    try:
        existing = _read_settings()
        for key, geometry in geometries.items():
            existing[key] = bytes(geometry.toBase64()).decode("ascii")
        _write_json(SETTINGS_FILE, existing)
    except Exception:
        # Like sort prefs, non-critical; don't crash on a locked/read-only file.
        pass


# Columns each table can be sorted by, and the default (column, descending)
SORT_KEYS = {"running": ("name", "ends", "remaining"), "saved": ("name", "alarm", "status")}
SORT_DEFAULTS = {"running": ("remaining", False), "saved": ("alarm", False)}


def load_sort_prefs():
    """
    Returns {"running": (key, descending), "saved": (key, descending)},
    using SORT_DEFAULTS for a table with nothing valid saved.
    """
    existing = _settings_for_loading()
    prefs = {}
    for table, keys in SORT_KEYS.items():
        value = existing.get(f"{table}_sort")
        if (isinstance(value, list) and len(value) == 2
                and value[0] in keys and isinstance(value[1], bool)):
            prefs[table] = (value[0], value[1])
        else:
            prefs[table] = SORT_DEFAULTS[table]
    return prefs


def save_sort_pref(table, key, descending):
    """
    Update just one table's sort pref in SETTINGS_FILE.
    """
    try:
        existing = _read_settings()
        existing[f"{table}_sort"] = [key, descending]
        _write_json(SETTINGS_FILE, existing)
    except Exception:
        # Sort prefs are non-critical; don't crash on a locked/read-only file.
        pass


def load_filters():
    """
    Read the "filters" key from settings.json and return it as a list.
    Returns None if no filter list has been saved yet; an empty list means
    the user deleted them all.
    """
    data = _settings_for_loading()
    return _as_str_list(data.get("filters"), None)


def save_filters(filter_list):
    """
    Merge the given filter_list into settings.json under the "filters" key,
    preserving any other existing keys (like thresholds or alert preferences).
    """
    try:
        existing = _read_settings()
        existing["filters"] = filter_list
        _write_json(SETTINGS_FILE, existing)
    except Exception as e:
        QMessageBox.critical(None, "Error", f"Failed to save filters:\n{e}")


def save_site_icons(site_icons):
    """Merge just the "sites" key into settings.json."""
    try:
        existing = _read_settings()
        existing["sites"] = site_icons
        _write_json(SETTINGS_FILE, existing)
    except Exception as e:
        QMessageBox.critical(None, "Error", f"Failed to save site icons:\n{e}")


def save_popout_ontop(kind, ontop):
    """Merge just one pop-out's "keep on top" key into settings.json (its pin button)."""
    try:
        existing = _read_settings()
        existing[POPOUT_KEYS[kind][0]] = ontop
        _write_json(SETTINGS_FILE, existing)
    except Exception as e:
        QMessageBox.critical(None, "Error", f"Failed to save settings:\n{e}")


# ───── Look and feel ─────
# "Graphite": charcoal background, rounded cards and solid pill buttons.
BG       = "#141517"   # window background
PANEL    = "#1c1d20"   # cards
BORDER   = "#2a2c30"   # card outlines
LINE     = "#26282c"   # lines between rows, timeline gridlines
CONTROL  = "#2c2d32"   # buttons and count badges
HOVER    = "#36383e"
PRESSED  = "#404248"
INPUT    = "#232428"   # text boxes, lists and spin boxes in dialogs
EDGE     = "#34363b"   # their outlines
SELECT   = "#3a3c42"   # selected list items
TAG      = "#26272b"   # filter tags on saved rows
TEXT     = "#ececee"
MUTED    = "#a6a8ae"   # secondary text
FAINT    = "#8b8e95"   # placeholder text, focus outlines
DISABLED = "#6e7178"

# Colours for each urgency level: the marker dot, countdown text, progress bar,
# timeline label, and the pop-out's card for the next timer.
URGENCY = {
    "red": {
        "dot": "#ef6f63", "text": "#f28b7f", "track": "#3a2b29", "fill": "#f28b7f",
        "stem": "#ef6f63", "pill": "#33211f", "pill_text": "#f5a197",
        "card": "#261f1f", "card_border": "#4d2e2a", "card_sub": "#c4aeaa",
    },
    "yellow": {
        "dot": "#e2b45a", "text": "#e8c46f", "track": "#38331f", "fill": "#e8c46f",
        "stem": "#e2b45a", "pill": "#322a1a", "pill_text": "#eccc84",
        "card": "#25221b", "card_border": "#4a3f26", "card_sub": "#c9bd9f",
    },
    "normal": {
        "dot": TEXT, "text": TEXT, "track": "#2e3034", "fill": "#c9cacf",
        "stem": "#4a4c52", "pill": CONTROL, "pill_text": TEXT,
        "card": PANEL, "card_border": BORDER, "card_sub": MUTED,
    },
}

# The default site icons are coloured circle emoji. Qt 5 draws emoji in
# monochrome, so those are drawn as dots in these colours instead.
SITE_DOTS = {"🔴": "#ef6f63", "🟠": "#f0a35e", "🟡": "#e8c46f", "🟢": "#5fbf7a", "🔵": "#6aa3f5",
             "🟣": "#b08cf0", "🟤": "#a87c5f", "⚫": "#0b0c0d", "⚪": "#ececee"}

FONT_FAMILY = "Segoe UI"   # becomes "Inter" once the bundled fonts load

_STYLESHEET = """
QToolTip { background: $CONTROL; color: $TEXT; border: 1px solid $BORDER; padding: 4px 8px; }

QFrame#Card { background: $PANEL; border: 1px solid $BORDER; border-radius: 20px; }
QFrame#CardHeader { background: transparent; border: 0; border-bottom: 1px solid $LINE; }
QWidget#RowsHost { background: transparent; }
QScrollArea { background: transparent; border: 0; }
QLabel#Badge { background: $CONTROL; color: $TEXT; border-radius: 10px; padding: 0 8px; }
QLabel#RunningPill { background: #1e2a22; color: #8fd3a2; border-radius: 11px; padding: 0 10px; }

QPushButton {
    background: $CONTROL; color: $TEXT; border: 1px solid transparent;
    border-radius: 8px; padding: 5px 14px;
}
QPushButton:hover { background: $HOVER; }
QPushButton:pressed { background: $PRESSED; }
QPushButton:focus { border-color: $FAINT; }
QPushButton:disabled { background: $INPUT; color: $DISABLED; }
QPushButton[variant="primary"], QPushButton:default, QPushButton:checked { background: $TEXT; color: $BG; }
QPushButton[variant="primary"]:hover, QPushButton:default:hover, QPushButton:checked:hover { background: #ffffff; }
QPushButton[variant="primary"]:pressed, QPushButton:default:pressed, QPushButton:checked:pressed { background: #d4d5d9; }
QPushButton[variant="primary"]:disabled { background: $CONTROL; color: $DISABLED; }
QPushButton[pill="40"] { min-height: 38px; max-height: 38px; border-radius: 20px; padding: 0 16px; }
QPushButton[pill="34"] { min-height: 32px; max-height: 32px; border-radius: 17px; padding: 0 14px; }
QPushButton[pill="32"] { min-height: 30px; max-height: 30px; border-radius: 16px; padding: 0 12px; }
QPushButton[pill="24"] { min-height: 22px; max-height: 22px; border-radius: 12px; padding: 0 10px; }
QPushButton[icononly="true"] { padding: 0; }
QPushButton[dropdown="true"] { padding-right: 32px; }
QPushButton::menu-indicator { image: none; width: 0; }
QPushButton[filtering="true"] { background: $TEXT; color: $BG; }
QPushButton[filtering="true"]:hover { background: #ffffff; }

QMenu { background: $PANEL; color: $TEXT; border: 1px solid $EDGE; padding: 6px; }
QMenu#FilterMenu { border-radius: 12px; }
QMenu::item { padding: 6px 16px 6px 32px; border-radius: 6px; background: transparent; }
QMenu::item:selected { background: $SELECT; }
QMenu::item:disabled { color: $DISABLED; }
QMenu::separator { height: 1px; background: $LINE; margin: 5px 6px; }
QMenu::indicator {
    width: 14px; height: 14px; left: 9px; border: 1px solid #5a5d63; border-radius: 4px; background: $INPUT;
}
QMenu::indicator:checked { background: $TEXT; border-color: $TEXT; image: url($IMAGES/check.png); }

QLineEdit, QSpinBox, QComboBox {
    background: $INPUT; color: $TEXT; border: 1px solid $EDGE; border-radius: 8px;
    padding: 4px 8px; selection-background-color: $SELECT;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: $FAINT; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: $DISABLED; }
QComboBox QAbstractItemView {
    background: $INPUT; color: $TEXT; border: 1px solid $EDGE;
    selection-background-color: $SELECT; outline: 0;
}
QListWidget { background: $INPUT; border: 1px solid $EDGE; border-radius: 8px; padding: 4px; outline: 0; }
QListWidget::item { padding: 3px 4px; border-radius: 6px; }
QListWidget::item:hover { background: #2a2b30; }
QListWidget::item:selected { background: $SELECT; color: $TEXT; }

QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 18px; border: 0; border-radius: 4px; background: transparent;
}
QSpinBox::up-button { subcontrol-position: top right; margin: 3px 3px 0 0; }
QSpinBox::down-button { subcontrol-position: bottom right; margin: 0 3px 3px 0; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: $HOVER; }
QSpinBox::up-arrow { image: url($IMAGES/up.png); }
QSpinBox::down-arrow { image: url($IMAGES/down.png); }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: 0; }
QComboBox::down-arrow { image: url($IMAGES/down.png); }

QGroupBox {
    background: $PANEL; border: 1px solid $BORDER; border-radius: 12px;
    margin-top: 22px; padding-top: 6px;
}
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 6px; top: 2px; color: $MUTED; }
QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator, QAbstractItemView::indicator {
    width: 14px; height: 14px; border: 1px solid #5a5d63; background: $INPUT;
}
QCheckBox::indicator, QAbstractItemView::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover, QAbstractItemView::indicator:hover { border-color: $FAINT; }
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {
    background: $TEXT; border-color: $TEXT; image: url($IMAGES/check.png);
}
QRadioButton::indicator:checked {
    border-color: $TEXT;
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
                                stop: 0 $TEXT, stop: 0.45 $TEXT, stop: 0.55 $INPUT, stop: 1 $INPUT);
}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { border-color: $SELECT; }
QCheckBox::indicator:checked:disabled { background: $DISABLED; border-color: $DISABLED; }
QRadioButton::indicator:checked:disabled {
    border-color: $DISABLED;
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
                                stop: 0 $DISABLED, stop: 0.45 $DISABLED, stop: 0.55 $INPUT, stop: 1 $INPUT);
}

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 2px 2px 2px; }
QScrollBar::handle:vertical { background: $SELECT; border-radius: 3px; min-height: 28px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px 2px 2px 2px; }
QScrollBar::handle:horizontal { background: $SELECT; border-radius: 3px; min-width: 28px; }
QScrollBar::handle:hover { background: #4a4d55; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
"""


def load_fonts():
    """Register the bundled Inter fonts (in fonts/ next to the code or exe)."""
    global FONT_FAMILY
    ids = [QFontDatabase.addApplicationFont(str(p))
           for p in sorted((CODE_DIR / "fonts").glob("Inter-*.ttf"))]
    if any(i != -1 for i in ids):
        FONT_FAMILY = "Inter"


def apply_theme(app):
    """The dark look for every window: Fusion style, a dark palette and _STYLESHEET."""
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: INPUT,
        QPalette.AlternateBase: PANEL, QPalette.Text: TEXT, QPalette.Button: CONTROL,
        QPalette.ButtonText: TEXT, QPalette.BrightText: "#ffffff", QPalette.Highlight: SELECT,
        QPalette.HighlightedText: TEXT, QPalette.ToolTipBase: CONTROL, QPalette.ToolTipText: TEXT,
        QPalette.Link: TEXT, QPalette.PlaceholderText: FAINT, QPalette.Light: "#3a3c42",
        QPalette.Midlight: "#303237", QPalette.Mid: LINE, QPalette.Dark: "#101113",
        QPalette.Shadow: "#000000",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(DISABLED))
    app.setPalette(pal)
    app.setFont(ui_font(13))
    app.setStyleSheet(Template(_STYLESHEET).substitute(
        BG=BG, PANEL=PANEL, BORDER=BORDER, LINE=LINE, CONTROL=CONTROL, HOVER=HOVER,
        PRESSED=PRESSED, INPUT=INPUT, EDGE=EDGE, SELECT=SELECT, TEXT=TEXT, FAINT=FAINT,
        MUTED=MUTED, DISABLED=DISABLED, IMAGES=_write_theme_images()))
    if sys.platform == "win32":
        global _title_bars
        _title_bars = TitleBarTheme(app)
        app.installEventFilter(_title_bars)


_theme_images = None  # the folder of stylesheet images; deleted when the app exits
_title_bars = None    # the TitleBarTheme watching for windows to colour


class TitleBarTheme(QObject):
    """
    Colours the title bar of every window and dialog to match the app: on
    Windows 11 the window background, light text and the card-edge border;
    on Windows 10, just its dark title bar. Done as each window is shown,
    before it's drawn, so there's no flash of the light default. Changing a
    window's flags (the pop-outs' pin) makes it a new native window, which is
    shown and coloured again.
    """
    # DwmSetWindowAttribute settings
    DARK_MODE, DARK_MODE_OLD = 20, 19   # the second before Windows 10 20H1
    BORDER_COLOR, CAPTION_COLOR, TEXT_COLOR = 34, 35, 36   # Windows 11 only

    def __init__(self, parent):
        super().__init__(parent)
        self._set = ctypes.windll.dwmapi.DwmSetWindowAttribute
        self._set.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        self._set.restype = ctypes.c_long

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Show and obj.isWidgetType() and obj.isWindow()
                and obj.windowType() in (Qt.Window, Qt.Dialog)):
            self._color(int(obj.winId()))
        return False

    def _attribute(self, hwnd, attribute, value):
        value = ctypes.c_uint(value)
        return self._set(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0

    def _color(self, hwnd):
        # Dark mode gives the minimise/maximise/close buttons light symbols
        if not self._attribute(hwnd, self.DARK_MODE, 1):
            self._attribute(hwnd, self.DARK_MODE_OLD, 1)
        for attribute, color in ((self.CAPTION_COLOR, BG), (self.TEXT_COLOR, TEXT),
                                 (self.BORDER_COLOR, BORDER)):
            c = QColor(color)  # as a COLORREF, 0x00BBGGRR
            self._attribute(hwnd, attribute, c.red() | c.green() << 8 | c.blue() << 16)


def _write_theme_images():
    """
    Draw the images the stylesheet needs (check mark, spin box and combo box
    arrows) into a temporary folder, with @2x/@3x copies for scaled displays.
    Returns the folder for use in url().
    """
    global _theme_images
    _theme_images = QTemporaryDir()
    folder = Path(_theme_images.path())
    for name, color, size, stroke in (("check", BG, 12, 3.5), ("up", TEXT, 10, 2.5), ("down", TEXT, 10, 2.5)):
        for scale, suffix in ((1, ""), (2, "@2x"), (3, "@3x")):
            _icon_pixmap(name, color, size * scale, stroke).save(str(folder / f"{name}{suffix}.png"))
    return folder.as_posix()


def ui_font(px, weight=QFont.Normal):
    font = QFont(FONT_FAMILY)
    font.setPixelSize(px)
    font.setWeight(weight)
    return font


# Line icons on a 24×24 grid, stroked in the colour they're made with.
_ICON_PATHS = {
    "popout":   '<path d="M15 3h6v6"/><path d="M10 14 21 3"/>'
                '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "settings": '<path d="M4 21v-7"/><path d="M4 10V3"/><path d="M12 21v-9"/><path d="M12 8V3"/>'
                '<path d="M20 21v-5"/><path d="M20 12V3"/><path d="M1 14h6"/><path d="M9 8h6"/>'
                '<path d="M17 16h6"/>',
    "plus":     '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "x":        '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "edit":     '<path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    "check":    '<path d="M20 6 9 17l-5-5"/>',
    "filter":   '<path d="M3 6h18"/><path d="M7 12h10"/><path d="M10 18h4"/>',
    "up":       '<path d="m18 15-6-6-6 6"/>',
    "down":     '<path d="m6 9 6 6 6-6"/>',
    "pin":      '<path d="M12 17v5"/><path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 '
                '1 1h12a1 1 0 0 0 1-1v-.8a2 2 0 0 0-1.1-1.8l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 '
                '0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>',
    "play":     '<path fill="currentColor" stroke="none" d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 '
                '0-1.72L8.5 3.64A1 1 0 0 0 7 4.5z"/>',
}


def _icon_pixmap(name, color, px, stroke):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="currentColor" stroke-width="{stroke}" stroke-linecap="round" '
           f'stroke-linejoin="round">{_ICON_PATHS[name]}</svg>').replace("currentColor", color)
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    return pixmap


def make_icon(name, color, size=14, stroke=2.0, checked_color=None):
    """An icon from _ICON_PATHS, sharp at any display scale. checked_color
    gives a checkable button a second colour for when it's on."""
    icon = QIcon()
    for scale in (1, 2, 3):
        icon.addPixmap(_icon_pixmap(name, color, size * scale, stroke), QIcon.Normal, QIcon.Off)
        if checked_color:
            icon.addPixmap(_icon_pixmap(name, checked_color, size * scale, stroke), QIcon.Normal, QIcon.On)
    return icon


def pill_button(text="", icon=None, height=34, primary=False, tooltip="", icon_size=14, stroke=2.0,
                cls=QPushButton):
    """A solid, fully rounded button; icon-only when there's no text."""
    btn = cls(f" {text}" if icon and text else text)
    btn.setProperty("pill", str(height))
    btn.setFont(ui_font(12 if height <= 32 else 13, QFont.DemiBold if primary else QFont.Medium))
    if primary:
        btn.setProperty("variant", "primary")
    if icon:
        btn.setIcon(make_icon(icon, BG if primary else TEXT, icon_size, stroke))
        btn.setIconSize(QSize(icon_size, icon_size))
    if not text:
        btn.setProperty("icononly", "true")
        btn.setFixedWidth(height)
    if tooltip:
        btn.setToolTip(tooltip)
        btn.setAccessibleName(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFocusPolicy(Qt.TabFocus)  # a click doesn't leave a focus outline behind
    return btn


def make_label(text="", px=13, weight=QFont.Normal, color=None):
    lbl = QLabel(text)
    lbl.setFont(ui_font(px, weight))
    if color:
        set_color(lbl, color)
    return lbl


def make_badge():
    """The count next to a card's title."""
    badge = QLabel()
    badge.setObjectName("Badge")
    badge.setFont(ui_font(12, QFont.Medium))
    badge.setFixedHeight(20)
    badge.setAlignment(Qt.AlignCenter)
    return badge


def set_color(widget, color):
    pal = widget.palette()
    pal.setColor(QPalette.WindowText, QColor(color))
    widget.setPalette(pal)


def format_hms(secs):
    """0:03:41"""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_short(secs):
    """3:41 under an hour, 1:12:41 from an hour up."""
    return format_hms(secs) if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"


def format_minutes(minutes):
    """30 min, 1 h, 1 h 30 min"""
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} min"
    return f"{h} h {m} min" if m else f"{h} h"


def saved_time_text(entry):
    """A saved timer's alarm time, or the length of an older countdown entry."""
    if entry["type"] == "alarm":
        return f"{entry.get('alarm_hour', 0):02d}:{entry.get('alarm_minute', 0):02d}"
    h, m = entry.get("hours", 0), entry.get("minutes", 0)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# Table columns as (width, stretch): stretch 0 is a fixed width, otherwise
# width is the minimum and the leftover space is shared by stretch.
COL_GAP = 14
ROW_MARGINS = (20, 0, 16, 0)
RUNNING_COLUMNS = [(160, 2), (56, 0), (120, 0), (60, 1), (110, 0), (32, 0)]
SAVED_COLUMNS = [(160, 4), (130, 3), (64, 0), (100, 0), (112, 0)]
VISIBLE_ROWS = 5  # each table shows this many rows, and scrolls for more


def lay_out_cells(row, cells, columns):
    """
    Add a table row's cells to its layout. Stretching cells ignore what
    their contents would like, so every row (and the headings) line up.
    """
    for cell, (width, stretch) in zip(cells, columns):
        if stretch:
            cell.setMinimumWidth(width)
            cell.setSizePolicy(QSizePolicy.Ignored, cell.sizePolicy().verticalPolicy())
        else:
            cell.setFixedWidth(width)
        row.addWidget(cell, stretch)


class ElidedLabel(QLabel):
    """One line of text that ends in "…" when it doesn't fit (hover for the whole thing)."""
    def __init__(self, text="", px=14, weight=QFont.Medium):
        super().__init__(text)
        self.setFont(ui_font(px, weight))
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.WindowText))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width())
        painter.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, text)

    def event(self, e):
        if e.type() == QEvent.ToolTip:
            if self.fontMetrics().horizontalAdvance(self.text()) > self.width():
                QToolTip.showText(e.globalPos(), self.text(), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)


class SiteMark(QWidget):
    """
    A timer's site, shown before its name: a coloured dot for the default
    circle icons, a hollow ring when it has none, otherwise the site's text.
    """
    MAX_TEXT_WIDTH = 90

    def __init__(self, dot=8):
        super().__init__()
        self.dot = dot
        self.site = None
        self.setFont(ui_font(13))
        self.set_site("")

    def _dot_color(self):
        return SITE_DOTS.get(self.site.replace("️", ""))

    def set_site(self, site):
        if site == self.site:
            return
        self.site = site
        if not site or self._dot_color():
            self.setFixedWidth(self.dot)
        else:
            self.setFixedWidth(min(self.MAX_TEXT_WIDTH, self.fontMetrics().horizontalAdvance(site)))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self._dot_color()
        if self.site and not color:
            painter.setPen(QColor(MUTED))
            text = self.fontMetrics().elidedText(self.site, Qt.ElideRight, self.width())
            painter.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, text)
            return
        rect = QRectF(0, (self.height() - self.dot) / 2, self.dot, self.dot)
        if not self.site:
            painter.setPen(QPen(QColor(DISABLED), 1.5))
            painter.drawEllipse(rect.adjusted(0.75, 0.75, -0.75, -0.75))
        else:
            # A black dot needs an outline to show up on the dark background
            painter.setPen(QPen(QColor(DISABLED), 1) if color == SITE_DOTS["⚫"] else Qt.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(rect)


class TagStrip(QWidget):
    """A saved timer's filters as small tags, ending in "+N" when they don't all fit."""
    def __init__(self, names):
        super().__init__()
        self.names = names
        self.setFont(ui_font(12))
        self.setFixedHeight(22)
        if names:
            self.setToolTip(", ".join(names))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        fm = self.fontMetrics()
        tag_width = lambda text: fm.horizontalAdvance(text) + 18
        x = 0
        for i, name in enumerate(self.names):
            left = len(self.names) - i - 1
            # Room to keep for a "+N" tag after this one, if any are left
            reserve = tag_width(f"+{left}") + 4 if left else 0
            text = name
            if x + tag_width(text) + reserve > self.width():
                if i:  # the previous tag kept room for this
                    self._tag(painter, x, f"+{left + 1}")
                    break
                text = fm.elidedText(name, Qt.ElideRight, max(0, self.width() - reserve - 18))
            x += self._tag(painter, x, text) + 4

    def _tag(self, painter, x, text):
        width = self.fontMetrics().horizontalAdvance(text) + 18
        rect = QRectF(x, 0, width, self.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(TAG))
        painter.drawRoundedRect(rect, 11, 11)
        painter.setPen(QColor("#c9cacf"))
        painter.drawText(rect, Qt.AlignCenter, text)
        return width


class ProgressBar(QWidget):
    """A thin bar showing how much of a timer is left."""
    def __init__(self):
        super().__init__()
        self.setFixedHeight(4)
        self._state = None

    def set_value(self, fraction, track, fill):
        state = (round(max(0.0, min(1.0, fraction)) * 1000), track, fill)
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, event):
        if self._state is None:
            return
        permille, track, fill = self._state
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(track))
        painter.drawRoundedRect(QRectF(self.rect()), 2, 2)
        if permille:
            painter.setBrush(QColor(fill))
            width = max(4.0, self.width() * permille / 1000)
            painter.drawRoundedRect(QRectF(0, 0, width, self.height()), 2, 2)


class DropdownButton(QPushButton):
    """
    A button that opens its menu, with a chevron drawn in its right padding.
    (Qt's own menu arrow takes its room out of the text, which then gets cut off.)
    """
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(self.palette().color(QPalette.ButtonText), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x, y = self.width() - 22, self.height() / 2
        painter.drawPolyline(QPointF(x - 3.5, y - 1.75), QPointF(x, y + 1.75), QPointF(x + 3.5, y - 1.75))


class FilterMenu(QMenu):
    """
    The Saved card's filter dropdown. Ticking a filter leaves the menu open,
    so several can be picked in one go; anything else closes it as usual.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FilterMenu")
        # See-through behind the rounded corners
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setMinimumWidth(180)

    def _toggles(self, action):
        return action is not None and action.isCheckable() and action.isEnabled()

    def mouseReleaseEvent(self, event):
        action = self.actionAt(event.pos())
        if self._toggles(action):
            action.trigger()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and self._toggles(self.activeAction()):
            self.activeAction().trigger()
            return
        super().keyPressEvent(event)


class SortHeading(QLabel):
    """A column heading that sorts the table when clicked, with ↑ or ↓ when it's the sort column."""
    def __init__(self, text, key, on_click):
        super().__init__(text)
        self.base_text = text
        self.key = key
        self.on_click = on_click
        self.setFont(ui_font(12, QFont.Medium))
        set_color(self, MUTED)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"Sort by {text.lower()}")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.on_click(self.key)

    def show_sort(self, key, descending):
        arrow = (" ↓" if descending else " ↑") if key == self.key else ""
        self.setText(self.base_text + arrow)


class RowList(QScrollArea):
    """
    A table card's rows: the height of VISIBLE_ROWS of them however many
    there are (at least that, if it grows into spare room), scrolling past
    that. Shows a placeholder line when empty.
    """
    def __init__(self, row_height, empty_text, grows=False):
        super().__init__()
        self.items = []
        if grows:
            self.setMinimumHeight(VISIBLE_ROWS * row_height)
        else:
            self.setFixedHeight(VISIBLE_ROWS * row_height)
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        host = QWidget()
        host.setObjectName("RowsHost")
        self.rows = QVBoxLayout(host)
        self.rows.setContentsMargins(*ROW_MARGINS)
        self.rows.setSpacing(0)
        self.placeholder = make_label(empty_text, 13, color=MUTED)
        self.placeholder.setFixedHeight(row_height)
        self.rows.addWidget(self.placeholder)
        self.rows.addStretch()
        self.setWidget(host)
        # Keep the rows' right edge under the headings' whether or not the scroll bar shows
        self.verticalScrollBar().rangeChanged.connect(self._fit_gutter)

    def _fit_gutter(self, _minimum, maximum):
        left, top, right, bottom = ROW_MARGINS
        bar = self.verticalScrollBar().sizeHint().width() if maximum > 0 else 0
        self.rows.setContentsMargins(left, top, max(0, right - bar), bottom)

    def add(self, row):
        self.items.append(row)
        self.rows.insertWidget(self.rows.count() - 1, row)
        self._changed()

    def take(self, row):
        self.items.remove(row)
        self.rows.removeWidget(row)
        row.hide()
        self._changed()

    def clear(self):
        for row in self.items:
            self.rows.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.items = []
        self._changed()

    def reorder(self, rows):
        for row in rows:
            self.rows.removeWidget(row)
            self.rows.insertWidget(self.rows.count() - 1, row)
        self.items = list(rows)

    def _changed(self):
        self.placeholder.setVisible(not self.items)


class TableCard(QFrame):
    """A rounded card holding a table: a title row, column headings and the rows."""
    def __init__(self, title, columns, headings, row_height, empty_text, on_sort, grows=False):
        super().__init__()
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if grows else QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("CardHeader")
        self.header_row = QHBoxLayout(header)
        self.header_row.setContentsMargins(20, 9, 16, 9)
        self.header_row.setSpacing(12)
        title_box = QWidget()
        title_box.setFixedHeight(34)
        title_row = QHBoxLayout(title_box)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)
        title_row.addWidget(make_label(title, 15, QFont.DemiBold))
        self.badge = make_badge()
        title_row.addWidget(self.badge)
        self.header_row.addWidget(title_box, 0, Qt.AlignTop)
        layout.addWidget(header)

        heads = QWidget()
        heads.setFixedHeight(32)
        heads_row = QHBoxLayout(heads)
        heads_row.setContentsMargins(*ROW_MARGINS)
        heads_row.setSpacing(COL_GAP)
        self.headings = []
        cells = []
        for text, key in headings:
            if key:
                heading = SortHeading(text, key, on_sort)
                self.headings.append(heading)
            else:
                heading = make_label(text, 12, QFont.Medium, MUTED)
            cells.append(heading)
        lay_out_cells(heads_row, cells, columns)
        layout.addWidget(heads)

        self.list = RowList(row_height, empty_text, grows)
        layout.addWidget(self.list)

    def show_sort(self, key, descending):
        for heading in self.headings:
            heading.show_sort(key, descending)


def _row_line(widget):
    """The line above each table row."""
    QPainter(widget).fillRect(0, 0, widget.width(), 1, QColor(LINE))


class SavedRow(QWidget):
    """A saved timer: one row of the Saved card."""
    HEIGHT = 52

    def __init__(self, entry, main_win):
        super().__init__()
        self.entry = entry
        self.setFixedHeight(self.HEIGHT)

        name_cell = QWidget()
        name_row = QHBoxLayout(name_cell)
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(10)
        mark = SiteMark()
        mark.set_site(entry.get("site", ""))
        name_row.addWidget(mark)
        name_row.addWidget(ElidedLabel(entry["name"]), 1)

        tags = TagStrip(entry.get("filters", []))
        when = make_label(saved_time_text(entry), 15)

        status = QWidget()
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(0, 0, 0, 0)
        self.running_pill = QLabel('<span style="color:#5fbf7a">●</span>&nbsp;Running')
        self.running_pill.setObjectName("RunningPill")
        self.running_pill.setFont(ui_font(12, QFont.Medium))
        self.running_pill.setFixedHeight(22)
        self.idle = make_label("Idle", 13, color=MUTED)
        status_row.addWidget(self.running_pill)
        status_row.addWidget(self.idle)
        status_row.addStretch()

        actions = QWidget()
        actions_row = QHBoxLayout(actions)
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(6)
        actions_row.addStretch()
        self.btn_start = pill_button(icon="play", height=32, primary=True,
                                     tooltip="Start this timer", icon_size=12)
        btn_edit = pill_button(icon="edit", height=32, tooltip="Edit this timer")
        btn_delete = pill_button(icon="x", height=32, tooltip="Delete this timer", stroke=2.2)
        self.btn_start.clicked.connect(lambda: main_win._start_saved_timer(entry))
        btn_edit.clicked.connect(lambda: main_win._edit_saved_timer(entry))
        btn_delete.clicked.connect(lambda: main_win._delete_saved_timer(entry))
        for btn in (self.btn_start, btn_edit, btn_delete):
            actions_row.addWidget(btn)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(COL_GAP)
        lay_out_cells(row, [name_cell, tags, when, status, actions], SAVED_COLUMNS)
        self.set_running(False)

    def set_running(self, running):
        self.running_pill.setVisible(running)
        self.idle.setVisible(not running)
        self.btn_start.setVisible(not running)

    def paintEvent(self, event):
        _row_line(self)


class RunningRow(QWidget):
    """
    A running timer: one row of the Running card. It counts down against the
    wall clock, so sleep or a busy UI can't make it run late.
    """
    HEIGHT = 52

    def __init__(self, entry, total_seconds, main_win):
        super().__init__()
        self.entry = entry
        self.main_win = main_win
        # Whole seconds, so every timer ticks over at the same moment
        self.end_time = round(time.time() + total_seconds)
        self.total = total_seconds  # for the progress bar; ±1m changes it too
        self.remaining = total_seconds
        self._level = None
        self.setFixedHeight(self.HEIGHT)

        name_cell = QWidget()
        name_row = QHBoxLayout(name_cell)
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(10)
        self.mark = SiteMark()
        self.name = ElidedLabel()
        name_row.addWidget(self.mark)
        name_row.addWidget(self.name, 1)

        self.ends = make_label("", 14)
        self.time = make_label("", 24)
        font = self.time.font()
        font.setLetterSpacing(QFont.AbsoluteSpacing, -0.5)
        self.time.setFont(font)
        # Fixed sizes, so the per-second text changes don't re-lay out the window
        for lbl in (self.ends, self.time):
            lbl.setFixedHeight(self.HEIGHT)
        self.bar = ProgressBar()

        adjust = QWidget()
        adjust_row = QHBoxLayout(adjust)
        adjust_row.setContentsMargins(0, 0, 0, 0)
        adjust_row.setSpacing(6)
        self.btn_less = pill_button("−1m", height=32, tooltip="Take a minute off")
        btn_more = pill_button("+1m", height=32, tooltip="Add a minute")
        self.btn_less.clicked.connect(lambda: self.adjust(-60))
        btn_more.clicked.connect(lambda: self.adjust(60))
        adjust_row.addWidget(self.btn_less)
        adjust_row.addWidget(btn_more)
        adjust_row.addStretch()

        btn_cancel = pill_button(icon="x", height=32, tooltip="Cancel this timer", stroke=2.2)
        btn_cancel.clicked.connect(self.cancel)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(COL_GAP)
        lay_out_cells(row, [name_cell, self.ends, self.time, self.bar, adjust, btn_cancel], RUNNING_COLUMNS)
        self.show_entry()
        self.refresh(time.time())

    def show_entry(self):
        """Show the entry's name and site (on start, and after it's edited)."""
        self.mark.set_site(self.entry.get("site", ""))
        self.name.setText(self.entry["name"])

    def refresh(self, now):
        """Update the countdown for the time now; remaining hits 0 when it's done."""
        self.remaining = max(0, math.ceil(self.end_time - now))
        level = self.main_win.urgency(self.remaining)
        colors = URGENCY[level]
        if level != self._level:
            self._level = level
            set_color(self.time, colors["text"])
        self.time.setText(format_hms(self.remaining))
        self.ends.setText(datetime.fromtimestamp(self.end_time).strftime("%H:%M"))
        self.bar.set_value(self.remaining / self.total, colors["track"], colors["fill"])
        # Taking off the last minute would end it on the spot
        self.btn_less.setEnabled(self.remaining > 60)

    def adjust(self, seconds):
        self.end_time += seconds
        self.total = max(1, self.total + seconds)
        self.main_win._timer_adjusted(self)

    def cancel(self):
        self.main_win._finish_running_timer(self)

    def paintEvent(self, event):
        _row_line(self)


def upcoming_title(later):
    """The Upcoming card's title, with how many timers end after the time it shows."""
    extra = f'<span style="color:{MUTED}">&nbsp;&nbsp;·&nbsp;&nbsp;{later} later</span>' if later else ""
    return f"Upcoming{extra}"


class Timeline(QWidget):
    """
    The coming hours (as many as Settings says), with a marker for every
    running timer that ends in them. Hovering a marker lists the timers there
    in a TimelineCard, for the ones whose labels didn't fit. In the pop-out
    (stretches=True) a wider window shows more time, and a taller one has
    more rows for labels.
    """
    BASE_WIDTH = 600   # a stretching timeline this wide shows the hours from Settings
    LANE = 24          # from one row of labels to the next
    LABEL_HEIGHT = 22
    HOVER_REACH = 12   # how far either side of the pointer a marker counts as hovered

    def __init__(self, main_win, stretches=False):
        super().__init__()
        self.main_win = main_win
        self.stretches = stretches
        if stretches:
            self.setMinimumHeight(96)
        else:
            self.setFixedHeight(96)
        self.setMinimumWidth(240)
        self.setMouseTracking(True)
        self._pointer = None      # where the mouse is, while it's over the timeline
        self._label_rects = []    # (rect, x of its marker) for each label drawn
        self._card = None

    def refresh(self):
        """Redraw for the time now (every second), keeping an open card up to date."""
        self.update()
        self._update_card()

    def mouseMoveEvent(self, event):
        self._pointer = QPointF(event.pos())
        self.refresh()

    def leaveEvent(self, event):
        self._pointer = None
        self.refresh()

    def hideEvent(self, event):
        self._pointer = None
        self._update_card()

    def span(self):
        """How many seconds ahead it shows (never more than a day)."""
        span = self.main_win.upcoming_hours * 3600
        if self.stretches:
            span *= self.width() / self.BASE_WIDTH
        return min(span, 24 * 3600)

    def later(self, now):
        """How many running timers end after the time it shows."""
        span = self.span()
        return sum(1 for row in self.main_win.active_running if row.end_time - now > span)

    def _axis(self):
        """y of the axis line, with the times below it."""
        return self.height() - 36

    def _lanes(self):
        """Tops of the rows of labels above the axis, the one nearest it first."""
        return list(range(self._axis() - 36, -1, -self.LANE))

    @staticmethod
    def _grid_step(hour_width):
        """
        Minutes between gridlines when hours are hour_width apart: each hour,
        each half or quarter hour when there's plenty of room, or each few
        hours when there isn't room for every hour's time.
        """
        for minutes in (15, 30):
            if hour_width * minutes / 60 >= 150:
                return minutes
        return next((m for m in (60, 120, 180, 240, 360) if hour_width * m / 60 >= 56), 720)

    def _markers(self, now):
        """(row, x) for each timer ending in the time it shows, soonest first."""
        w, span = self.width(), self.span()
        return [(row, min(max((row.end_time - now) / span * w, 5.0), w - 5.0))
                for row in sorted(self.main_win.active_running, key=lambda r: r.end_time)
                if row.end_time - now <= span]

    def _hovered(self, markers):
        """The markers near the pointer, or near the marker whose label it's on."""
        if self._pointer is None:
            return []
        anchor = next((x for rect, x in self._label_rects if rect.contains(self._pointer)), self._pointer.x())
        return [(row, x) for row, x in markers if abs(x - anchor) <= self.HOVER_REACH]

    def _update_card(self):
        hovered = self._hovered(self._markers(time.time()))
        if not hovered:
            if self._card is not None:
                self._card.hide()
            return
        if self._card is None:
            self._card = TimelineCard(self)
        rows = [(row.entry["name"], datetime.fromtimestamp(row.end_time).strftime("%H:%M"),
                 format_short(row.remaining), URGENCY[self.main_win.urgency(row.remaining)])
                for row, _x in hovered]
        middle = sum(x for _row, x in hovered) / len(hovered)
        self._card.show_rows(rows, self.mapToGlobal(QPoint(round(middle), self._axis())))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        now = time.time()
        w, h = self.width(), self.height()
        span, axis, lanes = self.span(), self._axis(), self._lanes()

        # "Now" at the left edge, then a gridline and time every step minutes
        painter.setFont(ui_font(12, QFont.DemiBold))
        painter.setPen(QColor(TEXT))
        painter.drawText(QRectF(0, h - 16, 60, 16), Qt.AlignLeft | Qt.AlignVCenter, "Now")
        now_width = painter.fontMetrics().horizontalAdvance("Now")
        painter.setFont(ui_font(12))
        fm = painter.fontMetrics()
        step = self._grid_step(w / span * 3600)
        start = datetime.fromtimestamp(now)
        first = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            minutes=((start.hour * 60 + start.minute) // step + 1) * step)
        for i in range(int(span // (step * 60)) + 2):
            t = first + timedelta(minutes=i * step)
            x = (t.timestamp() - now) / span * w
            if x >= w:
                break
            painter.fillRect(QRectF(x, 0, 1, h - 20), QColor(LINE))
            text = f"{t:%H:%M}"
            width = fm.horizontalAdvance(text)
            left = min(x - width / 2, w - width)
            if left > now_width + 8:  # not on top of "Now"
                painter.setPen(QColor(MUTED))
                painter.drawText(QRectF(left, h - 16, width, 16), Qt.AlignCenter, text)

        # The axis, and a tick for now
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#2e3034"))
        painter.drawRoundedRect(QRectF(0, axis - 1, w, 2), 1, 1)
        painter.fillRect(QRectF(0, axis - 13, 2, 26), QColor(TEXT))

        # Markers, soonest first. Each label goes in the first lane with room
        # for it; a timer with no room left keeps its dot but not its label.
        painter.setFont(ui_font(12, QFont.Medium))
        fm = painter.fontMetrics()
        lane_ends = [float("-inf")] * len(lanes)
        labels = []
        self._label_rects = []
        markers = self._markers(now)
        for row, x in markers:
            colors = URGENCY[self.main_win.urgency(row.remaining)]
            name = fm.elidedText(row.entry["name"], Qt.ElideRight, 160)
            text = f"{name}  {format_short(row.remaining)}"
            width = fm.horizontalAdvance(text) + 20
            left = max(0.0, min(x - 10, w - width))
            lane = next((i for i, end in enumerate(lane_ends) if left >= end + 6), None)
            if lane is not None:
                lane_ends[lane] = left + width
                top = lanes[lane]
                stem_top = top + self.LABEL_HEIGHT
                painter.fillRect(QRectF(x - 0.5, stem_top, 1, axis - stem_top), QColor(colors["stem"]))
                rect = QRectF(left, top, width, self.LABEL_HEIGHT)
                labels.append((rect, text, colors))
                self._label_rects.append((rect, x))
            painter.setBrush(QColor(colors["dot"]))
            painter.drawEllipse(QPointF(x, axis), 5, 5)
        for rect, text, colors in labels:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors["pill"]))
            painter.drawRoundedRect(rect, self.LABEL_HEIGHT / 2, self.LABEL_HEIGHT / 2)
            painter.setPen(QColor(colors["pill_text"]))
            painter.drawText(rect, Qt.AlignCenter, text)

        # Ring the hovered markers, so it's clear which ones the card lists
        for row, x in self._hovered(markers):
            painter.setPen(QPen(QColor(TEXT), 2))
            painter.setBrush(QColor(URGENCY[self.main_win.urgency(row.remaining)]["dot"]))
            painter.drawEllipse(QPointF(x, axis), 6.5, 6.5)


class TimelineCard(QWidget):
    """
    A small floating card over the timeline listing the timers under the
    pointer: name, end time and time left, brought in front of everything.
    """
    ROW = 26
    PAD = 8
    MAX_ROWS = 8
    NAME_WIDTH = 220   # longer names end in "…"

    def __init__(self, parent):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.name_font = ui_font(13, QFont.Medium)
        self.small_font = ui_font(12)
        self.rows = []
        self.more = 0

    def show_rows(self, rows, marker):
        """
        List rows [(name, ends, time left, urgency colours)] just above marker
        (a global point on the axis), or below it when there's no room above.
        """
        self.rows, self.more = rows[:self.MAX_ROWS], max(0, len(rows) - self.MAX_ROWS)
        names, small = QFontMetrics(self.name_font), QFontMetrics(self.small_font)
        self.name_width = min(self.NAME_WIDTH, max(names.horizontalAdvance(r[0]) for r in self.rows))
        self.ends_width = small.horizontalAdvance("00:00")
        self.time_width = max(names.horizontalAdvance(r[2]) for r in self.rows)
        width = 2 * self.PAD + 12 + 7 + 8 + self.name_width + 14 + self.ends_width + 14 + self.time_width + 12
        height = 2 * self.PAD + self.ROW * (len(self.rows) + (1 if self.more else 0))
        self.setFixedSize(width, height)

        screen = (QApplication.screenAt(marker) or QApplication.primaryScreen()).availableGeometry()
        x = max(screen.left(), min(marker.x() - width // 2, screen.right() - width))
        y = marker.y() - 12 - height
        if y < screen.top():
            y = marker.y() + 12
        self.move(x, y)
        self.update()
        self.show()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(EDGE), 1))
        painter.setBrush(QColor(CONTROL))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)

        x0 = self.PAD + 12
        name_x = x0 + 7 + 8
        ends_x = name_x + self.name_width + 14
        time_right = self.width() - self.PAD - 12
        y = self.PAD
        for name, ends, left, colors in self.rows:
            rect = lambda x, w: QRectF(x, y, w, self.ROW)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors["dot"]))
            painter.drawEllipse(QPointF(x0 + 3.5, y + self.ROW / 2), 3.5, 3.5)
            painter.setFont(self.name_font)
            painter.setPen(QColor(TEXT))
            text = painter.fontMetrics().elidedText(name, Qt.ElideRight, self.name_width)
            painter.drawText(rect(name_x, self.name_width), Qt.AlignLeft | Qt.AlignVCenter, text)
            painter.setFont(self.small_font)
            painter.setPen(QColor(MUTED))
            painter.drawText(rect(ends_x, self.ends_width), Qt.AlignLeft | Qt.AlignVCenter, ends)
            painter.setFont(self.name_font)
            painter.setPen(QColor(colors["text"]))
            painter.drawText(rect(time_right - self.time_width, self.time_width),
                             Qt.AlignRight | Qt.AlignVCenter, left)
            y += self.ROW
        if self.more:
            painter.setFont(self.small_font)
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(name_x, y, self.width(), self.ROW), Qt.AlignLeft | Qt.AlignVCenter,
                             f"and {self.more} more")


class TimerDialog(QDialog):
    def __init__(self, parent,
                 on_add=None, on_save=None,
                 entry: dict=None,
                 filters: list=None, sites: list=None):
        """
        If entry is None → “Add” mode → call on_add(entry, secs).
        If entry is a dict → “Edit” mode → populate fields, call on_save() on OK.
        """
        super().__init__(parent)
        self.on_add  = on_add
        self.on_save = on_save
        self.is_edit = entry is not None
        self.entry   = entry or {}
        self.mode    = self.entry.get("type", "countdown")
        self.filters = filters or []
        # Keep a site the timer already has even if it's no longer in the
        # managed list, so pressing OK in Edit doesn't silently strip it.
        self.sites   = list(sites or [])
        site = self.entry.get("site", "")
        if site and site not in self.sites:
            self.sites.append(site)

        # build UI
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setWindowTitle("Edit Timer" if self.is_edit else "Add New Timer")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8,8,8,8)
        main_layout.setSpacing(10)

        # ── Name ──
        grp_name = QGroupBox("Timer Name")
        lay_name = QVBoxLayout(grp_name)
        self.entry_name = QLineEdit(self.entry.get("name", ""))
        self.entry_name.setPlaceholderText("e.g. Warm up, Main Event, or Knockout")
        lay_name.addWidget(self.entry_name)
        main_layout.addWidget(grp_name)

        # ── Mode ──
        grp = QGroupBox("Mode")
        lay = QHBoxLayout(grp); lay.setContentsMargins(6,6,6,6); lay.setSpacing(8)
        self.rd_cd = QRadioButton("Countdown")
        self.rd_al = QRadioButton("Alarm")
        self.rd_cd.setChecked(self.mode=="countdown")
        self.rd_al.setChecked(self.mode=="alarm")
        if self.is_edit:
            self.rd_cd.setEnabled(False)
            self.rd_al.setEnabled(False)
        lay.addWidget(self.rd_cd); lay.addWidget(self.rd_al)
        self.rd_cd.toggled.connect(self._update_label)
        main_layout.addWidget(grp)

        # ── Duration/Time ──
        grp = QGroupBox("Duration / Time")
        lay = QHBoxLayout(grp); lay.setContentsMargins(6,6,6,6); lay.setSpacing(6)
        self.lbl_dyn = QLabel("Duration:" if self.mode=="countdown" else "Time:")
        lay.addWidget(self.lbl_dyn)
        lay.addWidget(QLabel("Hours:"))
        self.sp_hr = QSpinBox(); self.sp_hr.setRange(0,23)
        lay.addWidget(self.sp_hr)
        lay.addWidget(QLabel("Minutes:"))
        self.sp_min = QSpinBox(); self.sp_min.setRange(0,59)
        lay.addWidget(self.sp_min)
        # prefill
        if self.mode=="countdown":
            self.sp_hr.setValue(self.entry.get("hours",0))
            self.sp_min.setValue(self.entry.get("minutes",0))
        else:
            self.sp_hr.setValue(self.entry.get("alarm_hour",0))
            self.sp_min.setValue(self.entry.get("alarm_minute",0))
        main_layout.addWidget(grp)

        # ── Filters ──
        grp = QGroupBox("Filters")
        lay = QVBoxLayout(grp); lay.setContentsMargins(6,6,6,6); lay.setSpacing(4)
        self.chk_all = QCheckBox("Select All")
        self.chk_all.toggled.connect(self._on_select_all)
        lay.addWidget(self.chk_all)
        self.lst = QListWidget()
        for f in self.filters:
            it = QListWidgetItem(f)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if f in self.entry.get("filters",[]) else Qt.Unchecked)
            self.lst.addItem(it)
        self.lst.setFixedHeight(130)
        self.lst.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # Tracks whether the most recent press landed on the native checkbox
        # indicator (which toggles itself) vs. the row text (which does not).
        self._press_on_indicator = False
        self.lst.viewport().installEventFilter(self)
        self.lst.itemClicked.connect(self._on_item_clicked)
        self.lst.itemChanged.connect(self._sync_select_all)
        self.chk_all.setEnabled(self.lst.count() > 0)
        self._sync_select_all()  # items were pre-checked before the signal was wired
        lay.addWidget(self.lst)
        main_layout.addWidget(grp)

        # ─── Site (optional) ───
        grp_site = QGroupBox("Site (optional)")
        lay_site = QVBoxLayout(grp_site)
        lay_site.setContentsMargins(6, 6, 6, 6)
        lay_site.setSpacing(4)

        self.site_combo = QComboBox()
        self.site_combo.setEditable(False)  # prevent free‐form typing
        self.site_combo.addItem("")  # blank = no site

        # Populate only from the current user-managed list
        for icon in self.sites:
            self.site_combo.addItem(icon)

        # If in edit‐mode, preselect the timer’s existing site
        if self.is_edit:
            self.site_combo.setCurrentText(self.entry.get("site", ""))

        # Keep it a reasonable width
        self.site_combo.setMaximumWidth(120)
        lay_site.addWidget(self.site_combo)

        main_layout.addWidget(grp_site)

        # ── Options ──
        grp = QGroupBox("Options")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 6)

        self.chk_save = QCheckBox("Save For Future Use (will save as alarm)")
        # hide it when in edit mode
        if self.is_edit:
            self.chk_save.setEnabled(False)

        lay.addWidget(self.chk_save)
        main_layout.addWidget(grp)

        # ── Buttons ──
        btns = QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        main_layout.addWidget(btns)

        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _update_label(self, _):
        if self.rd_cd.isChecked():
            self.lbl_dyn.setText("Duration:")
            self.mode = "countdown"
        else:
            self.lbl_dyn.setText("Time:")
            self.mode = "alarm"

    def _on_select_all(self, chk):
        st = Qt.Checked if chk else Qt.Unchecked
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(st)

    def eventFilter(self, obj, event):
        # Record, on each press, whether the cursor was over the checkbox
        # indicator so _on_item_clicked can avoid a double-toggle.
        if obj is self.lst.viewport() and event.type() == QEvent.MouseButtonPress:
            item = self.lst.itemAt(event.pos())
            self._press_on_indicator = bool(item) and self._pos_on_indicator(item, event.pos())
        return super().eventFilter(obj, event)

    def _pos_on_indicator(self, item, pos):
        opt = QStyleOptionViewItem()
        opt.initFrom(self.lst)
        opt.rect = self.lst.visualItemRect(item)
        opt.features |= QStyleOptionViewItem.HasCheckIndicator
        rect = self.lst.style().subElementRect(
            QStyle.SE_ItemViewItemCheckIndicator, opt, self.lst)
        return rect.contains(pos)

    def _on_item_clicked(self, item):
        # If the click hit the checkbox indicator, Qt already toggled the state
        # natively — toggling again here would cancel it out (the old bug where
        # clicking the box did nothing). Only toggle for clicks on the row text.
        if not self._press_on_indicator:
            new = Qt.Unchecked if item.checkState()==Qt.Checked else Qt.Checked
            item.setCheckState(new)

    def _sync_select_all(self, *args):
        # Fires on any check-state change (indicator click, text click, or
        # Select All), keeping the "Select All" box in sync.
        allc = self.lst.count() > 0 and all(
            self.lst.item(i).checkState()==Qt.Checked for i in range(self.lst.count()))
        self.chk_all.blockSignals(True)
        self.chk_all.setChecked(allc)
        self.chk_all.blockSignals(False)

    def _on_ok(self):
        name = self.entry_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Invalid Name", "Enter a name for the timer.")
            return

        data = {"name":name}
        if self.mode=="countdown":
            hrs,mins = self.sp_hr.value(),self.sp_min.value()
            secs = hrs*3600+mins*60
            if secs<=0:
                QMessageBox.warning(self,"Invalid Duration","Enter a duration > 0.")
                return
            data.update(type="countdown",hours=hrs,minutes=mins)
        else:
            ah,am = self.sp_hr.value(),self.sp_min.value()
            secs = seconds_until(ah, am)
            data.update(type="alarm",alarm_hour=ah,alarm_minute=am)

        chosen = [ self.lst.item(i).text()
                   for i in range(self.lst.count())
                   if self.lst.item(i).checkState()==Qt.Checked ]
        data["filters"]=chosen
        data["site"] = self.site_combo.currentText().strip()

        if self.is_edit:
            # update the passed-in dict
            self.entry.update(data)
            # invoke the save callback
            self.on_save()
        else:
            # add new timer; False means it was rejected (e.g. a duplicate),
            # so stay open and keep what the user typed
            if self.on_add(data, secs, self.chk_save.isChecked()) is False:
                return

        self.accept()


class AddTimerDialog(TimerDialog):
    def __init__(self, parent, on_add, filters, sites):
        super().__init__(parent,
                         on_add=on_add,
                         on_save=None,
                         entry=None,
                         filters=filters,
                         sites=sites)




class EditTimerDialog(TimerDialog):
    def __init__(self, parent, entry, filters, sites, on_save):
        super().__init__(parent,
                         on_add=None,
                         on_save=on_save,
                         entry=entry,
                         filters=filters,
                         sites=sites)


class SettingsDialog(QDialog):
    def __init__(
        self,
        parent,
        current_yellow: int,
        current_red: int,
        current_popup: bool,
        current_sound: bool,
        current_ontop: dict,
        current_sites: list,
        current_hours: int
    ):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setWindowTitle("Settings")
        self.site_icons = list(current_sites)

        # ─── Color Thresholds ───
        grp_thresh = QGroupBox("Color Thresholds")
        v_thresh = QVBoxLayout(grp_thresh)
        hl = QHBoxLayout()
        hl.addWidget(QLabel("Yellow threshold (min):"))
        self.spin_yellow = QSpinBox()
        self.spin_yellow.setRange(1, 1440)
        self.spin_yellow.setValue(current_yellow)
        hl.addWidget(self.spin_yellow)
        v_thresh.addLayout(hl)
        hl = QHBoxLayout()
        hl.addWidget(QLabel("Red threshold (min):"))
        self.spin_red = QSpinBox()
        self.spin_red.setRange(1, 1440)
        self.spin_red.setValue(current_red)
        hl.addWidget(self.spin_red)
        v_thresh.addLayout(hl)

        # ─── Timer Alert ───
        grp_alert = QGroupBox("Timer Alert")
        v_alert = QVBoxLayout(grp_alert)
        self.radio_popup = QRadioButton("Popup only")
        self.radio_sound = QRadioButton("Sound only")
        self.radio_both  = QRadioButton("Both popup and sound")
        v_alert.addWidget(self.radio_popup)
        v_alert.addWidget(self.radio_sound)
        v_alert.addWidget(self.radio_both)
        if current_popup and current_sound:
            self.radio_both.setChecked(True)
        elif current_sound:
            self.radio_sound.setChecked(True)
        else:
            self.radio_popup.setChecked(True)

        # ─── Upcoming ───
        grp_upcoming = QGroupBox("Upcoming")
        v_upcoming = QVBoxLayout(grp_upcoming)
        hl = QHBoxLayout()
        hl.addWidget(QLabel("Hours shown:"))
        self.spin_hours = QSpinBox()
        self.spin_hours.setRange(1, MAX_UPCOMING_HOURS)
        self.spin_hours.setValue(current_hours)
        hl.addWidget(self.spin_hours)
        v_upcoming.addLayout(hl)
        v_upcoming.addWidget(make_label("Widen its pop-out to see further ahead.", 12, color=MUTED))

        # ─── Pop-Outs ───
        grp_popout = QGroupBox("Pop-Outs")
        v_pop = QVBoxLayout(grp_popout)
        self.chk_ontop = {}
        for kind, text in (("running", "Keep Running on top"), ("upcoming", "Keep Upcoming on top")):
            chk = QCheckBox(text)
            chk.setChecked(current_ontop[kind])
            v_pop.addWidget(chk)
            self.chk_ontop[kind] = chk
        v_pop.addWidget(make_label("They reopen where you left them, at the same size.", 12, color=MUTED))

        # ─── Site Icons ───
        grp_icons = QGroupBox("Site Icons")
        v_icons = QVBoxLayout(grp_icons)
        btn_manage = QPushButton("Manage Site Icons…")
        btn_manage.clicked.connect(self._open_manage_site_icons)
        v_icons.addWidget(btn_manage)

        # ─── Layout split: two rows ───
        split = QHBoxLayout()
        split.addWidget(grp_thresh)
        split.addWidget(grp_alert)
        split.addWidget(grp_icons)
        split_views = QHBoxLayout()
        split_views.addWidget(grp_upcoming)
        split_views.addWidget(grp_popout)

        # ─── OK / Cancel ───
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.addLayout(split)
        main_layout.addLayout(split_views)
        main_layout.addWidget(buttons)

        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _on_ok(self):
        # Rows go yellow below the yellow mark, then red below the red one, so
        # red must be the smaller value or the yellow stage never shows.
        if self.spin_red.value() >= self.spin_yellow.value():
            QMessageBox.warning(
                self, "Invalid Thresholds",
                "The red threshold must be lower than the yellow threshold."
            )
            return
        self.accept()

    def _open_manage_site_icons(self):
        # Icon edits apply as they're made (like Manage Filters), so they
        # stick even if this Settings dialog is then cancelled.
        parent = self.parent()
        dlg = ManageSiteIconsDialog(
            self, self.site_icons,
            on_done=lambda: parent._set_site_icons(list(self.site_icons))
        )
        dlg.exec_()

    def get_values(self):
        y   = self.spin_yellow.value()
        r   = self.spin_red.value()
        if self.radio_both.isChecked():
            popup, sound = True, True
        elif self.radio_sound.isChecked():
            popup, sound = False, True
        else:
            popup, sound = True, False
        ontop = {kind: chk.isChecked() for kind, chk in self.chk_ontop.items()}
        return y, r, popup, sound, ontop, self.site_icons, self.spin_hours.value()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{VERSION}")

        # LOAD all 9 values now (including site_icons)
        y_min, r_min, popup, sound, ontop, popup_x, popup_y, site_icons, upcoming_hours = load_settings()
        self.yellow_threshold = y_min * 60
        self.red_threshold    = r_min * 60
        self.alert_popup      = popup
        self.alert_sound      = sound
        self.popout_ontop     = ontop  # {"running": bool, "upcoming": bool}
        self.popout_x         = popup_x
        self.popout_y         = popup_y
        self.upcoming_hours   = upcoming_hours

        self.saved_timers = self._load_saved_timers()

        # store the user’s curated icon list
        self.site_icons = site_icons

        # Try reading filters from settings.json first
        loaded = load_filters()
        if loaded is not None:
            self.filter_order = list(loaded)
            # Drop tags left on timers by filters deleted/renamed in older versions
            for entry in self.saved_timers:
                entry["filters"] = [f for f in entry["filters"] if f in self.filter_order]
        else:
            initial_filters = {f for entry in self.saved_timers for f in entry.get("filters", [])}
            self.filter_order = sorted(initial_filters)

        self.active_filters = []   # empty means show all
        self.active_running = []   # RunningRow widgets
        self.saved_rows = []
        self._filter_actions = {}  # filter name (None for "All") -> its menu item
        self._finish_popups = []   # open "Timer Finished" boxes (kept alive here)
        self._bulk = False         # True while starting/cancelling several timers at once
        self.popouts = {}          # "running"/"upcoming" -> its pop-out, once opened
        prefs = load_sort_prefs()
        self.running_sort = prefs["running"]
        self.saved_sort = prefs["saved"]

        body = QVBoxLayout(self)
        body.setSpacing(16)
        body.setContentsMargins(24, 20, 24, 20)
        # The cards, top to bottom; reordering them is just reordering these
        # lines. The tables show VISIBLE_ROWS rows and scroll, so nothing
        # moves as timers start and stop; a taller window gives Running more.
        # New timer and Settings are in the Saved card's header.
        body.addWidget(self._build_saved_card())
        body.addWidget(self._build_timeline_card())
        body.addWidget(self._build_running_card(), 1)

        self._rebuild_filter_menu()
        self._refresh_saved_list()
        self._running_changed()

        # One ticker drives every countdown, the timeline and the pop-outs
        self._ticker = QTimer(self)
        self._ticker.setSingleShot(True)
        self._ticker.setTimerType(Qt.PreciseTimer)
        self._ticker.timeout.connect(self._tick)
        self._tick()

        # Where it was left (Qt moves it back on screen if that monitor's gone)
        saved = load_geometry("window_geometry")
        if saved is None or not self.restoreGeometry(saved):
            # First run: 792×925 inside the frame, shrunk to fit smaller
            # screens, and centred so a window this tall isn't placed partly
            # behind the taskbar (move() places the frame, which adds about
            # 32px of title bar and border)
            avail = QApplication.primaryScreen().availableGeometry()
            width, height = min(792, avail.width() - 16), min(925, avail.height() - 36)
            self.resize(width, height)
            self.move(avail.x() + (avail.width() - width) // 2,
                      avail.y() + max(0, (avail.height() - height - 32) // 2))

    def _corner_button(self, icon, tooltip, on_click):
        """A small button in a card's top-right corner: Settings, or a pop-out."""
        btn = pill_button(icon=icon, height=24, tooltip=tooltip, icon_size=12)
        btn.clicked.connect(on_click)
        return btn

    def _pop_out_button(self, kind, name):
        return self._corner_button("popout", f"Pop out {name}", lambda: self._pop_out(kind))

    def _build_timeline_card(self):
        card = QFrame()
        card.setObjectName("Card")
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # The pop-out button is 16px in, like the other cards' corner buttons,
        # while the timeline keeps 24px either side. The margins leave the
        # title and timeline where they were before the button was added.
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 8, 16, 10)
        layout.setSpacing(7)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.timeline_title = make_label("", 13, QFont.Medium)
        self.timeline_title.setTextFormat(Qt.RichText)
        self.legend = make_label("", 12, color=MUTED)
        self.legend.setTextFormat(Qt.RichText)
        top.addWidget(self.timeline_title)
        top.addStretch()
        top.addWidget(self.legend)
        top.addWidget(self._pop_out_button("upcoming", "Upcoming"))
        layout.addLayout(top)
        self.timeline = Timeline(self)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 8, 0)
        line.addWidget(self.timeline)
        layout.addLayout(line)
        self._show_legend()
        self.timeline_card = card
        return card

    def _build_running_card(self):
        card = TableCard(
            "Running", RUNNING_COLUMNS,
            [("Name", "name"), ("Ends", "ends"), ("Remaining", "remaining"),
             ("Left", None), ("Adjust", None), ("", None)],
            RunningRow.HEIGHT, "No active timers", self._sort_running_by, grows=True)
        card.header_row.addStretch()
        self.btn_clear_all = pill_button("Clear all", height=34)
        self.btn_clear_all.clicked.connect(self._clear_all_timers)
        card.header_row.addWidget(self.btn_clear_all, 0, Qt.AlignTop)
        card.header_row.addWidget(self._pop_out_button("running", "Running"), 0, Qt.AlignVCenter)
        card.show_sort(*self.running_sort)
        self.running_card = card
        return card

    def _build_saved_card(self):
        card = TableCard(
            "Saved", SAVED_COLUMNS,
            [("Name", "name"), ("Filters", None), ("Alarm", "alarm"), ("Status", "status"), ("", None)],
            SavedRow.HEIGHT, "No saved timers", self._sort_saved_by)
        # The filter dropdown, named after the active filters, then the
        # "3 of 7 · clear" count while any are on
        self.filter_menu = FilterMenu(self)
        self.btn_filters = pill_button("All", "filter", height=34, cls=DropdownButton)
        self.btn_filters.setProperty("dropdown", "true")
        self.btn_filters.setMenu(self.filter_menu)
        card.header_row.addWidget(self.btn_filters, 0, Qt.AlignTop)
        self.lbl_filter_count = make_label("", 12, color=MUTED)
        self.lbl_filter_count.setFixedHeight(34)
        self.lbl_filter_count.setTextFormat(Qt.RichText)
        self.lbl_filter_count.setTextInteractionFlags(Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        self.lbl_filter_count.linkActivated.connect(self._clear_filters)
        card.header_row.addWidget(self.lbl_filter_count, 0, Qt.AlignTop)
        card.header_row.addStretch()
        self.btn_start_visible = pill_button("Start all", "play", height=34, icon_size=12)
        self.btn_start_visible.clicked.connect(self._start_all_timers)
        card.header_row.addWidget(self.btn_start_visible, 0, Qt.AlignTop)
        # The window has no header, so the app-wide buttons live here, at the top
        btn_add = pill_button("New timer", "plus", height=34, primary=True, stroke=2.6)
        btn_add.clicked.connect(self.open_add_dialog)
        card.header_row.addWidget(btn_add, 0, Qt.AlignTop)
        card.header_row.addWidget(self._corner_button("settings", "Settings", self.open_settings_dialog),
                                  0, Qt.AlignVCenter)
        card.show_sort(*self.saved_sort)
        self.saved_card = card
        return card

    def _show_legend(self):
        """The timeline's legend, using the thresholds from Settings."""
        items = [
            (URGENCY["red"]["dot"], f"Under {format_minutes(self.red_threshold // 60)}"),
            (URGENCY["yellow"]["dot"], f"Under {format_minutes(self.yellow_threshold // 60)}"),
            (URGENCY["normal"]["dot"], "Later"),
        ]
        self.legend.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join(
            f'<span style="color:{color}">●</span>&nbsp;{text}' for color, text in items))

    def urgency(self, seconds):
        """"red", "yellow" or "normal" for a timer with this many seconds left."""
        if seconds < self.red_threshold:
            return "red"
        if seconds < self.yellow_threshold:
            return "yellow"
        return "normal"

    def _tick(self):
        # Fire again just after the next whole second. Timers end on whole
        # seconds too, so every countdown ticks over at once, right on time.
        self._ticker.start(1000 - int(time.time() * 1000) % 1000 + 5)
        now = time.time()
        finished = []
        for row in self.active_running:
            row.refresh(now)
            if row.remaining <= 0:
                finished.append(row)
        for row in finished:
            self._finish_running_timer(row, completed=True)
        self._refresh_overview(now)

    def _refresh_overview(self, now=None):
        """Redraw what shows every running timer at once: the timeline and the pop-outs."""
        now = time.time() if now is None else now
        self.timeline_title.setText(upcoming_title(self.timeline.later(now)))
        self.timeline.refresh()
        for pop in self._open_popouts():
            pop.refresh()

    def _load_saved_timers(self):
        # Any path that can't keep the file safe sets this False, so the
        # session never saves a partial list over the real one.
        self._timers_writable = True
        try:
            raw = _read_text(TIMERS_FILE)
        except OSError:
            self._timers_writable = False
            _warn(f"{TIMERS_FILE.name} couldn't be opened (another program may be using it), "
                  "so your saved timers weren't loaded. Changes to saved timers won't be "
                  "saved until you restart Slugware.")
            return []
        if raw is None:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, list):
            try:
                _set_aside_damaged(TIMERS_FILE)
            except OSError:
                self._timers_writable = False
            return []

        valid = [e for e in map(_clean_timer, data) if e is not None]
        skipped = len(data) - len(valid)
        if skipped:
            # Keep a copy of the original, then write back the cleaned list so
            # the next launch doesn't warn (and back up) all over again.
            backup = _back_up(TIMERS_FILE, move=False)
            msg = f"{skipped} saved timer(s) in {TIMERS_FILE.name} couldn't be read and were skipped."
            if backup:
                msg += f"\n\nA copy of the original file was saved to:\n{backup}"
                try:
                    _write_json(TIMERS_FILE, valid)
                except OSError:
                    pass
            else:
                self._timers_writable = False
            _warn(msg)
        return valid

    def _save_saved_timers(self):
        if not self._timers_writable:
            QMessageBox.warning(
                self, "Not Saved",
                f"Saved timers can't be written this session because {TIMERS_FILE.name} "
                "couldn't be read safely at startup. Restart Slugware to save changes."
            )
            return
        try:
            _write_json(TIMERS_FILE, self.saved_timers)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save timers:\n{e}")

    def _rebuild_filter_menu(self):
        """(Re)fill the filter dropdown, after the list of filters changes."""
        menu = self.filter_menu
        menu.clear()
        self._filter_actions = {}
        # "&" would otherwise mark a keyboard shortcut in menu and button text
        add_tick = lambda text: menu.addAction(text.replace("&", "&&"))
        self._filter_actions[None] = add_tick("All")
        self._filter_actions[None].triggered.connect(self._clear_filters)
        if self.filter_order:
            menu.addSeparator()
        for name in self.filter_order:
            action = add_tick(name)
            action.triggered.connect(lambda _checked=False, n=name: self._toggle_filter(n))
            self._filter_actions[name] = action
        for action in self._filter_actions.values():
            action.setCheckable(True)
        menu.addSeparator()
        menu.addAction("Manage filters…", self.open_manage_filters_dialog)
        self._sync_filter_menu()

    def _sync_filter_menu(self):
        """Tick the active filters, name them on the dropdown, and count what they let through."""
        for name, action in self._filter_actions.items():
            action.setChecked(not self.active_filters if name is None else name in self.active_filters)
        active = bool(self.active_filters)
        btn = self.btn_filters
        btn.setText(" " + self._filter_summary().replace("&", "&&"))
        btn.setToolTip(", ".join(self.active_filters) if active else "Filter the saved timers")
        # White while filtering, so a list with timers hidden is easy to spot
        if btn.property("filtering") != active:
            btn.setProperty("filtering", active)
            btn.setIcon(make_icon("filter", BG if active else TEXT))
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if active:
            shown = len(self._visible_saved())
            self.lbl_filter_count.setText(
                f'{shown} of {len(self.saved_timers)} · <a href="clear" style="color:{TEXT}">clear</a>')
        self.lbl_filter_count.setVisible(active)

    def _filter_summary(self, max_width=160):
        """
        The dropdown's label: "All", or as many active filter names as fit,
        then "+N". max_width keeps the Saved card's header (which also holds
        New timer and Settings) inside the window's default width.
        """
        names = self.active_filters
        if not names:
            return "All"
        fm = QFontMetrics(self.btn_filters.font())
        for count in range(len(names), 1, -1):
            more = f" +{len(names) - count}" if count < len(names) else ""
            text = ", ".join(names[:count]) + more
            if fm.horizontalAdvance(text) <= max_width:
                return text
        more = f" +{len(names) - 1}" if len(names) > 1 else ""
        return fm.elidedText(names[0], Qt.ElideRight, max_width - fm.horizontalAdvance(more)) + more

    def _toggle_filter(self, name):
        if name in self.active_filters:
            self.active_filters.remove(name)
        else:
            # Keep them in the same order as the menu
            self.active_filters = [f for f in self.filter_order if f in self.active_filters or f == name]
        self._refresh_saved_list()

    def _clear_filters(self, *_):
        self.active_filters = []
        self._refresh_saved_list()

    def _visible_saved(self):
        """Saved timers that match any active filter (all of them when none is on)."""
        if not self.active_filters:
            return list(self.saved_timers)
        return [e for e in self.saved_timers
                if any(f in self.active_filters for f in e.get("filters", []))]

    def _running_entries(self):
        return {id(row.entry) for row in self.active_running}

    def _refresh_saved_list(self):
        key, descending = self.saved_sort
        running = self._running_entries()
        if key == "name":
            sort_key = lambda e: e["name"].casefold()
        elif key == "status":
            sort_key = lambda e: (id(e) not in running, e["name"].casefold())
        else:  # alarm time; any older countdown entries go after the alarms, shortest first
            def sort_key(e):
                if e["type"] == "alarm":
                    minutes = e.get("alarm_hour", 0) * 60 + e.get("alarm_minute", 0)
                else:
                    minutes = e.get("hours", 0) * 60 + e.get("minutes", 0)
                return e["type"] != "alarm", minutes, e["name"].casefold()
        visible = sorted(self._visible_saved(), key=sort_key, reverse=descending)

        rows = self.saved_card.list
        rows.clear()
        self.saved_rows = []
        for entry in visible:
            row = SavedRow(entry, self)
            rows.add(row)
            self.saved_rows.append(row)
        rows.placeholder.setText(
            "No saved timers for the selected filters" if self.active_filters else "No saved timers")
        self.saved_card.badge.setText(str(len(self.saved_timers)))
        self._sync_filter_menu()
        self._update_saved_status()

    def _update_saved_status(self):
        """Mark which saved timers are running, and count the ones Start can start."""
        running = self._running_entries()
        idle = 0
        for row in self.saved_rows:
            is_running = id(row.entry) in running
            row.set_running(is_running)
            idle += not is_running
        self.btn_start_visible.setText(f" Start {idle}" if self.active_filters else " Start all")
        self.btn_start_visible.setEnabled(idle > 0)

    def _sort_saved_by(self, key):
        old_key, descending = self.saved_sort
        self.saved_sort = (key, not descending if key == old_key else False)
        save_sort_pref("saved", *self.saved_sort)
        self.saved_card.show_sort(*self.saved_sort)
        self._refresh_saved_list()

    def _sort_running_by(self, key):
        old_key, descending = self.running_sort
        self.running_sort = (key, not descending if key == old_key else False)
        save_sort_pref("running", *self.running_sort)
        self.running_card.show_sort(*self.running_sort)
        self._sort_running_rows()

    def _sort_running_rows(self):
        key, descending = self.running_sort
        if key == "name":
            sort_key = lambda r: r.entry["name"].casefold()
        else:  # ends / remaining
            sort_key = lambda r: r.end_time
        self.active_running.sort(key=sort_key, reverse=descending)
        self.running_card.list.reorder(self.active_running)

    def _start_saved_timer(self, entry, quiet=False):
        """Start entry; returns False if it was already running."""
        for widget in self.active_running:
            if widget.entry is entry:
                if not quiet:
                    QMessageBox.information(
                        self,
                        "Timer Already Running",
                        f"‘{entry['name']}’ is already active."
                    )
                return False

        if entry["type"] == "countdown":
            total = entry.get("hours", 0) * 3600 + entry.get("minutes", 0) * 60
        else:
            total = seconds_until(entry.get("alarm_hour", 0), entry.get("alarm_minute", 0))

        self._add_running_widget(entry, total)
        return True

    def _delete_saved_timer(self, entry):
        confirm = QMessageBox.question(self, "Delete Timer", f"Delete '{entry['name']}'?")
        if confirm == QMessageBox.Yes:
            self.saved_timers = [e for e in self.saved_timers if e is not entry]
            self._save_saved_timers()
            self._refresh_saved_list()

    def _edit_saved_timer(self, entry):
        dlg = EditTimerDialog(
            self,
            entry,
            self.filter_order[:],
            self.site_icons,
            on_save=self._on_edit_save
        )
        dlg.exec_()

    def _on_edit_save(self):
        self._save_saved_timers()
        self._refresh_saved_list()
        # A running instance shares the edited dict; show its new name/site
        # (it keeps counting down from when it was started).
        for row in self.active_running:
            row.show_entry()
        self._sort_running_rows()
        self._refresh_overview()

    def _start_all_timers(self):
        # The ones already running are skipped; the button only counts idle ones
        self._bulk = True
        try:
            for entry in self._visible_saved():
                self._start_saved_timer(entry, quiet=True)
        finally:
            self._bulk = False
        self._running_changed()

    def open_add_dialog(self):
        dlg = AddTimerDialog(
            self,
            self._on_add_new_timer,
            self.filter_order[:],
            self.site_icons
        )
        dlg.exec_()

    def _on_add_new_timer(self, entry, total_seconds, persistent):
        if persistent:
            if entry["type"] == "countdown":
                now = datetime.now()
                delta = timedelta(hours=entry["hours"], minutes=entry["minutes"])
                target = now + delta
                entry = {
                    "name": entry["name"],
                    "type": "alarm",
                    "alarm_hour": target.hour,
                    "alarm_minute": target.minute,
                    "filters": entry.get("filters", []),
                    "site": entry.get("site", "")
                }
            else:
                entry["filters"] = entry.get("filters", [])
                entry["site"] = entry.get("site", "")

            for e in self.saved_timers:
                if e["name"] == entry["name"] and set(e.get("filters", [])) == set(entry.get("filters", [])):
                    QMessageBox.warning(self, "Duplicate Name", f"A timer named '{entry['name']}' already exists with the same filters.")
                    return False

            self.saved_timers.append(entry)
            self._save_saved_timers()

            # Add any new filters to filter_order
            for f in entry.get("filters", []):
                if f and f not in self.filter_order:
                    self.filter_order.append(f)
            save_filters(self.filter_order)

            self._rebuild_filter_menu()
            self._refresh_saved_list()

        self._add_running_widget(entry, total_seconds)

    def _add_running_widget(self, entry, total_seconds):
        row = RunningRow(entry, total_seconds, self)
        self.active_running.append(row)
        self.running_card.list.add(row)
        self._running_changed()

    def _finish_running_timer(self, row, completed=False):
        if row not in self.active_running:
            return
        self.active_running.remove(row)
        self.running_card.list.take(row)
        row.deleteLater()
        self._running_changed()
        if completed:
            self._alert_finished(row.entry)

    def _timer_adjusted(self, row):
        """After −1m/+1m on a running timer."""
        row.refresh(time.time())
        self._sort_running_rows()
        self._refresh_overview()

    def _running_changed(self):
        """Update everything that depends on which timers are running."""
        if self._bulk:
            return
        self._sort_running_rows()
        self.running_card.badge.setText(str(len(self.active_running)))
        self.btn_clear_all.setEnabled(bool(self.active_running))
        if self.saved_sort[0] == "status":
            self._refresh_saved_list()
        else:
            self._update_saved_status()
        self._refresh_overview()

    def _alert_finished(self, entry):
        if self.alert_sound:
            QApplication.beep()
        if not self.alert_popup:
            return
        # A top-level, non-modal box: it stays visible while the main window is
        # minimised, and each finished timer gets its own box instead of
        # nesting inside (and blocking) the previous one.
        msg = QMessageBox()
        msg.setWindowTitle("Timer Finished")
        msg.setText(f"Time for {entry['name']}!")
        # The Information icon also plays the system sound, so tie it to the sound setting
        msg.setIcon(QMessageBox.Information if self.alert_sound else QMessageBox.NoIcon)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.setWindowModality(Qt.NonModal)
        self._finish_popups.append(msg)
        msg.finished.connect(lambda _res, m=msg: self._close_finish_popup(m))
        msg.show()
        msg.raise_()
        msg.activateWindow()

    def _close_finish_popup(self, msg):
        if msg in self._finish_popups:
            self._finish_popups.remove(msg)
        msg.deleteLater()

    def open_settings_dialog(self):
        # only allow one instance (checked first so a second click doesn't
        # build and leak another dialog)
        if getattr(self, "_settings_dialog", None) and self._settings_dialog.isVisible():
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        # 1) read the settings (the pop-out position isn't edited here any more)
        y, r, popup, sound, ontop, _px, _py, sites, hours = load_settings()

        # 2) create dialog, passing in the site_icons list
        dlg = SettingsDialog(
            self,
            current_yellow=y,
            current_red=r,
            current_popup=popup,
            current_sound=sound,
            current_ontop=ontop,
            current_sites=sites,
            current_hours=hours
        )
        dlg.setWindowModality(Qt.NonModal)

        # 3) when user hits OK, grab the values back from get_values()
        dlg.accepted.connect(lambda: self._apply_settings_from(dlg))
        # finished fires after accepted; drop our handle before freeing the
        # dialog so the one-instance check above never touches a deleted one
        dlg.finished.connect(lambda _res: self._release_settings_dialog(dlg))

        self._settings_dialog = dlg
        dlg.show()

    def _release_settings_dialog(self, dlg):
        if self._settings_dialog is dlg:
            self._settings_dialog = None
        dlg.deleteLater()

    def open_manage_filters_dialog(self):
        # Pass the list of filter names, not saved_timers
        dlg = ManageFiltersDialog(
            self,
            self.filter_order,  # list of strings
            on_done=self._after_filter_change,
            on_rename=self._on_filter_renamed,
            on_delete=self._on_filter_deleted
        )
        dlg.exec_()

    def _on_filter_renamed(self, old, new):
        rename = lambda names: list(dict.fromkeys(new if f == old else f for f in names))
        for entry in self.saved_timers:
            entry["filters"] = rename(entry.get("filters", []))
        self.active_filters = rename(self.active_filters)

    def _on_filter_deleted(self, name):
        for entry in self.saved_timers:
            entry["filters"] = [f for f in entry.get("filters", []) if f != name]
        self.active_filters = [f for f in self.active_filters if f != name]

    def _after_filter_change(self):
        self._save_saved_timers()
        save_filters(self.filter_order)
        # Never leave the list filtered by a name the menu no longer offers
        self.active_filters = [f for f in self.active_filters if f in self.filter_order]
        self._rebuild_filter_menu()
        self._refresh_saved_list()

    def _set_site_icons(self, site_icons):
        self.site_icons = site_icons
        save_site_icons(site_icons)

    def _clear_all_timers(self):
        if not self.active_running:
            return
        reply = QMessageBox.question(
            self,
            "Clear All Timers",
            "Are you sure you want to clear all running timers?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self._bulk = True
            try:
                for row in list(self.active_running):
                    row.cancel()
            finally:
                self._bulk = False
            self._running_changed()

    def closeEvent(self, event):
        self._save_window_geometry()
        # Running timers end with the app. The pop-out and finish boxes are
        # separate top-level windows, so quit explicitly or they'd keep a
        # hidden app running.
        super().closeEvent(event)
        QApplication.quit()

    def _save_window_geometry(self):
        """Remember where this window and any open pop-outs are, and their sizes."""
        geometries = {"window_geometry": self.saveGeometry()}
        # Quitting doesn't close the pop-outs, so they can't save themselves
        for pop in self._open_popouts():
            geometries[POPOUT_KEYS[pop.KIND][1]] = pop.saveGeometry()
        save_geometries(geometries)

    def _open_popouts(self):
        return [pop for pop in self.popouts.values() if pop.isVisible()]

    def _pop_out(self, kind):
        """Open the "running" or "upcoming" pop-out, or bring it to the front if it's open."""
        existing = self.popouts.get(kind)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        pop = RunningPopOut(self) if kind == "running" else UpcomingPopOut(self)
        self.popouts[kind] = pop
        pop.show()

    def _apply_settings_from(self, dlg):
        new_y, new_r, new_popup, new_sound, new_ontop, new_sites, new_hours = dlg.get_values()

        # update thresholds & flags
        self.yellow_threshold = new_y * 60
        self.red_threshold    = new_r * 60
        self.alert_popup      = new_popup
        self.alert_sound      = new_sound
        self.popout_ontop     = new_ontop
        self.upcoming_hours   = new_hours

        # **new**: update your in-memory site_icons
        self.site_icons = new_sites

        save_settings(
            new_y, new_r,
            new_popup, new_sound,
            new_ontop,
            new_sites,
            new_hours
        )

        # re-colour running timers for the new thresholds (alerts read self.alert_* directly)
        now = time.time()
        for row in self.active_running:
            row.refresh(now)
        self._show_legend()
        self._refresh_overview(now)
        for kind in POPOUT_KEYS:
            self._apply_popout_ontop(kind)

    def _set_popout_ontop(self, kind, ontop):
        """A pop-out's pin button: keep it on top or not, and remember that."""
        self.popout_ontop[kind] = ontop
        save_popout_ontop(kind, ontop)
        self._apply_popout_ontop(kind)
        dlg = getattr(self, "_settings_dialog", None)
        if dlg is not None:
            dlg.chk_ontop[kind].setChecked(ontop)

    def _apply_popout_ontop(self, kind):
        """Apply "keep on top" to that pop-out, if it's open."""
        pop = self.popouts.get(kind)
        if pop is None or not pop.isVisible():
            return
        ontop = self.popout_ontop[kind]
        pop.btn_pin.setChecked(ontop)
        if bool(pop.windowFlags() & Qt.WindowStaysOnTopHint) != ontop:
            pop.setWindowFlag(Qt.WindowStaysOnTopHint, ontop)
            pop.show()  # changing window flags hides the window

# ─── pop-out windows ───
class PopOutRow(QWidget):
    """A timer listed under the next one in the pop-out."""
    HEIGHT = 34

    def __init__(self):
        super().__init__()
        self.setFixedHeight(self.HEIGHT)
        self._level = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.mark = SiteMark(7)
        self.name = ElidedLabel(px=13, weight=QFont.Medium)
        self.ends = make_label("", 12, color=MUTED)
        self.ends.setFixedWidth(44)
        self.time = make_label("", 15)
        self.time.setFixedWidth(72)
        self.time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.mark)
        row.addWidget(self.name, 1)
        row.addWidget(self.ends)
        row.addWidget(self.time)

    def show_timer(self, timer, main_win):
        self.setVisible(timer is not None)
        if timer is None:
            return
        level = main_win.urgency(timer.remaining)
        if level != self._level:
            self._level = level
            set_color(self.time, URGENCY[level]["text"])
        self.mark.set_site(timer.entry.get("site", ""))
        self.name.setText(timer.entry["name"])
        self.ends.setText(datetime.fromtimestamp(timer.end_time).strftime("%H:%M"))
        self.time.setText(format_hms(timer.remaining))

    def paintEvent(self, event):
        _row_line(self)


class PopOut(QWidget):
    """
    One of the main window's cards in a small window of its own, with a pin
    to keep it on top. It can be resized, and reopens where it was left at
    the same size. KIND ("running" or "upcoming") picks its settings.
    """
    KIND = None

    def __init__(self, main_window, title, heading):
        super().__init__(None, Qt.Window)

        # Keep on top if enabled
        if main_window.popout_ontop[self.KIND]:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.setWindowTitle(title)
        self.main_win = main_window
        self.setMinimumWidth(300)

        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 16)
        self.body.setSpacing(10)

        top = QHBoxLayout()
        self.heading = make_label(heading, 12, QFont.Medium, MUTED)
        top.addWidget(self.heading)
        top.addStretch()
        self.btn_pin = pill_button(height=32, tooltip="Keep on top")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setIcon(make_icon("pin", TEXT, 14, 2.2, checked_color=BG))
        self.btn_pin.setIconSize(QSize(14, 14))
        self.btn_pin.setChecked(main_window.popout_ontop[self.KIND])
        self.btn_pin.clicked.connect(lambda on: main_window._set_popout_ontop(self.KIND, on))
        top.addWidget(self.btn_pin)
        self.body.addLayout(top)

    def place(self, size, pos):
        """
        Put it back where it was left. The first time, open it at this size
        and position, as long as both ends of the title bar would be on a
        screen (that monitor may have been unplugged). Probe just inside the
        bar: Windows' invisible frame puts a window that's flush with a
        screen's left edge at x≈-7.
        """
        saved = load_geometry(POPOUT_KEYS[self.KIND][1])
        if saved is not None and self.restoreGeometry(saved):
            return
        self.resize(size)
        title_bar = (pos + QPoint(20, 15), pos + QPoint(self.width() - 20, 15))
        if not all(QApplication.screenAt(p) for p in title_bar):
            pos = QApplication.primaryScreen().availableGeometry().topLeft() + QPoint(50, 50)
        self.move(pos)

    def closeEvent(self, event):
        save_geometries({POPOUT_KEYS[self.KIND][1]: self.saveGeometry()})
        super().closeEvent(event)


class RunningPopOut(PopOut):
    """The running timers: the next one to finish, and as many after it as fit."""
    KIND = "running"

    def __init__(self, main_window):
        super().__init__(main_window, "Running", "Next up")
        v = self.body

        # The next timer, large, on a card coloured by how soon it ends
        self.hero = QFrame()
        self.hero.setObjectName("Hero")
        hero_row = QHBoxLayout(self.hero)
        hero_row.setContentsMargins(16, 12, 16, 12)
        hero_row.setSpacing(12)
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.hero_mark = SiteMark()
        self.hero_name = ElidedLabel(px=15, weight=QFont.DemiBold)
        name_row.addWidget(self.hero_mark)
        name_row.addWidget(self.hero_name, 1)
        text_col.addLayout(name_row)
        self.hero_ends = make_label("", 12)
        text_col.addWidget(self.hero_ends)
        hero_row.addLayout(text_col, 1)
        self.hero_time = make_label("", 44, QFont.Light)
        font = self.hero_time.font()
        font.setLetterSpacing(QFont.AbsoluteSpacing, -1.5)
        self.hero_time.setFont(font)
        hero_row.addWidget(self.hero_time)
        v.addWidget(self.hero)
        self._hero_level = None

        # The rest of the window: the timers after the next one, as many as
        # fit its height, or "No active timers". It doesn't ask for any
        # height itself, so the window can be made as short as the card.
        self.area = QWidget()
        self.area.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.area.installEventFilter(self)
        self.more_rows = QVBoxLayout(self.area)
        self.more_rows.setContentsMargins(0, 0, 0, 0)
        self.more_rows.setSpacing(0)
        self.empty = make_label("No active timers", 15, color=MUTED)
        self.empty.setAlignment(Qt.AlignCenter)
        self.more_rows.addWidget(self.empty, 1)
        self.more_rows.addStretch()
        self.more = []  # PopOutRows, made as the window gets room for them
        v.addWidget(self.area, 1)

        self.refresh()
        # First time: the position older versions set in Settings
        self.place(QSize(380, 264), QPoint(main_window.popout_x, main_window.popout_y))

    def eventFilter(self, obj, event):
        if obj is self.area and event.type() == QEvent.Resize:
            self.refresh()  # room for more rows, or fewer
        return super().eventFilter(obj, event)

    def refresh(self):
        """Show the timers closest to finishing (the main window calls this every second)."""
        active = sorted(self.main_win.active_running, key=lambda r: r.end_time)
        self.empty.setVisible(not active)
        self.hero.setVisible(bool(active))
        if active:
            first = active[0]
            level = self.main_win.urgency(first.remaining)
            if level != self._hero_level:
                self._hero_level = level
                colors = URGENCY[level]
                self.hero.setStyleSheet(
                    f"QFrame#Hero {{ background: {colors['card']}; "
                    f"border: 1px solid {colors['card_border']}; border-radius: 18px; }}")
                set_color(self.hero_time, colors["text"])
                set_color(self.hero_ends, colors["card_sub"])
            self.hero_mark.set_site(first.entry.get("site", ""))
            self.hero_name.setText(first.entry["name"])
            self.hero_ends.setText(f"Ends {datetime.fromtimestamp(first.end_time):%H:%M}")
            self.hero_time.setText(format_short(first.remaining))

        rest = active[1:1 + self.area.height() // PopOutRow.HEIGHT]
        while len(self.more) < len(rest):
            row = PopOutRow()
            self.more_rows.insertWidget(self.more_rows.count() - 1, row)
            self.more.append(row)
        for i, row in enumerate(self.more):
            row.show_timer(rest[i] if i < len(rest) else None, self.main_win)


class UpcomingPopOut(PopOut):
    """
    The Upcoming timeline. At first it shows the hours set in Settings;
    making it wider shows further ahead, and taller gives labels more rows.
    """
    KIND = "upcoming"

    def __init__(self, main_window):
        super().__init__(main_window, "Upcoming", "")
        self.heading.setTextFormat(Qt.RichText)
        self.timeline = Timeline(main_window, stretches=True)
        self.body.addWidget(self.timeline, 1)
        self.refresh()
        self.timeline.installEventFilter(self)

        # First time: over the Upcoming card, wide enough for the set hours
        margins = self.body.contentsMargins()
        size = QSize(Timeline.BASE_WIDTH + margins.left() + margins.right(), self.sizeHint().height())
        card = main_window.timeline_card
        self.place(size, card.mapToGlobal(QPoint((card.width() - size.width()) // 2, 0)))

    def eventFilter(self, obj, event):
        if obj is self.timeline and event.type() == QEvent.Resize:
            self.refresh()  # wider shows further ahead, so fewer are "later"
        return super().eventFilter(obj, event)

    def refresh(self):
        """Redraw for the time now (the main window calls this every second)."""
        self.heading.setText(upcoming_title(self.timeline.later(time.time())))
        self.timeline.refresh()


class ManageFiltersDialog(QDialog):
    def __init__(self, parent, filters_list, on_done=None,
                 on_rename=None, on_delete=None):
        super().__init__(parent)
        # remove the “?” help button
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowTitle("Manage Filters")

        self.filters = filters_list
        self.on_done = on_done
        # on_rename(old, new) / on_delete(name) let the owner update the
        # timers that carry the filter, before on_done saves everything.
        self.on_rename = on_rename
        self.on_delete = on_delete

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Add Filter… button
        btn_add = pill_button("Add Filter…", "plus", height=34)
        btn_add.clicked.connect(self._add_filter)
        layout.addWidget(btn_add, alignment=Qt.AlignLeft)

        # List of filters
        self.list_widget = QListWidget()
        for f in self.filters:
            self.list_widget.addItem(QListWidgetItem(f))
        layout.addWidget(self.list_widget)

        # up / down / rename / delete controls
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)

        self.btn_up = pill_button(icon="up", height=32, tooltip="Move this filter up")
        self.btn_up.clicked.connect(self._move_up)
        hl.addWidget(self.btn_up)

        self.btn_down = pill_button(icon="down", height=32, tooltip="Move this filter down")
        self.btn_down.clicked.connect(self._move_down)
        hl.addWidget(self.btn_down)

        self.btn_rename = pill_button(icon="edit", height=32, tooltip="Rename this filter")
        self.btn_rename.clicked.connect(self._rename_filter)
        hl.addWidget(self.btn_rename)

        self.btn_delete = pill_button(icon="x", height=32, tooltip="Delete this filter", stroke=2.2)
        self.btn_delete.clicked.connect(self._delete_filter)
        hl.addWidget(self.btn_delete)
        hl.addStretch()

        layout.addWidget(row)

        # OK button
        btn_ok = QPushButton("OK")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        layout.addWidget(btn_ok, alignment=Qt.AlignCenter)

        # Wire selection change once
        self.list_widget.currentRowChanged.connect(self._update_buttons)

        # Nothing is selected yet (currentRow() is -1), so this disables everything
        self._update_buttons(self.list_widget.currentRow())

        # clamp dialog width
        self.setFixedWidth(200)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    def _update_buttons(self, idx):
        count = self.list_widget.count()
        self.btn_up.setEnabled(idx > 0)
        self.btn_down.setEnabled(0 <= idx < count - 1)
        self.btn_rename.setEnabled(idx >= 0)
        self.btn_delete.setEnabled(idx >= 0)

    def _add_filter(self):
        text, ok = QInputDialog.getText(self, "Add New Filter", "Filter name:")
        if not ok or not text.strip():
            return
        name = text.strip()
        if name in self.filters:
            QMessageBox.information(self, "Duplicate Filter", f"‘{name}’ already exists.")
            return
        self.filters.append(name)
        self.list_widget.addItem(QListWidgetItem(name))
        self._update_buttons(self.list_widget.currentRow())
        if self.on_done:
            self.on_done()

    def _rename_filter(self):
        idx = self.list_widget.currentRow()
        if idx < 0:
            return
        old = self.filters[idx]
        new, ok = QInputDialog.getText(self, "Rename Filter", f"New name for '{old}':", text=old)
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        if new in self.filters:
            QMessageBox.warning(self, "Duplicate Filter", f"‘{new}’ already exists.")
            return
        self.filters[idx] = new
        self.list_widget.item(idx).setText(new)
        if self.on_rename:
            self.on_rename(old, new)
        if self.on_done:
            self.on_done()

    def _delete_filter(self):
        idx = self.list_widget.currentRow()
        if idx < 0:
            return
        name = self.filters[idx]
        reply = QMessageBox.question(
            self,
            "Delete Filter",
            f"Are you sure you want to delete '{name}'?\nAll timers in this filter will become unfiltered.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        self.filters.pop(idx)
        self.list_widget.takeItem(idx)
        self._update_buttons(self.list_widget.currentRow())
        if self.on_delete:
            self.on_delete(name)
        if self.on_done:
            self.on_done()

    def _move_up(self):
        idx = self.list_widget.currentRow()
        if idx <= 0:
            return
        self.filters[idx-1], self.filters[idx] = self.filters[idx], self.filters[idx-1]
        item = self.list_widget.takeItem(idx)
        self.list_widget.insertItem(idx-1, item)
        self.list_widget.setCurrentRow(idx-1)
        if self.on_done:
            self.on_done()

    def _move_down(self):
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self.filters)-1:
            return
        self.filters[idx], self.filters[idx+1] = self.filters[idx+1], self.filters[idx]
        item = self.list_widget.takeItem(idx)
        self.list_widget.insertItem(idx+1, item)
        self.list_widget.setCurrentRow(idx+1)
        if self.on_done:
            self.on_done()

class ManageSiteIconsDialog(QDialog):
    def __init__(self, parent, site_icons, on_done=None):
        super().__init__(parent)
        # Remove the “?” help button
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowTitle("Manage Site Icons")

        self.site_icons = site_icons
        self.on_done    = on_done

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Add Icon…
        btn_add = pill_button("Add Icon…", "plus", height=34)
        btn_add.clicked.connect(self._add_icon)
        layout.addWidget(btn_add, alignment=Qt.AlignLeft)

        # Scrollable list of icons
        self.list_widget = QListWidget()
        for ic in self.site_icons:
            self.list_widget.addItem(QListWidgetItem(ic))
        layout.addWidget(self.list_widget)

        # up / down / remove controls
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        self.btn_up     = pill_button(icon="up", height=32, tooltip="Move this icon up")
        self.btn_down   = pill_button(icon="down", height=32, tooltip="Move this icon down")
        self.btn_remove = pill_button(icon="x", height=32, tooltip="Remove this icon", stroke=2.2)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)
        self.btn_remove.clicked.connect(self._remove_icon)
        for w in (self.btn_up, self.btn_down, self.btn_remove):
            hl.addWidget(w)
        hl.addStretch()
        layout.addWidget(row)

        # ─── Reset to Default ───
        btn_reset = QPushButton("Reset to Default")
        btn_reset.clicked.connect(self._reset_defaults)
        layout.addWidget(btn_reset, alignment=Qt.AlignCenter)

        # Close button
        btn_close = QPushButton("OK")
        btn_close.setDefault(True)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignCenter)

        # Wire up selection changes
        self.list_widget.currentRowChanged.connect(self._update_buttons)
        # Nothing is selected yet (currentRow() is -1), so this disables everything
        self._update_buttons(self.list_widget.currentRow())

        # Clamp width so it stays narrow
        self.setFixedWidth(175)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    def _add_icon(self):
        # Only suggest icons not already in the list, so the default pick is addable
        suggestions = [s for s in DEFAULT_ICONS + ["🌙", "⭐", "☕", "💡"] if s not in self.site_icons]
        dlg = AddIconDialog(self, suggestions)
        if dlg.exec_() != QDialog.Accepted:
            return

        icon = dlg.get_icon()
        if not icon:
            return
        if icon in self.site_icons:
            QMessageBox.information(self, "Exists", f"‘{icon}’ is already in the list.")
            return

        self.site_icons.append(icon)
        self.list_widget.addItem(QListWidgetItem(icon))
        self._update_buttons(self.list_widget.currentRow())
        if self.on_done:
            self.on_done()

    def _remove_icon(self):
        idx = self.list_widget.currentRow()
        if idx < 0:
            return
        icon = self.site_icons[idx]
        reply = QMessageBox.question(
            self, "Remove Icon",
            f"Are you sure you want to remove ‘{icon}’?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.site_icons.pop(idx)
            self.list_widget.takeItem(idx)
            self._update_buttons(self.list_widget.currentRow())
            if self.on_done:
                self.on_done()


    def _move_up(self):
        idx = self.list_widget.currentRow()
        if idx <= 0:
            return
        self.site_icons[idx-1], self.site_icons[idx] = (
            self.site_icons[idx],
            self.site_icons[idx-1]
        )
        item = self.list_widget.takeItem(idx)
        self.list_widget.insertItem(idx-1, item)
        self.list_widget.setCurrentRow(idx-1)
        if self.on_done:
            self.on_done()


    def _move_down(self):
        idx = self.list_widget.currentRow()
        if idx < 0 or idx >= len(self.site_icons)-1:
            return
        self.site_icons[idx], self.site_icons[idx+1] = (
            self.site_icons[idx+1],
            self.site_icons[idx]
        )
        item = self.list_widget.takeItem(idx)
        self.list_widget.insertItem(idx+1, item)
        self.list_widget.setCurrentRow(idx+1)
        if self.on_done:
            self.on_done()

    def _reset_defaults(self):
        reply = QMessageBox.question(
            self,
            "Reset Site Icons",
            "Reset to default icons? This will remove all custom ones.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # restore the global default list
            self.site_icons[:] = DEFAULT_ICONS.copy()
            # refresh the QListWidget
            self.list_widget.clear()
            for ic in self.site_icons:
                self.list_widget.addItem(QListWidgetItem(ic))
            self._update_buttons(self.list_widget.currentRow())
            if self.on_done:
                self.on_done()

    def _update_buttons(self, idx):
        count = self.list_widget.count()
        self.btn_up.setEnabled(idx > 0)
        self.btn_down.setEnabled(0 <= idx < count-1)
        self.btn_remove.setEnabled(idx >= 0)

class AddIconDialog(QDialog):
    def __init__(self, parent, suggestions):
        # 1) Properly call the base constructor
        super().__init__(parent)

        # 2) Remove the “?” help button
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowTitle("Add New Icon")

        # 3) Force a fixed width
        self.setFixedWidth(280)

        # ─── Layout ───
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Instruction label (wrapped)
        lbl = QLabel("Choose from the list, paste an emoji, or type a site name (max 20 chars):")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        # Editable combo with suggestions
        self.combo = QComboBox()
        self.combo.addItems(suggestions)
        self.combo.addItem("")  # blank
        self.combo.setEditable(True)
        layout.addWidget(self.combo)

        # OK / Cancel buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_icon(self) -> str:
        # return up to 20 chars
        return self.combo.currentText().strip()[:20]







def _install_excepthook():
    """
    PyQt5 aborts the whole app on an exception raised inside a slot, which would
    silently drop every running timer. Log it and tell the user instead.
    """
    showing = False

    def hook(exc_type, exc, tb):
        nonlocal showing
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}  v{VERSION} ---\n{text}\n")
        except OSError:
            pass
        if sys.stderr is not None:  # None in the windowed exe
            sys.__excepthook__(exc_type, exc, tb)
        # One dialog at a time, so a repeating error can't stack up popups.
        if QApplication.instance() is not None and not showing:
            showing = True
            try:
                QMessageBox.critical(
                    None, APP_NAME,
                    f"Something went wrong:\n{exc}\n\nDetails were saved to:\n{ERROR_LOG_FILE}"
                )
            finally:
                showing = False

    sys.excepthook = hook


if __name__ == "__main__":
    _install_excepthook()
    # Follow Windows' display scaling, fractional settings like 125% included,
    # instead of drawing everything at a fixed pixel size
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    # point to your .ico / .png file on disk (use an absolute or relative path)
    app.setWindowIcon(QIcon(str(CODE_DIR / "Slug.png")))
    load_fonts()
    apply_theme(app)

    # One copy at a time: each one saves its whole in-memory timer list, so
    # two copies would silently overwrite each other's changes.
    # The default stale time lets a lock left by a crash, logoff or another PC
    # (APPDATA roams) be reclaimed, while a running copy holds its lock file
    # open so it can't be taken. Only a real "held by another copy" blocks
    # startup; any other lock error just skips the guard.
    instance_lock = QLockFile(str(DATA_DIR / "slugware.lock"))
    if not instance_lock.tryLock(100) and instance_lock.error() == QLockFile.LockFailedError:
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running.")
        sys.exit(0)

    window = MainWindow()
    window.show()
    if STARTUP_WARNINGS:
        QTimer.singleShot(0, lambda: QMessageBox.warning(
            window, APP_NAME, "\n\n".join(STARTUP_WARNINGS)))
    _warnings_live = True  # later data-file problems are shown as they happen
    sys.exit(app.exec_())

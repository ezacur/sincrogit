"""Visual theme for the SincroGit GUI (QSS stylesheet, light + dark).

One place owns every color and spacing rule; widgets stay stock Qt and pick the
look up from the app-level stylesheet. `apply_theme(app, mode)` is called once at
startup; mode "auto" follows Windows' app theme (AppsUseLightTheme).

Buttons can opt into variants with a dynamic property:
    btn.setProperty("cssClass", "primary" | "danger" | "accent")
"""

import sys

# ------------------------------------------------------------------ palettes
_LIGHT = {
    "is_dark":   False,
    "bg":        "#f4f6f8",   # window background
    "surface":   "#ffffff",   # panels, inputs, tables
    "sunken":    "#eceff3",   # table headers, subtle fills
    "border":    "#d7dce3",
    "text":      "#1f2430",
    "muted":     "#6b7280",
    "accent":    "#2e7dd1",
    "accent_hi": "#3b8de2",   # hover
    "accent_lo": "#2868ad",   # pressed
    "on_accent": "#ffffff",
    "success":   "#2e9e5b",
    "warning":   "#a87900",
    "danger":    "#d23f3f",
    "danger_hi": "#e05252",
    "sel_bg":    "#dce9f7",   # table selection
    "sel_text":  "#1f2430",
}

_DARK = {
    "is_dark":   True,
    "bg":        "#20242b",
    "surface":   "#2a2f38",
    "sunken":    "#252a32",
    "border":    "#3c434e",
    "text":      "#e8eaed",
    "muted":     "#9aa3af",
    "accent":    "#4da3ff",
    "accent_hi": "#66b1ff",
    "accent_lo": "#3b8de2",
    "on_accent": "#10141a",
    "success":   "#4cc07a",
    "warning":   "#e0a400",
    "danger":    "#e06060",
    "danger_hi": "#ec7272",
    "sel_bg":    "#33415a",
    "sel_text":  "#e8eaed",
}


def _windows_prefers_dark() -> bool:
    """Windows app theme (AppsUseLightTheme = 0 -> dark). False anywhere else."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except OSError:
        return False


def palette(mode: str = "auto") -> dict:
    """The color tokens for `mode` ("auto" | "light" | "dark")."""
    if mode == "dark" or (mode == "auto" and _windows_prefers_dark()):
        return _DARK
    return _LIGHT


def stylesheet(mode: str = "auto") -> str:
    p = palette(mode)
    return f"""
* {{
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 10pt;
}}
QMainWindow, QDialog {{
    background: {p['bg']};
}}
QWidget {{
    color: {p['text']};
}}
QLabel {{
    background: transparent;
}}
QLabel[cssClass="muted"] {{
    color: {p['muted']};
}}

/* ----------------------------------------------------------------- tabs */
QTabWidget::pane {{
    border: 1px solid {p['border']};
    border-radius: 6px;
    background: {p['surface']};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {p['muted']};
    border: 1px solid transparent;
    border-bottom: none;
    padding: 7px 18px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{
    background: {p['surface']};
    color: {p['text']};
    border-color: {p['border']};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    color: {p['text']};
}}

/* -------------------------------------------------------------- buttons */
QPushButton {{
    background: {p['surface']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    border-color: {p['accent']};
    color: {p['accent']};
}}
QPushButton:pressed {{
    background: {p['sunken']};
}}
QPushButton:disabled {{
    color: {p['muted']};
    background: {p['sunken']};
    border-color: {p['border']};
}}
QPushButton[cssClass="primary"] {{
    background: {p['accent']};
    color: {p['on_accent']};
    border: 1px solid {p['accent']};
    font-weight: 600;
}}
QPushButton[cssClass="primary"]:hover {{
    background: {p['accent_hi']};
    color: {p['on_accent']};
}}
QPushButton[cssClass="primary"]:pressed {{
    background: {p['accent_lo']};
}}
QPushButton[cssClass="accent"] {{
    color: {p['accent']};
    border: 1px solid {p['accent']};
    font-weight: 600;
    background: transparent;
}}
QPushButton[cssClass="accent"]:hover {{
    background: {p['sel_bg']};
}}
QPushButton[cssClass="danger"] {{
    color: {p['danger']};
    border: 1px solid {p['danger']};
    background: transparent;
}}
QPushButton[cssClass="danger"]:hover {{
    background: {p['danger']};
    color: {p['on_accent']};
}}
QToolButton {{
    background: {p['surface']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    padding: 5px 12px;
}}
QToolButton:hover {{
    border-color: {p['accent']};
    color: {p['accent']};
}}
/* Segmented switch (a joined pair of exclusive toggles — mode selectors that
   must not read as two independent buttons). */
QToolButton[cssClass="seg"]:checked {{
    background: {p['accent']};
    color: {p['on_accent']};
    border-color: {p['accent']};
    font-weight: 600;
}}
QToolButton[segPos="first"] {{
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
}}
QToolButton[segPos="last"] {{
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
    margin-left: -1px;
}}

/* --------------------------------------------------------------- inputs */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {p['surface']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: {p['accent']};
    selection-color: {p['on_accent']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {p['accent']};
}}
/* A closed combo must NOT read as a push button (they used to share the same
   fill/border/radius with no visible arrow — Ernesto: "los botones y los
   popup listbox se ven iguales"). The tell is a recessed arrow WELL with a
   separator and an explicit triangle: "this opens a list", not "this acts". */
QComboBox {{
    padding: 4px 30px 4px 8px;   /* room for the arrow well */
}}
QComboBox::drop-down {{
    border: none;
    border-left: 1px solid {p['border']};
    width: 22px;
    background: {p['sunken']};
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}}
QComboBox::down-arrow {{
    /* a QSS-drawn triangle (no image asset): zero-size box, colored top border */
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p['muted']};
}}
QComboBox:hover::down-arrow {{
    border-top-color: {p['accent']};
}}
/* Buttons that open a menu spell it out with a ▾ in their text instead of
   Qt's tiny default indicator (invisible under a custom stylesheet). */
QPushButton::menu-indicator {{
    image: none;
    width: 0;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 18px;
    margin: 1px 2px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background: {p['sel_bg']};
    border-radius: 3px;
}}
QComboBox QAbstractItemView {{
    background: {p['surface']};
    border: 1px solid {p['border']};
    selection-background-color: {p['sel_bg']};
    selection-color: {p['sel_text']};
}}
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p['border']};
    border-radius: 4px;
    background: {p['surface']};
}}
QCheckBox::indicator:hover {{
    border-color: {p['accent']};
}}
QCheckBox::indicator:checked {{
    background: {p['accent']};
    border-color: {p['accent']};
    image: none;
}}

/* --------------------------------------------------------------- tables */
QTableWidget, QTreeView, QListView {{
    background: {p['surface']};
    alternate-background-color: {p['sunken']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    gridline-color: transparent;
    selection-background-color: {p['sel_bg']};
    selection-color: {p['sel_text']};
    outline: none;
}}
QTableWidget::item, QTreeView::item {{
    padding: 3px 6px;
    border: none;
}}
QHeaderView::section {{
    background: {p['sunken']};
    color: {p['muted']};
    border: none;
    border-bottom: 1px solid {p['border']};
    padding: 6px 8px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background: {p['sunken']};
    border: none;
}}

/* ------------------------------------------------------------ group box */
QGroupBox {{
    border: 1px solid {p['border']};
    border-radius: 8px;
    margin-top: 12px;
    padding: 10px 8px 6px 8px;
    background: {p['surface']};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {p['accent']};
    font-weight: 600;
}}

/* ------------------------------------------------------------ scrollbar */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p['border']};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p['muted']};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p['border']};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p['muted']};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0; width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ---------------------------------------------------- busy indicator */
QProgressBar[cssClass="busy"] {{
    background: {p['sunken']};
    border: none;
    border-radius: 2px;
}}
QProgressBar[cssClass="busy"]::chunk {{
    background: {p['accent']};
    border-radius: 2px;
}}

/* ---------------------------------------------------------------- misc */
QSplitter::handle {{
    background: {p['border']};
}}
QSplitter::handle:vertical {{ height: 3px; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QToolTip {{
    background: {p['surface']};
    color: {p['text']};
    border: 1px solid {p['border']};
    padding: 5px 8px;
    border-radius: 4px;
}}
QMenu {{
    background: {p['surface']};
    border: 1px solid {p['border']};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px 6px 16px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {p['sel_bg']};
}}
QMenu::separator {{
    height: 1px;
    background: {p['border']};
    margin: 4px 8px;
}}
"""


def apply_theme(app, mode: str = "auto") -> dict:
    """Apply the stylesheet to the QApplication. Returns the palette in use so
    callers can color custom elements (state labels, diff HTML) consistently."""
    app.setStyleSheet(stylesheet(mode))
    return palette(mode)

"""Cyberpunk / Dark Graphite theme tokens and Qt stylesheet."""
from __future__ import annotations

from pathlib import Path

BG = "#0B0E13"
BG_ALT = "#121722"
PANEL = "#161C28"
BG_ALT2 = "#1A2130"      # фон интерактивных элементов при наведении
LINE = "#232B3B"
TEXT = "#E6EDF7"
MUTED = "#8A97AD"
ACCENT = "#00E5FF"
ACCENT_2 = "#FF2D95"
OK = "#41E08A"
WARN = "#FFC857"

_ASSETS = Path(__file__).resolve().parent / "assets"
CHEVRON = (_ASSETS / "chevron_down.svg").as_posix()

QSS = f"""
* {{ font-family: 'Inter', 'Segoe UI', 'SF Pro Text', sans-serif; font-size: 13px; }}
QWidget {{ background: {BG}; color: {TEXT}; }}
QLabel, QCheckBox, QRadioButton, QToolButton {{ background: transparent; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 12px; background: {PANEL}; top: -1px; }}
QTabBar::tab {{ background: {BG_ALT}; color: {MUTED}; border: 1px solid {LINE};
    border-bottom: none; border-top-left-radius: 10px; border-top-right-radius: 10px;
    padding: 9px 16px; margin-right: 4px; font-weight: 700; }}
QTabBar::tab:selected {{ background: {PANEL}; color: {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QMainWindow::separator {{ background: {LINE}; width: 2px; height: 2px; }}
QFrame#Panel, QGroupBox {{
    background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px;
}}
QGroupBox {{ margin-top: 14px; padding: 14px 10px 10px 10px; font-weight: 700; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {ACCENT}; }}
QLabel#Title {{ font-size: 18px; font-weight: 800; color: {TEXT}; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Badge {{
    color: {BG}; background: {ACCENT}; border-radius: 8px; padding: 3px 10px; font-weight: 800;
}}
QPushButton {{
    background: {BG_ALT}; border: 1px solid {LINE}; border-radius: 10px;
    padding: 8px 14px; color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {LINE}; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    color: #06090F; font-weight: 900; border: none; padding: 12px 18px; font-size: 14px;
}}
QPushButton#Primary:disabled {{ background: {LINE}; color: {MUTED}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {BG_ALT}; border: 1px solid {LINE}; border-radius: 8px; padding: 5px 8px;
    selection-background-color: {ACCENT}; selection-color: {BG};
}}
/* Выпадающие списки должны ВЫГЛЯДЕТЬ как выпадающие: рамка, hover и стрелка */
QComboBox {{
    padding-right: 32px; min-height: 26px;
    /* иначе Fusion заливает текущий пункт цветом выделения */
    selection-background-color: transparent; selection-color: {TEXT};
}}
QComboBox:hover {{ border-color: {ACCENT}; background: {BG_ALT2}; }}
QComboBox:focus, QComboBox:on {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 26px; height: 100%; border: none; border-left: 1px solid {LINE};
    background: transparent;
}}
QComboBox::down-arrow {{
    image: url("{CHEVRON}"); width: 14px; height: 14px;
    subcontrol-origin: padding; subcontrol-position: center right; right: 6px;
}}

QComboBox QAbstractItemView {{
    background: {BG_ALT}; border: 1px solid {ACCENT}; border-radius: 8px;
    selection-background-color: {ACCENT}; selection-color: #06090F;
    outline: none; padding: 4px;
}}
QSlider::groove:horizontal {{ height: 4px; background: {LINE}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 14px; margin: -6px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QProgressBar {{
    background: {BG_ALT}; border: 1px solid {LINE}; border-radius: 8px; height: 18px;
    text-align: center; color: {TEXT};
}}
QProgressBar::chunk {{
    border-radius: 7px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {OK});
}}
QTableWidget {{
    background: {PANEL}; gridline-color: {LINE}; border: 1px solid {LINE}; border-radius: 10px;
    selection-background-color: {ACCENT}; selection-color: {BG};
}}
QHeaderView::section {{
    background: {BG_ALT}; color: {MUTED}; border: none; border-right: 1px solid {LINE};
    padding: 6px; font-weight: 700;
}}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QStatusBar {{ color: {MUTED}; border-top: 1px solid {LINE}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {ACCENT}; }}
"""

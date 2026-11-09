"""Cyberpunk / Dark Graphite theme tokens and Qt stylesheet.

Design goals (v1.1):
  * выше контраст текста и интерактивных элементов (AA), мягкие акценты;
  * скруглённые углы 10–14 px, тонкие рамки, аккуратные hover/focus-состояния;
  * прогресс-бар с градиентом и читаемым текстом (%p% + этап);
  * единая типографика Inter (загружается в main.py из assets/fonts).
"""
from __future__ import annotations

from pathlib import Path

BG = "#0B0E13"
BG_ALT = "#121722"
BG_ALT2 = "#1A2231"      # фон интерактивных элементов при наведении
PANEL = "#161D29"
PANEL_HI = "#1C2432"     # верхний стоп градиента панелей
LINE = "#273043"         # рамки: чуть светлее прежнего для контраста
LINE_SOFT = "#1E2635"    # тонкие разделители
TEXT = "#E9EEF6"
MUTED = "#9AA7BC"        # вторичный текст: было #8A97AD — тускло на тёмном
ACCENT = "#00E5FF"
ACCENT_SOFT = "#35ECFF"
ACCENT_2 = "#FF2D95"
ACCENT_2_SOFT = "#FF5CAD"
OK = "#41E08A"
WARN = "#FFC857"
SELECTION = "#12374A"    # фон выделения (вместо неон-заливки)
SELECTION_TEXT = "#8DF3FF"

_ASSETS = Path(__file__).resolve().parent / "assets"
CHEVRON = (_ASSETS / "chevron_down.svg").as_posix()

QSS = f"""
* {{ font-family: 'Inter', 'Segoe UI', 'SF Pro Text', sans-serif; font-size: 13px; }}
QWidget {{ background: {BG}; color: {TEXT}; }}
QLabel, QCheckBox, QRadioButton, QToolButton {{ background: transparent; }}

/* ---------- вкладки ---------- */
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 14px; background: {PANEL}; top: -1px; }}
QTabBar::tab {{ background: {BG_ALT}; color: {MUTED}; border: 1px solid {LINE};
    border-bottom: none; border-top-left-radius: 11px; border-top-right-radius: 11px;
    padding: 9px 18px; margin-right: 5px; font-weight: 700; }}
QTabBar::tab:selected {{ background: {PANEL}; color: {ACCENT};
    border-top: 2px solid {ACCENT}; padding-top: 8px; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QMainWindow::separator {{ background: {LINE}; width: 2px; height: 2px; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}

/* ---------- панели и группы ---------- */
QFrame#Panel, QGroupBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {PANEL_HI}, stop:1 {PANEL});
    border: 1px solid {LINE}; border-radius: 14px;
}}
QGroupBox {{ margin-top: 14px; padding: 14px 10px 10px 10px; font-weight: 700; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {ACCENT}; }}

QLabel#Title {{ font-size: 18px; font-weight: 800; color: {TEXT}; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Badge {{
    color: {BG}; background: {ACCENT}; border-radius: 9px; padding: 3px 10px; font-weight: 800;
}}

/* ---------- кнопки ---------- */
QPushButton, QToolButton {{
    background: {BG_ALT}; border: 1px solid {LINE}; border-radius: 11px;
    padding: 8px 15px; color: {TEXT};
}}
QPushButton:hover, QToolButton:hover {{
    border-color: {ACCENT}; color: {ACCENT}; background: {BG_ALT2};
}}
QPushButton:focus, QToolButton:focus {{ border-color: {ACCENT}; outline: none; }}
QPushButton:pressed {{ background: {PANEL_HI}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {MUTED}; border-color: {LINE_SOFT}; background: {BG_ALT}; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    color: #06090F; font-weight: 900; border: none; padding: 12px 18px; font-size: 14px;
    border-radius: 12px;
}}
QPushButton#Primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_SOFT}, stop:1 {ACCENT_2_SOFT});
    color: #06090F;
}}
QPushButton#Primary:disabled {{ background: {LINE}; color: {MUTED}; }}

/* ---------- поля ввода ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {BG_ALT}; border: 1px solid {LINE}; border-radius: 9px; padding: 5px 8px;
    selection-background-color: {ACCENT}; selection-color: {BG};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
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
    background: {BG_ALT}; border: 1px solid {ACCENT}; border-radius: 9px;
    selection-background-color: {ACCENT}; selection-color: #06090F;
    outline: none; padding: 4px;
}}

/* ---------- слайдеры ---------- */
QSlider::groove:horizontal {{ height: 5px; background: {LINE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 {ACCENT}, stop:1 #00A3FF); border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
    border: 2px solid {BG};
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_SOFT}; }}
QSlider::handle:horizontal:disabled {{ background: {MUTED}; }}

/* ---------- прогресс-бар с этапом ---------- */
QProgressBar {{
    background: {BG_ALT}; border: 1px solid {LINE}; border-radius: 11px; height: 22px;
    text-align: center; color: {TEXT}; font-size: 11px; font-weight: 700; padding: 1px;
}}
QProgressBar::chunk {{
    border-radius: 9px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {OK});
}}
QProgressBar#StageProgress {{
    border: 1px solid #1E3E50; background: {BG_ALT};
}}
QProgressBar#StageProgress::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
}}

/* ---------- таблица субтитров ---------- */
QTableWidget {{
    background: {PANEL}; gridline-color: {LINE_SOFT}; border: 1px solid {LINE}; border-radius: 12px;
    selection-background-color: {SELECTION}; selection-color: {SELECTION_TEXT};
}}
QTableWidget::item:selected {{ color: {SELECTION_TEXT}; }}
QHeaderView::section {{
    background: {BG_ALT}; color: {MUTED}; border: none; border-right: 1px solid {LINE_SOFT};
    padding: 7px; font-weight: 700;
}}

/* ---------- чекбоксы ---------- */
QCheckBox::indicator {{
    width: 17px; height: 17px; border-radius: 5px;
    border: 1px solid {LINE}; background: {BG_ALT};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 #00A3FF);
    border-color: {ACCENT};
}}

/* ---------- меню ---------- */
QMenuBar {{ background: {BG}; border-bottom: 1px solid {LINE_SOFT}; }}
QMenuBar::item {{ padding: 6px 12px; border-radius: 8px; }}
QMenuBar::item:selected {{ background: {BG_ALT2}; color: {ACCENT}; }}
QMenu {{
    background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; padding: 5px;
}}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 8px; }}
QMenu::item:selected {{ background: {SELECTION}; color: {SELECTION_TEXT}; }}

/* ---------- скроллбары ---------- */
QScrollBar:vertical {{ background: transparent; width: 11px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; }}
QScrollBar::handle:horizontal {{ background: {LINE}; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {MUTED}; }}

QStatusBar {{ color: {MUTED}; border-top: 1px solid {LINE_SOFT}; background: {BG_ALT}; }}
QStatusBar::item {{ border: none; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {ACCENT};
    border-radius: 8px; padding: 6px 9px; }}
"""

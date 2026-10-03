"""CapText AI Pro — главное окно (UX в духе CapCut / Submagic).

Структура:
    ┌───────────────────────────────┬──────────────────────────┐
    │  Плеер + Safe Zone (иконка)   │  📝 Текст и распознавание │
    │  транспорт, большая кнопка    │  🎨 Стиль и дизайн        │
    │                               │  ⚡ Анимация и позиция    │
    ├───────────────────────────────┤  ▸ Advanced (скрыто)     │
    │  Таблица субтитров (правка)   │                          │
    └───────────────────────────────┴──────────────────────────┘

Никаких сырых коэффициентов в основном UI: пружины, BPM, онсеты и проценты
жанра живут в сворачиваемой секции «Advanced / Для разработчиков».
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (QAction, QColor, QImage, QKeySequence, QPainter, QPalette,
                           QPen)
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFontComboBox, QFormLayout, QFrame, QGroupBox,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QSpinBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
                               QToolButton, QVBoxLayout, QWidget)

from app.audio.audio_processor import AudioProcessor, ProcessOptions
from app.core.models import Project, Style, Word
from app.render.exporter import VideoExporter
from app.render.presets import AutoStyleEngine
from app.render.scene import SubtitleScene
from app.render.style_presets import (ANIMATION_BY_KEY, ANIMATION_PRESETS, POSITION_PRESETS,
                                      POSITION_TITLES, SIZE_PRESETS, STYLE_BY_KEY,
                                      STYLE_PRESETS, apply_position, position_key_for,
                                      size_key_for)
from app.ui.theme import ACCENT, BG_ALT, LINE, MUTED, QSS, TEXT
from app.ui.widgets import CardGrid, Collapsible, PresetCard, SegmentedControl
from app.ui.player import VideoPlayer, multimedia_status
from app.ui.workers import JobRunner

log = logging.getLogger("captext.ui")



# ------------------------------------------------------------------ таймлайн
class SubtitleTable(QTableWidget):
    """Таблица субтитров: текст правится по клику, тайминги — числами."""

    text_edited = Signal(int, str)
    time_edited = Signal(int, float, float)
    seek_requested = Signal(float)

    def __init__(self) -> None:
        super().__init__(0, 4)
        self.setHorizontalHeaderLabels(["#", "Начало", "Конец", "Текст субтитра"])
        self.verticalHeader().setVisible(False)
        self.setColumnWidth(0, 40)
        self.setColumnWidth(1, 86)
        self.setColumnWidth(2, 86)
        self.horizontalHeader().setStretchLastSection(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.SelectedClicked |
                             QTableWidget.AnyKeyPressed)
        self.setAlternatingRowColors(False)
        self._loading = False
        self._ranges: list[tuple[float, float]] = []
        self._row_at: dict[int, int] = {}
        self._active_row = -2
        self.itemChanged.connect(self._on_item_changed)
        self.itemSelectionChanged.connect(self._on_select)

    def load(self, project: Project) -> None:
        self._loading = True
        self._ranges = [(ph.start, ph.end) for ph in project.phrases]
        self._row_at = {}
        for i, (a, b) in enumerate(self._ranges):        # индекс по четвертям секунды
            for q in range(int(a * 4), int(b * 4) + 2):
                self._row_at.setdefault(q, i)
        self._active_row = -2
        self.setRowCount(len(project.phrases))
        for i, ph in enumerate(project.phrases):
            for col, val in ((0, str(i + 1)), (1, f"{ph.start:.2f}"), (2, f"{ph.end:.2f}"),
                             (3, ph.text)):
                item = QTableWidgetItem(val)
                if col == 0:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    item.setForeground(QColor(MUTED))
                self.setItem(i, col, item)
        self._loading = False

    def highlight_at_time(self, t: float) -> None:
        """Подсветить строку, которая звучит сейчас (без рекурсивной перемотки)."""
        row = self._row_at.get(int(t * 4), None)
        if row is None:
            row = -1
            for i, (start, end) in enumerate(self._ranges):
                if start - 0.05 <= t <= end + 0.25:
                    row = i
                    break
        if row == self._active_row:
            return
        self._active_row = row
        for r in range(self.rowCount()):
            active = (r == row)
            for c in range(self.columnCount()):
                item = self.item(r, c)
                if item is None:
                    continue
                item.setBackground(QColor(ACCENT).darker(220) if active
                                   else QColor(0, 0, 0, 0))
                item.setForeground(QColor(ACCENT) if active and c == 3
                                   else (QColor(MUTED) if c == 0 else QColor(TEXT)))
        if row >= 0:
            self.scrollToItem(self.item(row, 3), QTableWidget.EnsureVisible)

    def _on_select(self) -> None:
        rows = self.selectionModel().selectedRows()
        if rows and self.item(rows[0].row(), 1):
            try:
                self.seek_requested.emit(float(self.item(rows[0].row(), 1).text()))
            except ValueError:
                pass

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if col == 3:
            self.text_edited.emit(row, item.text())
        elif col in (1, 2):
            try:
                self.time_edited.emit(row, float(self.item(row, 1).text()),
                                      float(self.item(row, 2).text()))
            except (ValueError, AttributeError):
                pass


# --------------------------------------------------------------- главное окно
def _style_combo(combo: QComboBox) -> None:
    """Явный вид выпадающего списка: стрелка, hover, читаемая текущая строка.

    Fusion рисует текущий пункт невыпадающего QComboBox как элемент списка и
    заливает его цветом выделения из палитры — поэтому гасим Highlight у самого
    комбобокса (всплывающий список стилизуется через QSS и остаётся акцентным).
    """
    combo.setCursor(Qt.PointingHandCursor)
    pal = combo.palette()
    pal.setColor(QPalette.Highlight, QColor(BG_ALT))
    pal.setColor(QPalette.HighlightedText, QColor(TEXT))
    combo.setPalette(pal)
    combo.setMinimumHeight(30)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CapText AI Pro")
        self.resize(1560, 980)

        self.project = Project(global_style=STYLE_BY_KEY["capcut_yellow"].apply(
            AutoStyleEngine.get_style_for_genre("speech_podcast")))
        self.scene = SubtitleScene(self.project)
        self.media_path = ""
        self.runner = JobRunner(self)
        self._updating = False
        self._position_key = "bottom"
        self._position_offset = 0.0
        self._last_report = None

        self._build_ui()
        self._build_menu()
        self.setStyleSheet(QSS)
        self._wire_runner()
        self.style_grid.set_value("capcut_yellow")
        self.anim_grid.set_value("karaoke")
        self._sync_controls()
        QTimer.singleShot(200, self._check_environment)

    # ================================================================= LAYOUT
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([900, 620])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("Откройте видео или аудио, чтобы начать")

    # --------------------------------------------------------------- левая
    def _build_left(self) -> QWidget:
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(14, 12, 8, 12)
        v.setSpacing(10)

        # header
        head = QHBoxLayout()
        title = QLabel("CapText AI Pro")
        title.setObjectName("Title")
        self.lbl_file = QLabel("файл не выбран")
        self.lbl_file.setObjectName("Muted")
        head.addWidget(title)
        head.addSpacing(10)
        head.addWidget(self.lbl_file, 1)
        v.addLayout(head)

        # ---- плеер с оверлеем субтитров (см. app/ui/player.py) ----
        self.player = VideoPlayer()
        self.player.set_scene(self.scene)
        self.player.overlay.box_moved.connect(self._on_box_moved)
        self.player.position_changed.connect(self._on_position)
        self.player.duration_changed.connect(self._on_duration)
        self.player.playing_changed.connect(
            lambda playing: self.btn_play.setText("❚❚" if playing else "▶"))
        self.player.error.connect(lambda m: self.statusBar().showMessage(f"Плеер: {m}"))
        v.addWidget(self.player, 1)

        # toolbar row: safe zone icon + aspect
        bar = QHBoxLayout()
        self.btn_safe = QToolButton()
        self.btn_safe.setText("⛶")
        self.btn_safe.setToolTip("Показать/скрыть безопасные зоны")
        self.btn_safe.setCheckable(True)
        self.btn_safe.setChecked(True)
        self.btn_safe.setCursor(Qt.PointingHandCursor)
        self.btn_safe.toggled.connect(self._toggle_safe)
        self.seg_aspect = SegmentedControl(
            [("9:16", "9:16"), ("16:9", "16:9"), ("1:1", "1:1"), ("4:3", "4:3")], "9:16")
        self.seg_aspect.changed.connect(self._on_aspect)
        self.seg_fit = SegmentedControl(
            [("fit_blur", "Размытые поля"), ("fit", "Поля"), ("fill", "Кроп")], "fit_blur")
        self.seg_fit.changed.connect(self._on_fit_mode)
        bar.addWidget(self.btn_safe)
        bar.addWidget(QLabel("Формат:"))
        bar.addWidget(self.seg_aspect, 1)
        v.addLayout(bar)

        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("Кадр:"))
        bar2.addWidget(self.seg_fit, 1)
        v.addLayout(bar2)

        # transport
        tr = QHBoxLayout()
        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedWidth(46)
        self.btn_play.clicked.connect(self.toggle_play)
        self.seekbar = QSlider(Qt.Horizontal)
        self.seekbar.setRange(0, 1000)
        self.seekbar.sliderMoved.connect(self._on_seek)
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setObjectName("Muted")
        tr.addWidget(self.btn_play)
        tr.addWidget(self.seekbar, 1)
        tr.addWidget(self.lbl_time)
        v.addLayout(tr)

        # primary actions
        row = QHBoxLayout()
        self.btn_open = QPushButton("📂 Открыть медиа")
        self.btn_open.clicked.connect(self.open_media)
        self.btn_process = QPushButton("🚀 Создать субтитры")
        self.btn_process.setObjectName("Primary")
        self.btn_process.clicked.connect(self.run_recognition)
        self.btn_export = QPushButton("🎬 Экспорт")
        self.btn_export.clicked.connect(self.export_video)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_process, 1)
        row.addWidget(self.btn_export)
        v.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setFormat("%p%  —  готов к работе")
        v.addWidget(self.progress)

        cap = QLabel("Субтитры (двойной клик по строке — правка текста)")
        cap.setObjectName("Muted")
        v.addWidget(cap)
        self.table = SubtitleTable()
        self.table.setMinimumHeight(170)
        self.table.text_edited.connect(self._on_text_edited)
        self.table.time_edited.connect(self._on_time_edited)
        self.table.seek_requested.connect(self.seek_seconds)
        v.addWidget(self.table, 1)
        return root

    # --------------------------------------------------------------- правая
    def _build_right(self) -> QWidget:
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(8, 12, 14, 12)
        v.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_text(), "📝 Текст")
        self.tabs.addTab(self._tab_style(), "🎨 Стиль")
        self.tabs.addTab(self._tab_motion(), "⚡ Анимация")
        v.addWidget(self.tabs, 1)

        self.advanced = Collapsible("▸ Advanced / Для разработчиков")
        self._build_advanced(self.advanced)
        v.addWidget(self.advanced)
        return root

    # ---- вкладка 1: текст и распознавание
    def _tab_text(self) -> QWidget:
        page = _scroll()
        v = page.inner_layout

        g = QGroupBox("Распознавание речи")
        f = QFormLayout(g)
        self.cmb_lang = QComboBox()
        _style_combo(self.cmb_lang)
        for code, title in [("ru", "Русский"), ("uk", "Українська"), ("en", "English"),
                            ("pl", "Polski"), ("de", "Deutsch"), ("es", "Español"),
                            ("auto", "Определить автоматически")]:
            self.cmb_lang.addItem(title, code)
        self.cmb_lang.setCurrentIndex(0)                 # язык фиксируем по умолчанию

        self.cmb_content = QComboBox()
        _style_combo(self.cmb_content)
        self.cmb_content.addItem("🎵 Песня / рэп (вокал под музыку)", True)
        self.cmb_content.addItem("🎙 Речь / подкаст", False)
        self.cmb_content.currentIndexChanged.connect(self._on_content_type)

        self.chk_vocals = QCheckBox("Изолировать вокал (Demucs) — убирает галлюцинации")
        self.chk_vocals.setChecked(True)
        self.chk_vocals.setToolTip(
            "Гитара, бас и барабаны уходят до распознавания: Whisper слышит только голос.")

        self.cmb_quality = QComboBox()
        _style_combo(self.cmb_quality)
        self.cmb_quality.addItem("Максимум качества (large-v3)", "large-v3")
        self.cmb_quality.addItem("Баланс (medium)", "medium")
        self.cmb_quality.addItem("Быстро (small)", "small")

        f.addRow("Язык", self.cmb_lang)
        f.addRow("Тип контента", self.cmb_content)
        f.addRow("Качество", self.cmb_quality)
        f.addRow(self.chk_vocals)
        v.addWidget(g)

        self.btn_recognize = QPushButton("🎤 Распознать речь")
        self.btn_recognize.setObjectName("Primary")
        self.btn_recognize.clicked.connect(self.run_recognition)
        v.addWidget(self.btn_recognize)

        g2 = QGroupBox("Свой текст песни (идеальные тайминги)")
        v2 = QVBoxLayout(g2)
        hint = QLabel("Вставьте точный текст — программа не будет угадывать слова, "
                      "а только расставит тайминги по вокалу (forced alignment).")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        self.txt_lyrics = QPlainTextEdit()
        self.txt_lyrics.setPlaceholderText("Куплет 1\nСтрока песни…\nЕщё строка…")
        self.txt_lyrics.setMinimumHeight(150)
        self.btn_align = QPushButton("📋 Выровнять мой текст по вокалу")
        self.btn_align.clicked.connect(self.run_alignment)
        btn_clear = QPushButton("Очистить")
        btn_clear.clicked.connect(self.txt_lyrics.clear)
        row = QHBoxLayout()
        row.addWidget(self.btn_align, 1)
        row.addWidget(btn_clear)
        v2.addWidget(hint)
        v2.addWidget(self.txt_lyrics)
        v2.addLayout(row)
        v.addWidget(g2)

        self.lbl_env = QLabel("")
        self.lbl_env.setObjectName("Muted")
        self.lbl_env.setWordWrap(True)
        v.addWidget(self.lbl_env)
        v.addStretch(1)
        return page

    # ---- вкладка 2: стиль
    def _tab_style(self) -> QWidget:
        page = _scroll()
        v = page.inner_layout

        g = QGroupBox("Готовые стили")
        gv = QVBoxLayout(g)
        self.style_grid = CardGrid(columns=1)
        for p in STYLE_PRESETS:
            self.style_grid.add_card(PresetCard(p.key, p.title, p.subtitle, p.sample,
                                                p.preview_fill, p.preview_stroke, p.preview_glow))
        self.style_grid.selected.connect(self._apply_style_preset)
        gv.addWidget(self.style_grid)
        v.addWidget(g)

        g2 = QGroupBox("Шрифт")
        f = QFormLayout(g2)
        self.font_box = QFontComboBox()
        self.font_box.setObjectName("FontPicker")
        self.font_box.setCursor(Qt.PointingHandCursor)
        self.font_box.setToolTip("Гарнитура субтитров — нажмите, чтобы развернуть список")
        self.font_box.setMinimumHeight(30)
        self.font_box.setMaxVisibleItems(18)
        self.font_box.setEditable(False)       # это меню, а не поле ввода
        _style_combo(self.font_box)
        self.font_box.currentFontChanged.connect(
            lambda fnt: self._update_style(font_family=fnt.family()))
        self.seg_size = SegmentedControl([(k, k) for k in SIZE_PRESETS], "M")
        self.seg_size.changed.connect(lambda k: self._update_style(font_size=SIZE_PRESETS[k]))
        self.chk_upper = QCheckBox("ЗАГЛАВНЫЕ БУКВЫ")
        self.chk_upper.toggled.connect(lambda b: self._update_style(uppercase=b))
        f.addRow("Гарнитура", self.font_box)
        f.addRow("Размер", self.seg_size)
        f.addRow(self.chk_upper)
        v.addWidget(g2)

        g3 = QGroupBox("Цвета и читаемость")
        f3 = QFormLayout(g3)
        # «Основной цвет» — цвет НЕактивных слов.
        self.btn_fill = self._color_button("fill_color")
        # «Акцент» — цвет ТЕКСТА активного слова (и ключевых слов). Источник истины.
        self.btn_key = self._color_button("accent")
        self.seg_stroke = SegmentedControl(
            [("none", "Нет"), ("thin", "Тонкая"), ("mid", "Средняя"), ("bold", "Жирная")], "mid")
        self.seg_stroke.changed.connect(self._on_stroke_preset)
        # Мастер-тумблер подложки: тёмный полупрозрачный прямоугольник
        # под ВСЕЙ строкой субтитра (rgba(0,0,0,0.6), радиус 8 px).
        self.chk_backing = QCheckBox("Полупрозрачная подложка")
        self.chk_backing.setToolTip("Тёмная подложка под всей строкой субтитра")
        self.chk_backing.toggled.connect(self._on_backing_toggled)
        # Плашка под активным словом — по умолчанию ВЫКЛЮЧЕНА (никаких красных боксов)
        self.chk_word_box = QCheckBox("Плашка под активным словом")
        self.chk_word_box.setToolTip(
            "Включает фоновый прямоугольный бокс под текущим произносимым словом "
            "(стиль Submagic)")
        self.chk_word_box.toggled.connect(self._on_word_box_toggled)
        self.btn_word_box = self._color_button("active_card_color")
        self.btn_word_box.setToolTip(
            "Цвет фонового бокса под активным словом. Доступен, только когда "
            "включён тумблер «Плашка под активным словом»")
        self.btn_word_box.setEnabled(False)
        self.sld_inactive = QSlider(Qt.Horizontal)
        self.sld_inactive.setRange(20, 100)
        self.sld_inactive.setValue(100)
        self.sld_inactive.setToolTip("Прозрачность неактивных слов во фразе "
                                     "(эффект караоке)")
        self.lbl_inactive = QLabel("100%")
        self.lbl_inactive.setObjectName("Muted")
        self.lbl_inactive.setMinimumWidth(42)
        self.lbl_inactive.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_inactive.setToolTip(self.sld_inactive.toolTip())
        self.sld_inactive.valueChanged.connect(self._on_inactive_opacity)
        row_inactive = QWidget()
        row_inactive.setToolTip(self.sld_inactive.toolTip())
        hl = QHBoxLayout(row_inactive)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.sld_inactive, 1)
        hl.addWidget(self.lbl_inactive)
        self.chk_contrast = QCheckBox("Авто-контраст (читаемо на любом фоне)")
        self.chk_contrast.setChecked(True)
        self.chk_contrast.toggled.connect(lambda b: self._update_style(smart_contrast=b))
        f3.addRow("Основной цвет", self.btn_fill)
        f3.addRow("Акцент", self.btn_key)
        f3.addRow("Обводка", self.seg_stroke)
        f3.addRow(self.chk_backing)
        f3.addRow(self.chk_word_box)
        f3.addRow("Цвет плашки слова", self.btn_word_box)
        f3.addRow("Неактивные слова", row_inactive)
        f3.addRow(self.chk_contrast)
        v.addWidget(g3)
        v.addStretch(1)
        return page

    # ---- вкладка 3: анимация и позиция
    def _tab_motion(self) -> QWidget:
        page = _scroll()
        v = page.inner_layout

        g = QGroupBox("Анимация текста")
        gv = QVBoxLayout(g)
        self.anim_grid = CardGrid(columns=2)   # 11 режимов — в две колонки
        for p in ANIMATION_PRESETS:
            self.anim_grid.add_card(PresetCard(p.key, p.title, p.description, "Abc",
                                               "#FFFFFF", "#000000", ACCENT))
        self.anim_grid.selected.connect(self._apply_animation_preset)
        gv.addWidget(self.anim_grid)
        v.addWidget(g)

        g2 = QGroupBox("Позиция на экране")
        f = QFormLayout(g2)
        self.seg_pos = SegmentedControl(
            [(k, POSITION_TITLES[k]) for k in POSITION_PRESETS], "bottom")
        self.seg_pos.changed.connect(self._on_position_preset)
        self.sld_offset = QSlider(Qt.Horizontal)
        self.sld_offset.setRange(-15, 15)                # ±15% высоты кадра
        self.sld_offset.setValue(0)
        self.sld_offset.valueChanged.connect(self._on_offset)
        self.lbl_offset = QLabel("смещение 0%")
        self.lbl_offset.setObjectName("Muted")
        f.addRow(self.seg_pos)
        f.addRow("Точная подстройка", self.sld_offset)
        f.addRow(self.lbl_offset)
        v.addWidget(g2)

        g3 = QGroupBox("Длина строки")
        f3 = QFormLayout(g3)
        self.seg_words = SegmentedControl(
            [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5")], "3")
        self.seg_words.changed.connect(
            lambda k: self._update_style(words_per_block=int(k), resegment=True))
        hint = QLabel("1–2 слова — драйв для рэпа и клипов, 3–5 — удобно читать в подкасте.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        f3.addRow("Слов в кадре", self.seg_words)
        f3.addRow(hint)
        v.addWidget(g3)
        v.addStretch(1)
        return page

    # ---- Advanced
    def _build_advanced(self, box: Collapsible) -> None:
        g = QGroupBox("Физика пружин (для разработчиков)")
        f = QFormLayout(g)
        self.spin_k = QDoubleSpinBox()
        self.spin_k.setRange(1, 2000)
        self.spin_k.setSingleStep(10)
        self.spin_k.valueChanged.connect(lambda v: self._update_spring(k=v))
        self.spin_m = QDoubleSpinBox()
        self.spin_m.setRange(0.05, 20)
        self.spin_m.setSingleStep(0.1)
        self.spin_m.valueChanged.connect(lambda v: self._update_spring(m=v))
        self.spin_c = QDoubleSpinBox()
        self.spin_c.setRange(0, 400)
        self.spin_c.valueChanged.connect(lambda v: self._update_spring(c=v))
        self.spin_pulse = QDoubleSpinBox()
        self.spin_pulse.setRange(0.0, 0.5)
        self.spin_pulse.setSingleStep(0.01)
        self.spin_pulse.valueChanged.connect(lambda v: self._update_style(kick_pulse_amp=v))
        self.spin_glow = QSpinBox()
        self.spin_glow.setRange(0, 60)
        self.spin_glow.valueChanged.connect(lambda v: self._update_style(glow_radius=v))
        self.lbl_zeta = QLabel("ζ = —")
        self.lbl_zeta.setObjectName("Muted")
        self.spin_k.setToolTip(
            "Жёсткость пружины k. Больше — текст «выстреливает» быстрее и резче, "
            "меньше — появляется вяло и тягуче. Типично 150–350.")
        self.spin_m.setToolTip(
            "Масса m. Больше — слово тяжелее, разгон и остановка медленнее "
            "(инертная, «солидная» подача). Типично 1.0.")
        self.spin_c.setToolTip(
            "Затухание c. Меньше — больше отскоков и покачивания, больше — слово "
            "встаёт на место сразу. При ζ < 1 есть отскок, при ζ ≥ 1 его нет.")
        self.spin_pulse.setToolTip(
            "Пульс от бочки (kick drum). Насколько слово подпрыгивает в масштабе "
            "на басовом ударе: 0 — реакции нет, 0.15 — заметный бит-синхрон.")
        self.spin_glow.setToolTip("Радиус неонового свечения вокруг текста, px.")
        f.addRow("Жёсткость k", self.spin_k)
        f.addRow("Масса m", self.spin_m)
        f.addRow("Затухание c", self.spin_c)
        f.addRow("Пульс от бочки", self.spin_pulse)
        f.addRow("Радиус свечения", self.spin_glow)
        f.addRow(self.lbl_zeta)
        box.add_widget(g)

        g2 = QGroupBox("Диагностика аудио и ASR")
        v2 = QVBoxLayout(g2)
        self.lbl_debug = QLabel("Данных пока нет — запустите распознавание.")
        self.lbl_debug.setWordWrap(True)
        self.lbl_debug.setObjectName("Muted")
        self.lbl_debug.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cmb_genre = QComboBox()
        _style_combo(self.cmb_genre)
        self.cmb_genre.addItem("Авто", None)
        for gkey in AutoStyleEngine.genres():
            self.cmb_genre.addItem(gkey, gkey)
        self.cmb_genre.currentIndexChanged.connect(self._on_genre_override)
        v2.addWidget(self.lbl_debug)
        v2.addWidget(QLabel("Переопределить жанр:"))
        v2.addWidget(self.cmb_genre)
        box.add_widget(g2)

    def _color_button(self, field: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedHeight(28)
        btn.setCursor(Qt.PointingHandCursor)

        def pick() -> None:
            style = self.project.global_style
            current = style.accent_color if field == "accent" else getattr(style, field)
            c = QColorDialog.getColor(QColor(current), self, "Выберите цвет")
            if c.isValid():
                self.set_color(field, c.name().upper())
        btn.clicked.connect(pick)
        return btn

    def _on_word_box_toggled(self, on: bool) -> None:
        """Чекбокс плашки напрямую управляет доступностью выбора её цвета."""
        self.btn_word_box.setEnabled(on)
        self._update_style(active_card_enabled=on)

    def _on_inactive_opacity(self, value: int) -> None:
        self.lbl_inactive.setText(f"{value}%")
        self._update_style(inactive_opacity=value / 100.0)

    def update_preview_frame(self) -> None:
        """Немедленная перерисовка кадра предпросмотра (без регенерации субтитров)."""
        self.scene.layout_engine.invalidate()
        self.player.refresh_overlay()
        if hasattr(self, "canvas") and self.canvas is not None:
            self.canvas.set_frame(self.player.current_video_image())
            self.canvas.update()
        self.player.update()

    def set_color(self, field: str, value: str) -> None:
        """Единая точка применения цветов из панели «Цвета и читаемость»."""
        if field == "accent":
            # Акцент задаёт ЦВЕТ ТЕКСТА активного слова (а не фон под ним)
            self.project.global_style = self.project.global_style.with_accent(value)
            self.scene.set_style(self.project.global_style)
            self._sync_controls()
            self.player.refresh_overlay()
            self.statusBar().showMessage(f"Акцент активного слова: {value}")
            return
        self._update_style(**{field: value})

    def _on_backing_toggled(self, on: bool) -> None:
        """Подложка под всей строкой: rgba(0,0,0,0.6), скругление 8 px."""
        self._update_style(backing_enabled=on, backing_color="#000000",
                           backing_opacity=0.6, card_enabled=False)

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("Файл")
        for text, slot, key in (("Открыть медиа…", self.open_media, QKeySequence.Open),
                                ("Открыть проект…", self.open_project, "Ctrl+Shift+O"),
                                ("Сохранить проект…", self.save_project, QKeySequence.Save),
                                ("Экспорт видео…", self.export_video, "Ctrl+E"),
                                ("Экспорт SRT…", self.export_srt, "Ctrl+Shift+E")):
            a = QAction(text, self)
            a.setShortcut(QKeySequence(key) if isinstance(key, str) else key)
            a.triggered.connect(slot)
            m.addAction(a)
        m.addSeparator()
        quit_a = QAction("Выход", self)
        quit_a.triggered.connect(self.close)
        m.addAction(quit_a)

    # ============================================================== СОСТОЯНИЕ
    def _wire_runner(self) -> None:
        self.runner.progress.connect(self._on_progress)
        self.runner.failed.connect(self._on_failed)
        self.runner.state_changed.connect(self._on_busy)

    def _check_environment(self) -> None:
        """Показать пользователю, какие движки реально доступны."""
        caps = AudioProcessor().capabilities()
        def mark(ok: bool) -> str:
            return "✓" if ok else "✗"
        self.lbl_env.setText(
            f"Движки: {mark(caps['stable_ts'])} stable-ts · "
            f"{mark(caps['faster_whisper'])} faster-whisper · "
            f"{mark(caps['demucs'])} Demucs · {mark(caps['torchaudio'])} CTC-выравнивание · "
            f"{mark(caps['cuda'])} GPU · {mark(caps['ffmpeg'])} FFmpeg\n"
            f"Видео: {self.player.backend_name()} · {multimedia_status()}")
        missing = []
        if not caps["stable_ts"]:
            missing.append("pip install stable-ts")
        if not caps["demucs"]:
            missing.append("pip install demucs")
        if missing:
            self.statusBar().showMessage("Для максимального качества: " + "; ".join(missing))

    def _sync_controls(self) -> None:
        self._updating = True
        s = self.project.global_style
        self.seg_aspect.set_value(s.safe_zone)
        self.seg_fit.set_value(s.fit_mode)
        self.font_box.setCurrentText(s.font_family)
        self.seg_size.set_value(size_key_for(s.font_size))
        self.chk_upper.setChecked(s.uppercase)
        self.chk_backing.setChecked(s.backing_enabled or s.card_enabled)
        self.chk_word_box.setChecked(s.active_card_enabled)
        self.btn_word_box.setEnabled(s.active_card_enabled)
        self.sld_inactive.setValue(int(round(s.inactive_opacity * 100)))
        self.lbl_inactive.setText(f"{int(round(s.inactive_opacity * 100))}%")
        self.chk_contrast.setChecked(s.smart_contrast)
        self.seg_stroke.set_value(_stroke_key(s.stroke_width))
        self.seg_words.set_value(str(min(5, max(1, s.words_per_block))))
        self.seg_pos.set_value(self._position_key)
        for btn, field in ((self.btn_fill, "fill_color"), (self.btn_key, "accent"),
                           (self.btn_word_box, "active_card_color")):
            col = s.accent_color if field == "accent" else getattr(s, field)
            btn.setText(col)
            swatch = QColor("#" + col[7:9] + col[1:7]) if len(col) == 9 else QColor(col)
            btn.setStyleSheet(
                f"background:{swatch.name()}; border-radius:8px; font-weight:700;"
                f"color:{'#000' if swatch.lightnessF() > 0.5 else '#FFF'};")
        self.spin_k.setValue(s.spring.k)
        self.spin_m.setValue(s.spring.m)
        self.spin_c.setValue(s.spring.c)
        self.spin_pulse.setValue(s.kick_pulse_amp)
        self.spin_glow.setValue(s.glow_radius)
        self.lbl_zeta.setText(f"ζ = {s.spring.zeta:.2f} "
                              f"({'с отскоком' if s.spring.zeta < 1 else 'без отскока'})")
        self._updating = False

    def _update_style(self, resegment: bool = False, **kwargs) -> None:
        if self._updating:
            return
        self.project.global_style = Style.model_validate(
            self.project.global_style.model_copy(update=kwargs).model_dump())
        self.scene.set_style(self.project.global_style)
        if resegment:
            self._resegment()
        self._sync_controls()
        self.player.refresh_overlay()

    def _update_spring(self, **kwargs) -> None:
        if self._updating:
            return
        self._update_style(spring=self.project.global_style.spring.model_copy(update=kwargs))

    def _on_aspect(self, key: str) -> None:
        """Смена формата = реальная смена разрешения композиции."""
        w, h = self.scene.use_aspect(key)
        self.project.width, self.project.height = w, h
        self._sync_controls()
        self.update_preview_frame()
        self.statusBar().showMessage(f"Формат {key} — композиция {w}x{h}")

    def _on_fit_mode(self, key: str) -> None:
        self._update_style(fit_mode=key)
        if hasattr(self, "canvas") and self.canvas is not None:
            self.canvas.set_fit_mode(key)
        self.update_preview_frame()
        self.statusBar().showMessage({
            "fit_blur": "Вписать кадр, поля — размытая копия",
            "fit": "Вписать кадр, поля чёрные",
            "fill": "Заполнить кадр (центральный кроп)"}.get(key, key))

    def _apply_style_preset(self, key: str) -> None:
        preset = STYLE_BY_KEY.get(key)
        if not preset:
            return
        self.project.global_style = preset.apply(self.project.global_style)
        self.scene.set_style(self.project.global_style)
        self._sync_controls()
        self.player.refresh_overlay()
        self.statusBar().showMessage(f"Стиль: {preset.title}")

    def _apply_animation_preset(self, key: str) -> None:
        preset = ANIMATION_BY_KEY.get(key)
        if not preset:
            return
        self.project.global_style = preset.apply(self.project.global_style)
        self.scene.set_style(self.project.global_style)
        self._sync_controls()
        self.player.refresh_overlay()
        self.statusBar().showMessage(f"Анимация: {preset.title}")

    def _on_position_preset(self, key: str) -> None:
        self._position_key = key
        self._apply_position()

    def _on_offset(self, value: int) -> None:
        self._position_offset = value / 100.0
        self.lbl_offset.setText(f"смещение {value:+d}%")
        self._apply_position()

    def _apply_position(self) -> None:
        if self._updating:
            return
        self.project.global_style = apply_position(self.project.global_style,
                                                   self._position_key, self._position_offset)
        self.scene.set_style(self.project.global_style)
        self.player.refresh_overlay()

    def _on_stroke_preset(self, key: str) -> None:
        self._update_style(stroke_width={"none": 0, "thin": 4, "mid": 7, "bold": 11}[key])

    def _on_content_type(self) -> None:
        is_song = bool(self.cmb_content.currentData())
        self.chk_vocals.setChecked(is_song)          # для речи изоляция обычно не нужна

    def _on_genre_override(self) -> None:
        if self._updating or not self.project.audio_profile:
            return
        genre = self.cmb_genre.currentData()
        if not genre:
            return
        self.project.audio_profile.genre = genre
        self.project.global_style = AutoStyleEngine.get_style_for_profile(
            self.project.audio_profile, self.project.global_style.safe_zone)
        self.scene.set_style(self.project.global_style)
        self._resegment()
        self._sync_controls()

    def _on_box_moved(self, x: float, y: float) -> None:
        self._update_style(x_offset_pct=x, y_offset_pct=y)
        self._updating = True
        self._position_key = position_key_for(y)
        self.seg_pos.set_value(self._position_key)
        self._updating = False

    def _toggle_safe(self, on: bool) -> None:
        self.player.set_safe_zone_visible(on)

    def _resegment(self) -> None:
        from app.audio.segment import GENRE_PARAMS, PhraseSegmenter, SegmentParams
        words = self.project.words
        if not words:
            return
        genre = self.project.audio_profile.genre if self.project.audio_profile else "speech_podcast"
        base = GENRE_PARAMS.get(genre, GENRE_PARAMS["speech_podcast"])
        n = self.project.global_style.words_per_block
        params = SegmentParams(max_words=max(2, n + 1), target_words=n,
                               max_chars=self.project.global_style.max_chars_per_block,
                               max_block_seconds=base.max_block_seconds,
                               gap_weight=base.gap_weight)
        self.project.phrases = PhraseSegmenter(params).segment_words(
            words, self.project.audio_profile)
        self.table.load(self.project)

    # ============================================================== МЕДИА
    def open_media(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите видео или аудио", "",
            "Медиа (*.mp4 *.mov *.mkv *.avi *.webm *.mp3 *.wav *.m4a *.flac);;Все файлы (*)")
        if path:
            self.load_media(path)

    def load_media(self, path: str) -> None:
        if not os.path.exists(path):
            QMessageBox.warning(self, "Файл не найден", path)
            return
        self.media_path = path
        self.project.video_path = path
        self.lbl_file.setText(Path(path).name)
        try:
            self.player.load(path)
        except Exception as exc:
            log.warning("плеер: %s", exc)
            self.statusBar().showMessage(f"Плеер не смог открыть файл: {exc}")
        try:
            from app.audio.extract import ffprobe_duration, probe_video
            w, h, fps = probe_video(path)
            self.project.width, self.project.height, self.project.fps = w, h, fps
            self.project.duration = ffprobe_duration(path)
            self.scene.resize(w, h)
            self.statusBar().showMessage(
                f"{Path(path).name} · {w}×{h} · {self.project.duration:.1f} с")
        except Exception as exc:
            self.statusBar().showMessage(f"Метаданные недоступны: {exc}")
        self.player.refresh_overlay()

    def toggle_play(self) -> None:
        self.player.toggle()

    def seek_seconds(self, t: float) -> None:
        """Клик по строке таблицы → мгновенная перемотка плеера на её начало."""
        self.player.seek(t)

    def _on_seek(self, value: int) -> None:
        dur = self.player.duration() or self.project.duration or 1.0
        self.player.seek(dur * value / 1000.0)

    def _on_duration(self, seconds: float) -> None:
        if seconds > 0:
            self.project.duration = self.project.duration or seconds
            self.lbl_time.setText(f"{_fmt(self.player.position())} / {_fmt(seconds)}")

    def _on_position(self, t: float) -> None:
        """Воспроизведение → подсветка текущей строки в таблице (двусторонняя синхронизация)."""
        dur = self.player.duration() or self.project.duration or 1.0
        if not self.seekbar.isSliderDown():
            self.seekbar.blockSignals(True)
            self.seekbar.setValue(int(1000 * min(1.0, t / dur)))
            self.seekbar.blockSignals(False)
        self.lbl_time.setText(f"{_fmt(t)} / {_fmt(dur)}")
        self.table.highlight_at_time(t)
        self._maybe_update_contrast()

    # ============================================================== ПАЙПЛАЙН
    def _options(self, lyrics: str = "") -> ProcessOptions:
        return ProcessOptions(
            language=self.cmb_lang.currentData() or "ru",
            model_size=self.cmb_quality.currentData() or "large-v3",
            isolate_vocals=self.chk_vocals.isChecked(),
            is_song=bool(self.cmb_content.currentData()),
            lyrics=lyrics,
            safe_zone=self.project.global_style.safe_zone,
        )

    def run_recognition(self) -> None:
        self._run_processor(self._options(""))

    def run_alignment(self) -> None:
        lyrics = self.txt_lyrics.toPlainText().strip()
        if not lyrics:
            QMessageBox.information(
                self, "Нет текста",
                "Вставьте текст песни в поле «Свой текст песни», затем нажмите кнопку ещё раз.")
            self.tabs.setCurrentIndex(0)
            return
        self._run_processor(self._options(lyrics))

    def _run_processor(self, options: ProcessOptions) -> None:
        if not self.media_path:
            QMessageBox.warning(self, "Нет файла", "Сначала откройте видео или аудио.")
            return
        if self.runner.busy:
            QMessageBox.information(self, "Идёт обработка", "Дождитесь завершения задачи.")
            return
        processor = AudioProcessor(options)
        self.runner.succeeded.connect(self._on_processed, Qt.UniqueConnection)
        self._pending_processor = processor
        self.runner.start(processor.process, self.media_path)

    @Slot(object)
    def _on_processed(self, project: object) -> None:
        try:
            self.runner.succeeded.disconnect(self._on_processed)
        except (RuntimeError, TypeError):
            pass
        if not isinstance(project, Project):
            return
        # сохраняем выбранный пользователем стиль, меняя только «жанровые» части
        keep = self.project.global_style.model_dump()
        for k in ("safe_zone", "y_offset_pct", "x_offset_pct", "alignment"):
            project.global_style = project.global_style.model_copy(update={k: keep[k]})
        preset_key = self.style_grid.value() or "capcut_yellow"
        project.global_style = STYLE_BY_KEY[preset_key].apply(project.global_style)
        anim_key = self.anim_grid.value()
        if anim_key:
            project.global_style = ANIMATION_BY_KEY[anim_key].apply(project.global_style)

        self.project = project
        self.scene = SubtitleScene(project, project.width, project.height)
        self.player.set_scene(self.scene)
        self.table.load(project)
        self._sync_controls()

        report = getattr(self._pending_processor, "report", None)
        self._last_report = report
        if report:
            p = project.audio_profile
            probs = ", ".join(f"{k} {v:.0%}" for k, v in
                              sorted((p.probabilities if p else {}).items(),
                                     key=lambda kv: -kv[1])[:3])
            self.lbl_debug.setText(
                f"ASR: {report.engine} · язык: {report.language} · вокал: {report.separation}\n"
                f"Слов: {report.n_words} · блоков: {report.n_phrases}\n"
                f"BPM {p.bpm:.0f} · онсетов {len(p.onsets)} · kick {len(p.kick_events)} · "
                f"слов/с {p.words_per_sec:.2f}\nЖанр: {probs}" if p else "")
            for w in report.warnings:
                log.warning("pipeline warning: %s", w)
            if report.warnings:
                self.statusBar().showMessage(report.warnings[0])
            else:
                self.statusBar().showMessage(
                    f"Готово: {report.n_phrases} блоков, {report.n_words} слов")
        self.player.refresh_overlay()

    @Slot(float, str)
    def _on_progress(self, p: float, msg: str) -> None:
        self.progress.setValue(int(p * 100))
        self.progress.setFormat(f"%p%  —  {msg}")
        self.statusBar().showMessage(msg)

    @Slot(str)
    def _on_failed(self, msg: str) -> None:
        self.progress.setValue(0)
        self.progress.setFormat("%p%  —  ошибка")
        box = QMessageBox(QMessageBox.Critical, "Не удалось выполнить",
                          msg.split("\n\n---\n")[0], parent=self)
        box.setDetailedText(msg)
        box.exec()

    @Slot(bool)
    def _on_busy(self, busy: bool) -> None:
        for btn in (self.btn_process, self.btn_recognize, self.btn_align,
                    self.btn_export, self.btn_open):
            btn.setEnabled(not busy)
        if not busy:
            self.progress.setFormat("%p%  —  готово")

    # ============================================================== РЕДАКТУРА
    def _on_text_edited(self, row: int, text: str) -> None:
        if not (0 <= row < len(self.project.phrases)):
            return
        ph = self.project.phrases[row]
        tokens = text.split()
        if not tokens:
            return
        start, end = ph.start, ph.end
        step = (end - start) / len(tokens)
        old = ph.words
        ph.words = [old[i].model_copy(update={"text": t}) if i < len(old)
                    else Word(text=t, start=start + i * step, end=start + (i + 1) * step)
                    for i, t in enumerate(tokens)]
        self.player.refresh_overlay()

    def _on_time_edited(self, row: int, start: float, end: float) -> None:
        if not (0 <= row < len(self.project.phrases)) or end <= start:
            return
        ph = self.project.phrases[row]
        old_s, span = ph.start, max(1e-6, ph.end - ph.start)
        k = (end - start) / span
        for w in ph.words:
            w.start = start + (w.start - old_s) * k
            w.end = start + (w.end - old_s) * k
        self.table._ranges = [(x.start, x.end) for x in self.project.phrases]
        self.table._row_at = {}
        for i, (a, b) in enumerate(self.table._ranges):
            for q in range(int(a * 4), int(b * 4) + 2):
                self.table._row_at.setdefault(q, i)
        self.player.refresh_overlay()

    # ============================================================== ФАЙЛЫ
    def save_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", "project.ctp",
                                              "CapText (*.ctp *.json)")
        if path:
            self.project.save(path)
            self.statusBar().showMessage(f"Сохранено: {path}")

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "",
                                              "CapText (*.ctp *.json)")
        if not path:
            return
        try:
            self.project = Project.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось открыть", str(exc))
            return
        self.scene = SubtitleScene(self.project, self.project.width, self.project.height)
        self.player.set_scene(self.scene)
        self.table.load(self.project)
        if self.project.video_path and os.path.exists(self.project.video_path):
            self.load_media(self.project.video_path)
        self._sync_controls()

    def export_video(self) -> None:
        if not self.project.phrases:
            QMessageBox.warning(self, "Пусто", "Сначала создайте субтитры.")
            return
        from app.audio.extract import has_ffmpeg
        if not has_ffmpeg():
            QMessageBox.warning(self, "Нужен FFmpeg",
                                "Для рендера установите: pip install static-ffmpeg\n\n"
                                "Экспорт SRT доступен всегда (Файл → Экспорт SRT…).")
            return
        path, selected = QFileDialog.getSaveFileName(
            self, "Экспорт", "captext_out.mp4",
            "MP4 H.264 (*.mp4);;MP4 HEVC (*.mp4);;ProRes 4444 alpha (*.mov);;WebM alpha (*.webm)")
        if not path:
            return
        fmt = {"MP4 H.264 (*.mp4)": "mp4_h264", "MP4 HEVC (*.mp4)": "mp4_hevc",
               "ProRes 4444 alpha (*.mov)": "mov_prores4444_alpha",
               "WebM alpha (*.webm)": "webm_alpha"}.get(selected, "mp4_h264")
        exporter = VideoExporter(SubtitleScene(self.project, self.project.width,
                                               self.project.height))

        def on_done(result: object) -> None:
            try:
                self.runner.succeeded.disconnect(on_done)
            except (RuntimeError, TypeError):
                pass
            QMessageBox.information(self, "Экспорт завершён", f"Файл готов:\n{result}")

        self.runner.succeeded.connect(on_done)
        self.runner.start(exporter.export, self.project, path, fmt)

    def export_srt(self) -> None:
        if not self.project.phrases:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт SRT", "captions.srt", "SRT (*.srt)")
        if path:
            VideoExporter.export_srt(self.project, path)
            self.statusBar().showMessage(f"SRT сохранён: {path}")

    def _maybe_update_contrast(self) -> None:
        """Раз в ~200 мс отдаём сцене текущий кадр — Smart Contrast подбирает цвет."""
        import time
        now = time.monotonic()
        if now - getattr(self, "_last_contrast", 0.0) < 0.2:
            return
        self._last_contrast = now
        if not self.project.global_style.smart_contrast:
            return
        img = self.player.current_video_image()
        if img is None or img.isNull():
            return
        small = img.scaled(96, 96, Qt.IgnoreAspectRatio, Qt.FastTransformation)
        small = small.convertToFormat(QImage.Format_RGB888)
        buf = bytes(small.constBits())[: small.bytesPerLine() * small.height()]
        arr = np.frombuffer(buf, np.uint8).reshape(small.height(), small.bytesPerLine())
        arr = arr[:, : small.width() * 3].reshape(small.height(), small.width(), 3)
        self.scene.update_contrast(arr)

    # ============================================================== ЗАКРЫТИЕ
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.runner.busy:
            if QMessageBox.question(self, "Идёт обработка",
                                    "Прервать текущую задачу и выйти?") != QMessageBox.Yes:
                event.ignore()
                return
            self.runner.shutdown()          # wait() только здесь, в GUI-потоке
        self.player.shutdown()
        event.accept()


# ------------------------------------------------------------------ helpers
class _ScrollPage(QScrollArea):
    """Прокручиваемая страница вкладки с готовым вертикальным layout."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.inner_layout = QVBoxLayout(inner)
        self.inner_layout.setContentsMargins(4, 8, 8, 8)
        self.inner_layout.setSpacing(12)
        self.setWidget(inner)


def _scroll() -> _ScrollPage:
    return _ScrollPage()


def _stroke_key(width: int) -> str:
    if width <= 0:
        return "none"
    if width <= 5:
        return "thin"
    if width <= 9:
        return "mid"
    return "bold"


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

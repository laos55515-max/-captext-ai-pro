"""CapText AI Pro — main window (PySide6 Widgets, Cyberpunk / Dark Graphite)."""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import (QObject, QPointF, QRectF, QSize, Qt, QThread, QTimer, QUrl,
                            Signal, Slot)
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app.core.models import Phrase, Project, Style, Word
from app.core.pipeline import AutoPipeline, PipelineOptions
from app.render.exporter import VideoExporter
from app.render.presets import AutoStyleEngine
from app.render.scene import SubtitleScene
from app.ui.theme import ACCENT, ACCENT_2, MUTED, QSS, WARN

log = logging.getLogger("captext.ui")


# --------------------------------------------------------------------- workers
class Worker(QObject):
    """Runs a callable in a worker thread. NEVER lets an exception escape the thread.

    Any failure (including ``BaseException`` such as MemoryError or a C-level
    RuntimeError from a model) is converted into the ``failed`` signal, so the
    process can never abort because of an unhandled background exception.
    """

    progress = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    done = Signal()          # always emitted exactly once, success or failure

    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, progress=self._emit_progress, **self._kwargs)
        except BaseException as exc:                   # noqa: BLE001 — must not propagate
            log.exception("worker failed")
            self.failed.emit(_friendly_error(exc))
        else:
            try:
                self.succeeded.emit(result)
            except BaseException as exc:               # noqa: BLE001
                log.exception("result handling failed")
                self.failed.emit(_friendly_error(exc))
        finally:
            self.done.emit()

    def _emit_progress(self, p: float, msg: str) -> None:
        try:
            self.progress.emit(float(p), str(msg))
        except Exception:                              # progress must never kill a job
            pass


def _friendly_error(exc: BaseException) -> str:
    """Human-readable message + actionable hint + short traceback."""
    from app.audio.extract import AudioDecodeError, backend_report

    head = f"{type(exc).__name__}: {exc}"
    hint = ""
    text = str(exc).lower()
    if isinstance(exc, AudioDecodeError) or "ffmpeg" in text:
        hint = ("\n\nПодсказка: установите декодер —\n"
                "    pip install static-ffmpeg av\n"
                f"Текущие бэкенды: {backend_report()}")
    elif "faster_whisper" in text or "faster-whisper" in text:
        hint = "\n\nПодсказка: pip install faster-whisper"
    elif isinstance(exc, (ImportError, ModuleNotFoundError)):
        hint = "\n\nПодсказка: pip install -r requirements.txt"
    elif "out of memory" in text or isinstance(exc, MemoryError):
        hint = "\n\nПодсказка: выберите модель ASR поменьше (medium / small)."
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1200:]
    return f"{head}{hint}\n\n---\n{tb}"


# ------------------------------------------------------------- subtitle canvas
class SubtitleOverlay(QWidget):
    """Transparent layer painted on top of the video with SubtitleScene."""

    box_moved = Signal(float, float)   # x_pct, y_pct

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.scene: SubtitleScene | None = None
        self.t = 0.0
        self.show_safe_zone = True
        self._drag = False
        self._drag_dy = 0.0

    def set_scene(self, scene: SubtitleScene) -> None:
        self.scene = scene
        self.update()

    def set_time(self, t: float) -> None:
        if abs(t - self.t) > 1e-4:
            self.t = t
            self.update()

    def _fit(self) -> tuple[float, float, float]:
        """Return (scale, offset_x, offset_y) fitting scene canvas into the widget."""
        if not self.scene:
            return 1.0, 0.0, 0.0
        sw, sh = self.scene.width, self.scene.height
        s = min(self.width() / sw, self.height() / sh) if sw and sh else 1.0
        return s, (self.width() - sw * s) / 2.0, (self.height() - sh * s) / 2.0

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self.scene:
            return
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        s, ox, oy = self._fit()
        p.translate(ox, oy)
        if self.show_safe_zone:
            r = self.scene.safe_rect()
            p.save()
            p.scale(s, s)
            pen = QPen(QColor(ACCENT))
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(2.0 / max(s, 1e-3))
            p.setPen(pen)
            p.drawRect(r)
            p.setPen(QPen(QColor(255, 255, 255, 40), 1.0 / max(s, 1e-3)))
            p.drawRect(QRectF(0, 0, self.scene.width - 1, self.scene.height - 1))
            p.restore()
        self.scene.paint(p, self.t, scale=s)
        p.end()

    # drag & drop of the subtitle box
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self.scene and e.button() == Qt.LeftButton:
            self._drag = True
            s, ox, oy = self._fit()
            y_pct = (e.position().y() - oy) / max(1.0, self.scene.height * s)
            self._drag_dy = self.scene.project.global_style.y_offset_pct - y_pct

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag and self.scene:
            s, ox, oy = self._fit()
            x_pct = (e.position().x() - ox) / max(1.0, self.scene.width * s)
            y_pct = (e.position().y() - oy) / max(1.0, self.scene.height * s) + self._drag_dy
            self.box_moved.emit(min(max(x_pct, 0.05), 0.95), min(max(y_pct, 0.03), 0.97))

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag = False


# -------------------------------------------------------------------- timeline
class TimelineTable(QTableWidget):
    phrase_edited = Signal(int, str)
    time_edited = Signal(int, float, float)
    seek_requested = Signal(float)

    COLS = ("#", "Начало", "Конец", "Текст блока")

    def __init__(self) -> None:
        super().__init__(0, 4)
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.setColumnWidth(0, 44)
        self.setColumnWidth(1, 88)
        self.setColumnWidth(2, 88)
        self.horizontalHeader().setStretchLastSection(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self._loading = False
        self.itemChanged.connect(self._on_item_changed)
        self.itemSelectionChanged.connect(self._on_select)

    def load(self, project: Project) -> None:
        self._loading = True
        self.setRowCount(len(project.phrases))
        for i, ph in enumerate(project.phrases):
            for col, val in ((0, str(i + 1)), (1, f"{ph.start:.2f}"), (2, f"{ph.end:.2f}"),
                             (3, ph.text)):
                item = QTableWidgetItem(val)
                if col == 0:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.setItem(i, col, item)
        self._loading = False

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
            self.phrase_edited.emit(row, item.text())
        elif col in (1, 2):
            try:
                start = float(self.item(row, 1).text())
                end = float(self.item(row, 2).text())
            except (ValueError, AttributeError):
                return
            self.time_edited.emit(row, start, end)


# ----------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CapText AI Pro — кинетические субтитры")
        self.resize(1560, 950)
        self.project = Project(global_style=AutoStyleEngine.get_style_for_genre("speech_podcast"))
        self.scene = SubtitleScene(self.project)
        self.media_path: str = ""
        self._thread: QThread | None = None
        self._worker: Worker | None = None
        self._result_handler = None
        self._updating_ui = False

        self._build_ui()
        self._build_menu()
        self.setStyleSheet(QSS)
        self._sync_inspector()
        self._report_backends()

    def _report_backends(self) -> None:
        """Show which decoding backend is active instead of crashing later."""
        from app.audio.extract import backend_report, ensure_ffmpeg, has_ffmpeg
        try:
            ensure_ffmpeg(verbose=True)
        except Exception as exc:                      # resolution must never raise
            log.warning("ffmpeg resolution issue: %s", exc)
        if has_ffmpeg():
            self.statusBar().showMessage(
                f"Готов к работе. Бэкенды: {backend_report()}")
            return
        try:
            import av  # noqa: F401
            self.statusBar().showMessage(
                "FFmpeg не найден — работаю через PyAV. Экспорт видео недоступен "
                "(pip install static-ffmpeg)")
        except Exception:
            self.statusBar().showMessage("Нет декодера: pip install static-ffmpeg av")
            QTimer.singleShot(400, lambda: QMessageBox.warning(
                self, "Нет декодера аудио",
                "Не найден ни FFmpeg, ни PyAV.\n\n"
                "Установите любой из них:\n"
                "    pip install static-ffmpeg\n"
                "    pip install av\n\n"
                "Приложение продолжит работать, но анализ медиа будет недоступен."))

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("Загрузите медиафайл и нажмите «ИИ-Автоматика»")

    def _build_left(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 12, 6, 12)

        header = QHBoxLayout()
        title = QLabel("CapText AI Pro")
        title.setObjectName("Title")
        self.genre_badge = QLabel("жанр: —")
        self.genre_badge.setObjectName("Badge")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.genre_badge)
        v.addLayout(header)

        # ---- player stack
        stack = QFrame()
        stack.setObjectName("Panel")
        stack.setMinimumHeight(420)
        sl = QVBoxLayout(stack)
        sl.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        sl.addWidget(self.video)
        self.overlay = SubtitleOverlay(stack)
        self.overlay.set_scene(self.scene)
        self.overlay.box_moved.connect(self._on_box_moved)
        stack.resizeEvent = self._sync_overlay_geometry(stack)  # type: ignore[assignment]
        v.addWidget(stack, 1)

        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)

        # ---- transport
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

        # ---- actions
        row = QHBoxLayout()
        self.btn_open = QPushButton("📂 Открыть медиа")
        self.btn_open.clicked.connect(self.open_media)
        self.btn_auto = QPushButton("🚀 ИИ-Автоматика (Анализ + Субтитры)")
        self.btn_auto.setObjectName("Primary")
        self.btn_auto.clicked.connect(self.run_auto)
        self.btn_export = QPushButton("🎬 Экспорт")
        self.btn_export.clicked.connect(self.export_video)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_auto, 1)
        row.addWidget(self.btn_export)
        v.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setFormat("%p%  —  ожидание")
        v.addWidget(self.progress)

        v.addWidget(QLabel("Таймлайн-редактор (текст и тайминги редактируются прямо в таблице)"))
        self.timeline = TimelineTable()
        self.timeline.setMinimumHeight(180)
        self.timeline.phrase_edited.connect(self._on_phrase_text)
        self.timeline.time_edited.connect(self._on_phrase_time)
        self.timeline.seek_requested.connect(self.seek_seconds)
        v.addWidget(self.timeline, 1)
        return box

    def _sync_overlay_geometry(self, stack: QFrame):
        def handler(event) -> None:
            self.overlay.setGeometry(0, 0, stack.width(), stack.height())
            self.overlay.raise_()
            QFrame.resizeEvent(stack, event)
        return handler

    def _build_right(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(6, 12, 12, 12)

        # --- AI group
        g_ai = QGroupBox("ИИ-Анализ")
        f = QFormLayout(g_ai)
        self.cmb_lang = QComboBox()
        self.cmb_lang.addItems(["auto", "uk", "ru", "en", "pl", "de", "es", "fr"])
        self.cmb_model = QComboBox()
        self.cmb_model.addItems(["large-v3-turbo", "medium", "small", "base"])
        self.cmb_genre = QComboBox()
        self.cmb_genre.addItems(["Авто"] + AutoStyleEngine.genres())
        self.cmb_genre.currentTextChanged.connect(self._on_genre_override)
        self.lbl_profile = QLabel("BPM —, онсетов —, длительность —")
        self.lbl_profile.setObjectName("Muted")
        self.lbl_profile.setWordWrap(True)
        f.addRow("Язык", self.cmb_lang)
        f.addRow("Модель ASR", self.cmb_model)
        f.addRow("Жанр", self.cmb_genre)
        f.addRow(self.lbl_profile)
        v.addWidget(g_ai)

        # --- layout group
        g_layout = QGroupBox("Композиция")
        fl = QFormLayout(g_layout)
        self.cmb_zone = QComboBox()
        self.cmb_zone.addItems(["9:16", "16:9", "1:1", "4:3"])
        self.cmb_zone.currentTextChanged.connect(lambda v_: self._set_style(safe_zone=v_))
        self.cmb_align = QComboBox()
        self.cmb_align.addItems(["center", "left", "right"])
        self.cmb_align.currentTextChanged.connect(lambda v_: self._set_style(alignment=v_))
        self.spin_y = QDoubleSpinBox()
        self.spin_y.setRange(0.0, 1.0)
        self.spin_y.setSingleStep(0.01)
        self.spin_y.valueChanged.connect(lambda v_: self._set_style(y_offset_pct=v_))
        self.spin_words = QSpinBox()
        self.spin_words.setRange(1, 12)
        self.spin_words.valueChanged.connect(lambda v_: self._set_style(words_per_block=v_,
                                                                       resegment=True))
        self.chk_safe = QCheckBox("Показывать сетку Safe Zone")
        self.chk_safe.setChecked(True)
        self.chk_safe.toggled.connect(self._toggle_safe)
        fl.addRow("Safe zone", self.cmb_zone)
        fl.addRow("Выравнивание", self.cmb_align)
        fl.addRow("Высота (0–1)", self.spin_y)
        fl.addRow("Слов в блоке", self.spin_words)
        fl.addRow(self.chk_safe)
        v.addWidget(g_layout)

        # --- typography group
        g_font = QGroupBox("Шрифт и цвета")
        ff = QFormLayout(g_font)
        self.edit_font = QLineEdit()
        self.edit_font.editingFinished.connect(
            lambda: self._set_style(font_family=self.edit_font.text()))
        self.spin_size = QSpinBox()
        self.spin_size.setRange(10, 400)
        self.spin_size.valueChanged.connect(lambda v_: self._set_style(font_size=v_))
        self.spin_weight = QSpinBox()
        self.spin_weight.setRange(100, 900)
        self.spin_weight.setSingleStep(100)
        self.spin_weight.valueChanged.connect(lambda v_: self._set_style(font_weight=v_))
        self.chk_upper = QCheckBox("ЗАГЛАВНЫЕ")
        self.chk_upper.toggled.connect(lambda v_: self._set_style(uppercase=v_))
        self.btn_fill = self._color_button("fill_color")
        self.btn_stroke = self._color_button("stroke_color")
        self.btn_glow = self._color_button("glow_color")
        self.btn_key = self._color_button("keyword_color")
        self.spin_stroke = QSpinBox()
        self.spin_stroke.setRange(0, 30)
        self.spin_stroke.valueChanged.connect(lambda v_: self._set_style(stroke_width=v_))
        self.spin_glow = QSpinBox()
        self.spin_glow.setRange(0, 60)
        self.spin_glow.valueChanged.connect(lambda v_: self._set_style(glow_radius=v_))
        self.chk_contrast = QCheckBox("Smart Contrast AI (WCAG 4.5:1)")
        self.chk_contrast.toggled.connect(lambda v_: self._set_style(smart_contrast=v_))
        self.chk_karaoke = QCheckBox("Караоке-подсветка")
        self.chk_karaoke.toggled.connect(lambda v_: self._set_style(karaoke=v_))
        ff.addRow("Шрифт", self.edit_font)
        ff.addRow("Размер", self.spin_size)
        ff.addRow("Насыщенность", self.spin_weight)
        ff.addRow(self.chk_upper)
        ff.addRow("Заливка", self.btn_fill)
        ff.addRow("Ключевые слова", self.btn_key)
        ff.addRow("Обводка", self.btn_stroke)
        ff.addRow("Толщина обводки", self.spin_stroke)
        ff.addRow("Свечение", self.btn_glow)
        ff.addRow("Радиус свечения", self.spin_glow)
        ff.addRow(self.chk_contrast)
        ff.addRow(self.chk_karaoke)
        v.addWidget(g_font)

        # --- physics group
        g_phys = QGroupBox("Физика пружин")
        fp = QFormLayout(g_phys)
        self.spin_k = QDoubleSpinBox()
        self.spin_k.setRange(1.0, 2000.0)
        self.spin_k.setSingleStep(10)
        self.spin_k.valueChanged.connect(lambda v_: self._set_spring(k=v_))
        self.spin_m = QDoubleSpinBox()
        self.spin_m.setRange(0.05, 20.0)
        self.spin_m.setSingleStep(0.1)
        self.spin_m.valueChanged.connect(lambda v_: self._set_spring(m=v_))
        self.spin_c = QDoubleSpinBox()
        self.spin_c.setRange(0.0, 400.0)
        self.spin_c.setSingleStep(1.0)
        self.spin_c.valueChanged.connect(lambda v_: self._set_spring(c=v_))
        self.lbl_zeta = QLabel("ζ = —")
        self.lbl_zeta.setObjectName("Muted")
        self.spin_pulse = QDoubleSpinBox()
        self.spin_pulse.setRange(0.0, 0.5)
        self.spin_pulse.setSingleStep(0.01)
        self.spin_pulse.valueChanged.connect(lambda v_: self._set_style(kick_pulse_amp=v_))
        fp.addRow("Жёсткость k", self.spin_k)
        fp.addRow("Масса m", self.spin_m)
        fp.addRow("Затухание c", self.spin_c)
        fp.addRow(self.lbl_zeta)
        fp.addRow("Пульс от бочки", self.spin_pulse)
        v.addWidget(g_phys)

        v.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _color_button(self, field: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedHeight(26)

        def pick() -> None:
            cur = QColor(getattr(self.project.global_style, field))
            c = QColorDialog.getColor(cur, self, "Выберите цвет")
            if c.isValid():
                self._set_style(**{field: c.name().upper()})
        btn.clicked.connect(pick)
        btn.setProperty("field", field)
        return btn

    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("Файл")
        for text, slot, key in (
            ("Открыть медиа…", self.open_media, QKeySequence.Open),
            ("Открыть проект…", self.open_project, "Ctrl+Shift+O"),
            ("Сохранить проект…", self.save_project, QKeySequence.Save),
            ("Экспорт видео…", self.export_video, "Ctrl+E"),
            ("Экспорт SRT…", self.export_srt, "Ctrl+Shift+E"),
        ):
            a = QAction(text, self)
            a.setShortcut(QKeySequence(key) if isinstance(key, str) else key)
            a.triggered.connect(slot)
            m_file.addAction(a)
        m_file.addSeparator()
        quit_a = QAction("Выход", self)
        quit_a.triggered.connect(self.close)
        m_file.addAction(quit_a)

    # ----------------------------------------------------------------- state
    def _sync_inspector(self) -> None:
        self._updating_ui = True
        s = self.project.global_style
        self.cmb_zone.setCurrentText(s.safe_zone)
        self.cmb_align.setCurrentText(s.alignment)
        self.spin_y.setValue(s.y_offset_pct)
        self.spin_words.setValue(s.words_per_block)
        self.edit_font.setText(s.font_family)
        self.spin_size.setValue(s.font_size)
        self.spin_weight.setValue(s.font_weight)
        self.chk_upper.setChecked(s.uppercase)
        self.spin_stroke.setValue(s.stroke_width)
        self.spin_glow.setValue(s.glow_radius)
        self.chk_contrast.setChecked(s.smart_contrast)
        self.chk_karaoke.setChecked(s.karaoke)
        self.spin_k.setValue(s.spring.k)
        self.spin_m.setValue(s.spring.m)
        self.spin_c.setValue(s.spring.c)
        self.spin_pulse.setValue(s.kick_pulse_amp)
        self.lbl_zeta.setText(f"ζ = {s.spring.zeta:.2f}  ("
                              f"{'bounce' if s.spring.zeta < 1 else 'без перелёта'})")
        for btn, field in ((self.btn_fill, "fill_color"), (self.btn_stroke, "stroke_color"),
                           (self.btn_glow, "glow_color"), (self.btn_key, "keyword_color")):
            col = getattr(s, field)
            btn.setText(col)
            btn.setStyleSheet(f"background:{col}; color:{'#000' if QColor(col).lightnessF() > .5 else '#FFF'};"
                              f"border-radius:8px; font-weight:700;")
        self._updating_ui = False

    def _set_style(self, resegment: bool = False, **kwargs) -> None:
        if self._updating_ui:
            return
        style = self.project.global_style.model_copy(update=kwargs)
        self.project.global_style = Style.model_validate(style.model_dump())
        self.scene.set_style(self.project.global_style)
        if resegment and self.project.phrases:
            self._resegment()
        self._sync_inspector()
        self.overlay.update()

    def _set_spring(self, **kwargs) -> None:
        if self._updating_ui:
            return
        sp = self.project.global_style.spring.model_copy(update=kwargs)
        self._set_style(spring=sp)

    def _resegment(self) -> None:
        from app.audio.segment import GENRE_PARAMS, PhraseSegmenter, SegmentParams
        genre = self.project.audio_profile.genre if self.project.audio_profile else "speech_podcast"
        base = GENRE_PARAMS.get(genre, GENRE_PARAMS["speech_podcast"])
        n = self.project.global_style.words_per_block
        params = SegmentParams(max_words=max(n + 1, 2), target_words=n,
                               max_chars=self.project.global_style.max_chars_per_block,
                               max_block_seconds=base.max_block_seconds,
                               gap_weight=base.gap_weight)
        words = self.project.words
        self.project.phrases = PhraseSegmenter(params).segment_words(
            words, self.project.audio_profile)
        self.timeline.load(self.project)
        self.scene.set_style(self.project.global_style)

    def _toggle_safe(self, on: bool) -> None:
        self.overlay.show_safe_zone = on
        self.overlay.update()

    def _on_genre_override(self, text: str) -> None:
        if self._updating_ui or text == "Авто" or not self.project.audio_profile:
            return
        self.project.audio_profile.genre = text  # type: ignore[assignment]
        self.project.global_style = AutoStyleEngine.get_style_for_profile(
            self.project.audio_profile, self.project.global_style.safe_zone)
        self.scene.set_style(self.project.global_style)
        self._resegment()
        self._sync_inspector()
        self.genre_badge.setText(f"жанр: {text}")

    def _on_box_moved(self, x_pct: float, y_pct: float) -> None:
        self._set_style(x_offset_pct=x_pct, y_offset_pct=y_pct)

    # ----------------------------------------------------------- media / play
    def open_media(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите медиафайл", "",
            "Медиа (*.mp4 *.mov *.mkv *.avi *.m4v *.webm *.mp3 *.wav *.m4a *.flac *.aac);;Все (*)")
        if not path:
            return
        self.load_media(path)

    def load_media(self, path: str) -> None:
        if not os.path.exists(path):
            QMessageBox.warning(self, "Файл не найден", path)
            return
        self.media_path = path
        self.project.video_path = path
        try:
            self.player.setSource(QUrl.fromLocalFile(path))
        except Exception as exc:                       # broken Qt multimedia plugin, etc.
            log.warning("player source failed: %s", exc)
            self.statusBar().showMessage(f"Предпросмотр недоступен: {exc}")
        try:
            from app.audio.extract import ffprobe_duration, probe_video
            w, h, fps = probe_video(path)
            self.project.width, self.project.height, self.project.fps = w, h, fps
            self.project.duration = ffprobe_duration(path)
            self.scene.resize(w, h)
        except Exception as exc:
            log.warning("metadata probe failed: %s", exc)
            self.statusBar().showMessage(f"Не удалось прочитать метаданные: {exc}")
        self.setWindowTitle(f"CapText AI Pro — {Path(path).name}")
        self.statusBar().showMessage(f"Загружено: {path}")
        self.overlay.update()

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶")
        else:
            self.player.play()
            self.btn_play.setText("❚❚")

    def seek_seconds(self, t: float) -> None:
        self.player.setPosition(int(t * 1000))
        self.overlay.set_time(t)

    def _on_seek(self, value: int) -> None:
        dur = max(1, self.player.duration())
        self.player.setPosition(int(dur * value / 1000))

    def _on_position(self, ms: int) -> None:
        t = ms / 1000.0
        self.overlay.set_time(t)
        dur = max(1, self.player.duration())
        if not self.seekbar.isSliderDown():
            self.seekbar.setValue(int(1000 * ms / dur))
        self.lbl_time.setText(f"{_fmt(t)} / {_fmt(dur / 1000.0)}")

    def _on_duration(self, ms: int) -> None:
        if ms > 0 and not self.project.duration:
            self.project.duration = ms / 1000.0

    # ------------------------------------------------------------- pipeline
    def run_auto(self) -> None:
        if not self.media_path:
            QMessageBox.warning(self, "Нет файла", "Сначала откройте медиафайл.")
            return
        genre = self.cmb_genre.currentText()
        opts = PipelineOptions(
            language=self.cmb_lang.currentText(),
            model_size=self.cmb_model.currentText(),
            safe_zone=self.cmb_zone.currentText(),
            forced_genre=None if genre == "Авто" else genre,
        )
        pipeline = AutoPipeline(opts)
        self._start_worker(pipeline.run, self.media_path,
                           on_done=self._on_pipeline_done, label="ИИ-пайплайн")

    def _on_pipeline_done(self, project: Project) -> None:
        self.project = project
        self.scene = SubtitleScene(project, project.width, project.height)
        self.overlay.set_scene(self.scene)
        self.timeline.load(project)
        p = project.audio_profile
        if p:
            self.genre_badge.setText(f"жанр: {p.genre}  ({max(p.probabilities.values(), default=0):.0%})")
            self.lbl_profile.setText(
                f"BPM {p.bpm:.0f} · онсетов {len(p.onsets)} · kick {len(p.kick_events)} · "
                f"слов/с {p.words_per_sec:.2f} · low {p.low_energy:.2f} / mid {p.mid_energy:.2f} "
                f"/ high {p.high_energy:.2f}")
        self._updating_ui = True
        self.cmb_genre.setCurrentText(p.genre if p else "Авто")
        self._updating_ui = False
        self._sync_inspector()
        self.statusBar().showMessage(
            f"Готово: {len(project.phrases)} блоков, {len(project.words)} слов")
        self.overlay.update()

    def _start_worker(self, fn, *args, on_done, label: str, **kwargs) -> None:
        """Start a background job safely.

        Threading contract (this is what used to crash with
        ``QThread::wait: Thread tried to wait on itself``):
          * all worker signals are delivered to *this* window with a queued
            connection, so slots run in the GUI thread;
          * the thread is stopped with ``quit()`` from the GUI thread only —
            ``wait()`` is never called from inside the worker thread;
          * cleanup happens in ``QThread.finished``, after the event loop of the
            thread has already returned;
          * objects are released via ``deleteLater()``.
        """
        if self._thread is not None:
            QMessageBox.information(self, "Занято", "Дождитесь завершения текущей задачи.")
            return

        self._set_busy(True, label)

        thread = QThread()
        worker = Worker(fn, *args, **kwargs)
        worker.moveToThread(thread)
        self._thread, self._worker = thread, worker
        self._result_handler = on_done

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        worker.succeeded.connect(self._on_worker_success, Qt.QueuedConnection)
        worker.failed.connect(self._on_worker_failed, Qt.QueuedConnection)
        # ask the thread's event loop to exit; wait() is NOT used here
        worker.done.connect(thread.quit, Qt.QueuedConnection)
        thread.finished.connect(self._on_thread_finished, Qt.QueuedConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    @Slot(object)
    def _on_worker_success(self, result) -> None:
        self.progress.setValue(100)
        handler = self._result_handler
        self._result_handler = None
        if handler is None:
            return
        try:
            handler(result)
        except Exception as exc:            # a bad result must not kill the app either
            log.exception("result handler failed")
            QMessageBox.critical(self, "Ошибка обработки результата", _friendly_error(exc))

    @Slot(str)
    def _on_worker_failed(self, msg: str) -> None:
        self._result_handler = None
        self.progress.setValue(0)
        self.progress.setFormat("%p%  —  ошибка")
        self.statusBar().showMessage("Ошибка выполнения задачи — подробности в диалоге")
        box = QMessageBox(QMessageBox.Critical, "Ошибка", msg.split("\n\n---\n")[0], parent=self)
        box.setDetailedText(msg)
        box.exec()

    @Slot()
    def _on_thread_finished(self) -> None:
        """Runs in the GUI thread after the worker thread's loop has exited."""
        self._thread = None
        self._worker = None
        self._set_busy(False, "")

    def _set_busy(self, busy: bool, label: str) -> None:
        self.btn_auto.setEnabled(not busy)
        self.btn_export.setEnabled(not busy)
        self.btn_open.setEnabled(not busy)
        if busy:
            self.progress.setValue(0)
            self.progress.setFormat(f"%p%  —  {label}")

    def closeEvent(self, event) -> None:  # noqa: N802
        """Stop a running job cleanly — wait() here is safe (GUI thread)."""
        thread = self._thread
        if thread is not None and thread.isRunning():
            answer = QMessageBox.question(
                self, "Задача выполняется",
                "Фоновая задача ещё не завершена. Закрыть приложение?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._result_handler = None
            thread.requestInterruption()
            thread.quit()
            if not thread.wait(5000):
                thread.terminate()
                thread.wait(2000)
        event.accept()

    def _on_progress(self, p: float, msg: str) -> None:
        self.progress.setValue(int(p * 100))
        self.progress.setFormat(f"%p%  —  {msg}")
        self.statusBar().showMessage(msg)

    # ------------------------------------------------------------- editing
    def _on_phrase_text(self, row: int, text: str) -> None:
        if not (0 <= row < len(self.project.phrases)):
            return
        ph = self.project.phrases[row]
        tokens = [t for t in text.split() if t]
        if not tokens:
            return
        start, end = ph.start, ph.end
        step = (end - start) / len(tokens)
        old = ph.words
        new: list[Word] = []
        for i, tok in enumerate(tokens):
            if i < len(old):
                w = old[i].model_copy(update={"text": tok})
            else:
                w = Word(text=tok, start=start + i * step, end=start + (i + 1) * step)
            new.append(w)
        ph.words = new
        self.scene.layout_engine.invalidate()
        self.overlay.update()

    def _on_phrase_time(self, row: int, start: float, end: float) -> None:
        if not (0 <= row < len(self.project.phrases)) or end <= start:
            return
        ph = self.project.phrases[row]
        old_s, old_e = ph.start, ph.end
        span = max(1e-6, old_e - old_s)
        k = (end - start) / span
        for w in ph.words:
            w.start = start + (w.start - old_s) * k
            w.end = start + (w.end - old_s) * k
        self.scene.layout_engine.invalidate()
        self.overlay.update()

    # ------------------------------------------------------------- file ops
    def save_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", "project.ctp",
                                              "CapText project (*.ctp *.json)")
        if path:
            self.project.save(path)
            self.statusBar().showMessage(f"Проект сохранён: {path}")

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "",
                                              "CapText project (*.ctp *.json)")
        if not path:
            return
        self.project = Project.load(path)
        self.scene = SubtitleScene(self.project, self.project.width, self.project.height)
        self.overlay.set_scene(self.scene)
        self.timeline.load(self.project)
        if self.project.video_path and os.path.exists(self.project.video_path):
            self.load_media(self.project.video_path)
        self._sync_inspector()

    def export_video(self) -> None:
        if not self.project.phrases:
            QMessageBox.warning(self, "Пусто", "Сначала сгенерируйте субтитры.")
            return
        from app.audio.extract import has_ffmpeg
        if not has_ffmpeg():
            QMessageBox.warning(
                self, "Нужен FFmpeg",
                "Для рендера видео требуется исполняемый FFmpeg.\n\n"
                "    pip install static-ffmpeg\n\n"
                "Сейчас доступен только экспорт SRT (Файл → Экспорт SRT…).")
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
        self._start_worker(exporter.export, self.project, path, fmt,
                           on_done=lambda r: QMessageBox.information(self, "Экспорт",
                                                                     f"Файл готов:\n{r}"),
                           label="экспорт")

    def export_srt(self) -> None:
        if not self.project.phrases:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт SRT", "captions.srt", "SRT (*.srt)")
        if path:
            VideoExporter.export_srt(self.project, path)
            self.statusBar().showMessage(f"SRT сохранён: {path}")


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

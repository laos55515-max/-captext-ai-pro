"""Видеоплеер предпросмотра с оверлеем субтитров.

Почему на macOS (Apple Silicon) раньше был пустой экран: `QVideoWidget` требует
корректно инициализированного нативного слоя, и при некоторых сочетаниях
Qt-бэкенда (`ffmpeg` вместо `darwin`) и Metal-слоя виджет остаётся чёрным, а
импорт `QtMultimediaWidgets` может падать целиком. Решение — цепочка бэкендов
с автоматическим откатом, ни один из которых не роняет приложение:

    1. VideoSinkBackend  — QVideoSink + собственная отрисовка QVideoFrame.
       Работает на любой платформе, не зависит от QtMultimediaWidgets и от
       нативных сабвью; на macOS это самый предсказуемый путь.
    2. VideoWidgetBackend — классический QVideoWidget (если доступен).
    3. GraphicsBackend    — QGraphicsView + QGraphicsVideoItem.
    4. FrameGrabBackend   — без QtMultimedia вообще: кадры декодируются PyAV
       при перемотке (звука нет, но предпросмотр субтитров работает).

Порядок можно задать переменной окружения ``CAPTEXT_PLAYER=sink|widget|graphics|frames``.
"""
from __future__ import annotations

import logging
import os
import platform
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QGraphicsScene, QGraphicsView, QLabel, QSizePolicy, QStackedLayout,
                               QVBoxLayout, QWidget)

from app.render.scene import SubtitleScene
from app.ui.theme import ACCENT, MUTED

log = logging.getLogger("captext.player")

# --- аккуратный импорт QtMultimedia -------------------------------------------------
_MM_ERROR = ""
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
    _HAS_MM = True
except Exception as exc:                                   # pragma: no cover
    _HAS_MM = False
    _MM_ERROR = str(exc)
    log.warning("QtMultimedia недоступен: %s", exc)

_HAS_MM_WIDGETS = False
if _HAS_MM:
    try:
        from PySide6.QtMultimediaWidgets import QGraphicsVideoItem, QVideoWidget
        _HAS_MM_WIDGETS = True
    except Exception as exc:                               # pragma: no cover
        log.warning("QtMultimediaWidgets недоступен: %s", exc)


def configure_media_backend() -> None:
    """Подсказать Qt правильный мультимедийный бэкенд ДО создания QApplication.

    На macOS нативный `darwin` (AVFoundation) стабильнее сборки с ffmpeg и
    корректно работает с VideoToolbox на M1/M2/M3.
    """
    if platform.system() == "Darwin":
        os.environ.setdefault("QT_MEDIA_BACKEND", "darwin")
    elif platform.system() == "Windows":
        os.environ.setdefault("QT_MEDIA_BACKEND", "windows")
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")


# ======================================================================= БЭКЕНДЫ
class _BaseSurface(QWidget):
    """Общий интерфейс поверхности вывода видео."""

    name = "base"

    def attach(self, player) -> None:      # noqa: ANN001
        raise NotImplementedError

    def clear(self) -> None:
        self.update()


class VideoSinkSurface(_BaseSurface):
    """QVideoSink + ручная отрисовка кадра. Максимально переносимый путь."""

    name = "sink"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self._image: QImage | None = None

    def attach(self, player) -> None:  # noqa: ANN001
        player.setVideoSink(self.sink)

    def _on_frame(self, frame: "QVideoFrame") -> None:
        if not frame.isValid():
            return
        img = frame.toImage()
        if not img.isNull():
            self._image = img
            self.update()

    def clear(self) -> None:
        self._image = None
        self.update()

    def current_image(self) -> QImage | None:
        return self._image

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#05070C"))
        if self._image is None or self._image.isNull():
            p.setPen(QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Предпросмотр\nОткройте медиафайл и нажмите ▶")
            p.end()
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        target = _fit_rect(self._image.size(), self.size())
        p.drawImage(target, self._image)
        p.end()


class VideoWidgetSurface(_BaseSurface):
    """Классический QVideoWidget (аппаратный путь)."""

    name = "widget"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget(self)
        self.video.setAspectRatioMode(Qt.KeepAspectRatio)
        self.video.setStyleSheet("background:#05070C;")
        lay.addWidget(self.video)

    def attach(self, player) -> None:  # noqa: ANN001
        player.setVideoOutput(self.video)


class GraphicsSurface(_BaseSurface):
    """QGraphicsView + QGraphicsVideoItem — запасной аппаратный путь."""

    name = "graphics"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene, self)
        self.view.setFrameShape(QGraphicsView.NoFrame)
        self.view.setStyleSheet("background:#05070C; border:none;")
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.item = QGraphicsVideoItem()
        self.scene.addItem(self.item)
        lay.addWidget(self.view)

    def attach(self, player) -> None:  # noqa: ANN001
        player.setVideoOutput(self.item)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.item.setSize(self.view.size())
        self.scene.setSceneRect(QRectF(0, 0, self.view.width(), self.view.height()))
        super().resizeEvent(event)


class FrameGrabSurface(_BaseSurface):
    """Работа вообще без QtMultimedia: кадры достаёт PyAV при перемотке."""

    name = "frames"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._image: QImage | None = None
        self._path = ""
        self._last_t = -1.0

    def attach(self, player) -> None:  # noqa: ANN001
        return

    def set_source(self, path: str) -> None:
        self._path = path
        self._last_t = -1.0
        self.show_time(0.0)

    def show_time(self, t: float) -> None:
        if not self._path or abs(t - self._last_t) < 0.15:
            return
        self._last_t = t
        img = grab_frame(self._path, t)
        if img is not None:
            self._image = img
            self.update()

    def current_image(self) -> QImage | None:
        return self._image

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#05070C"))
        if self._image is None:
            p.setPen(QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Звук недоступен (QtMultimedia не загрузился).\n"
                       "Кадры показываются при перемотке.")
        else:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(_fit_rect(self._image.size(), self.size()), self._image)
        p.end()


def grab_frame(path: str, t: float) -> QImage | None:
    """Достать кадр видео на секунде `t` через PyAV (без ffmpeg-бинарника)."""
    try:
        import av  # type: ignore
    except Exception:
        return None
    try:
        with av.open(path) as c:
            if not c.streams.video:
                return None
            stream = c.streams.video[0]
            stream.thread_type = "AUTO"
            if t > 0 and stream.time_base:
                c.seek(int(t / float(stream.time_base)), stream=stream, backward=True)
            for frame in c.decode(stream):
                arr = frame.to_ndarray(format="rgb24")
                h, w, _ = arr.shape
                return QImage(arr.tobytes(), w, h, 3 * w, QImage.Format_RGB888).copy()
    except Exception as exc:
        log.debug("grab_frame: %s", exc)
    return None


def _fit_rect(src: QSize, dst: QSize) -> QRectF:
    if src.width() <= 0 or src.height() <= 0:
        return QRectF(0, 0, dst.width(), dst.height())
    s = min(dst.width() / src.width(), dst.height() / src.height())
    w, h = src.width() * s, src.height() * s
    return QRectF((dst.width() - w) / 2, (dst.height() - h) / 2, w, h)


# ====================================================================== ОВЕРЛЕЙ
class SubtitleOverlay(QWidget):
    """Прозрачный слой: субтитры в реальном времени + сетка Safe Zone + drag боксa."""

    box_moved = Signal(float, float)          # x_pct, y_pct
    clicked_time = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
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

    def refresh(self) -> None:
        """Мгновенная перерисовка после правки текста/стиля (без регенерации)."""
        if self.scene is not None:
            self.scene.layout_engine.invalidate()
        self.update()

    def _fit(self) -> tuple[float, float, float]:
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
            p.save()
            p.scale(s, s)
            pen = QPen(QColor(ACCENT))
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(2.0 / max(s, 1e-3))
            p.setPen(pen)
            p.drawRect(self.scene.safe_rect())
            p.setPen(QPen(QColor(255, 255, 255, 40), 1.0 / max(s, 1e-3)))
            p.drawRect(QRectF(0, 0, self.scene.width - 1, self.scene.height - 1))
            p.restore()
        self.scene.paint(p, self.t, scale=s)
        p.end()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self.scene and e.button() == Qt.LeftButton:
            self._drag = True
            s, _ox, oy = self._fit()
            y = (e.position().y() - oy) / max(1.0, self.scene.height * s)
            self._drag_dy = self.scene.project.global_style.y_offset_pct - y

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag and self.scene:
            s, ox, oy = self._fit()
            x = (e.position().x() - ox) / max(1.0, self.scene.width * s)
            y = (e.position().y() - oy) / max(1.0, self.scene.height * s) + self._drag_dy
            self.box_moved.emit(min(max(x, 0.05), 0.95), min(max(y, 0.03), 0.97))

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag = False


# ======================================================================= ПЛЕЕР
class VideoPlayer(QWidget):
    """Плеер с оверлеем субтитров и единым API независимо от бэкенда."""

    position_changed = Signal(float)      # секунды
    duration_changed = Signal(float)
    playing_changed = Signal(bool)
    error = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(360)
        self._duration = 0.0
        self._position = 0.0
        self._path = ""
        self.player = None
        self.audio_output = None

        self.surface = self._create_surface()
        self.overlay = SubtitleOverlay(self)

        # оверлей лежит ПОВЕРХ поверхности видео в одном стеке
        self._stack = QStackedLayout(self)
        self._stack.setStackingMode(QStackedLayout.StackAll)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self.overlay)     # индекс 0 — рисуется поверх
        self._stack.addWidget(self.surface)

        self._init_player()

        # таймер обновления оверлея: плавные 60 FPS даже если сигналы плеера редкие
        self._ticker = QTimer(self)
        self._ticker.setInterval(16)
        self._ticker.timeout.connect(self._tick)

    # ------------------------------------------------------------- бэкенды
    def _create_surface(self) -> _BaseSurface:
        preferred = os.environ.get("CAPTEXT_PLAYER", "").strip().lower()
        order = ["sink", "widget", "graphics", "frames"]
        if platform.system() == "Darwin":
            order = ["sink", "graphics", "widget", "frames"]   # на macOS sink надёжнее
        if preferred in order:
            order.remove(preferred)
            order.insert(0, preferred)

        for name in order:
            try:
                if name == "sink" and _HAS_MM:
                    s = VideoSinkSurface(self)
                elif name == "widget" and _HAS_MM_WIDGETS:
                    s = VideoWidgetSurface(self)
                elif name == "graphics" and _HAS_MM_WIDGETS:
                    s = GraphicsSurface(self)
                elif name == "frames":
                    s = FrameGrabSurface(self)
                else:
                    continue
                log.info("Видео-бэкенд: %s", s.name)
                return s
            except Exception as exc:
                log.warning("бэкенд %s недоступен: %s", name, exc)
        return FrameGrabSurface(self)

    def backend_name(self) -> str:
        return self.surface.name

    def has_audio(self) -> bool:
        return self.player is not None

    def _init_player(self) -> None:
        if not _HAS_MM or isinstance(self.surface, FrameGrabSurface):
            if _MM_ERROR:
                log.warning("Плеер работает без звука: %s", _MM_ERROR)
            return
        try:
            self.player = QMediaPlayer(self)
            self.audio_output = QAudioOutput(self)
            self.audio_output.setVolume(0.9)
            self.player.setAudioOutput(self.audio_output)
            self.surface.attach(self.player)
            self.player.positionChanged.connect(self._on_position_ms)
            self.player.durationChanged.connect(self._on_duration_ms)
            self.player.playbackStateChanged.connect(
                lambda st: self.playing_changed.emit(st == QMediaPlayer.PlayingState))
            self.player.errorOccurred.connect(
                lambda _err, msg="": self.error.emit(msg or "Ошибка воспроизведения"))
        except Exception as exc:                       # pragma: no cover
            log.exception("не удалось создать QMediaPlayer")
            self.player = None
            self.error.emit(f"Плеер недоступен: {exc}")

    # --------------------------------------------------------------- сцена
    def set_scene(self, scene: SubtitleScene) -> None:
        self.overlay.set_scene(scene)

    def refresh_overlay(self) -> None:
        self.overlay.refresh()

    def set_safe_zone_visible(self, visible: bool) -> None:
        self.overlay.show_safe_zone = visible
        self.overlay.update()

    # ---------------------------------------------------------------- API
    def load(self, path: str) -> None:
        self._path = path
        self._position = 0.0
        if self.player is not None:
            self.player.setSource(QUrl.fromLocalFile(path))
            # даём Qt дорисовать первый кадр, иначе на macOS экран остаётся пустым
            QTimer.singleShot(60, lambda: self.player.setPosition(0))
        if isinstance(self.surface, FrameGrabSurface):
            self.surface.set_source(path)
            try:
                from app.audio.extract import ffprobe_duration
                self._duration = ffprobe_duration(path)
                self.duration_changed.emit(self._duration)
            except Exception as exc:
                log.debug("длительность неизвестна: %s", exc)
        self.overlay.set_time(0.0)
        self.overlay.raise_()

    def play(self) -> None:
        if self.player is not None:
            self.player.play()
        self._ticker.start()
        self.playing_changed.emit(True)

    def pause(self) -> None:
        if self.player is not None:
            self.player.pause()
        self._ticker.stop()
        self.playing_changed.emit(False)

    def toggle(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def is_playing(self) -> bool:
        if self.player is not None:
            return self.player.playbackState() == QMediaPlayer.PlayingState
        return self._ticker.isActive()

    def seek(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        self._position = seconds
        if self.player is not None:
            self.player.setPosition(int(seconds * 1000))
        if isinstance(self.surface, FrameGrabSurface):
            self.surface.show_time(seconds)
        self.overlay.set_time(seconds)
        self.position_changed.emit(seconds)

    def position(self) -> float:
        return self._position

    def duration(self) -> float:
        return self._duration

    def set_volume(self, value: float) -> None:
        if self.audio_output is not None:
            self.audio_output.setVolume(max(0.0, min(1.0, value)))

    def current_video_image(self) -> QImage | None:
        """Текущий кадр — используется Smart Contrast для подбора цвета текста."""
        getter = getattr(self.surface, "current_image", None)
        return getter() if callable(getter) else None

    # ------------------------------------------------------------- события
    def _on_position_ms(self, ms: int) -> None:
        self._position = ms / 1000.0
        self.overlay.set_time(self._position)
        self.position_changed.emit(self._position)

    def _on_duration_ms(self, ms: int) -> None:
        if ms > 0:
            self._duration = ms / 1000.0
            self.duration_changed.emit(self._duration)

    def _tick(self) -> None:
        """Плавное время для оверлея (и «программное» время без QtMultimedia)."""
        if self.player is not None:
            self.overlay.set_time(self.player.position() / 1000.0)
            return
        self._position += self._ticker.interval() / 1000.0
        if self._duration and self._position > self._duration:
            self._position = self._duration
            self.pause()
        self.overlay.set_time(self._position)
        if isinstance(self.surface, FrameGrabSurface):
            self.surface.show_time(self._position)
        self.position_changed.emit(self._position)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.overlay.setGeometry(0, 0, self.width(), self.height())
        self.overlay.raise_()

    def shutdown(self) -> None:
        self._ticker.stop()
        if self.player is not None:
            self.player.stop()


def multimedia_status() -> str:
    """Короткая строка для статус-бара/диагностики."""
    if not _HAS_MM:
        return f"QtMultimedia недоступен ({_MM_ERROR}); предпросмотр кадрами PyAV"
    return "QtMultimedia OK" + ("" if _HAS_MM_WIDGETS else " (без QtMultimediaWidgets)")

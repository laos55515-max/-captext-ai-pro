"""Responsive Aspect Ratio Canvas.

Смена формата (9:16 / 16:9 / 1:1 / 4:3) — это РЕАЛЬНАЯ смена целевого
разрешения композиции (1080x1920, 1920x1080, 1080x1080, 1440x1080), а не просто
рамка поверх видео. Исходное видео вписывается в композицию одним из режимов:

    fit_blur — AspectFit, поля закрыты размытой копией кадра (TikTok/Reels);
    fit      — AspectFit, поля чёрные;
    fill     — AspectFill, центральный кроп без полей.

Все координаты субтитров нормализованы: (u, v) ∈ [0, 1] от размера композиции,
поэтому при смене формата раскладка масштабируется, а не «съезжает».
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from app.core.models import COMPOSITION_SIZES, FitMode
from app.render.scene import SubtitleScene

__all__ = ["CanvasGeometry", "composition_size", "viewport_rect", "source_rect",
           "to_normalized", "from_normalized", "render_background", "VideoCanvas"]


# ------------------------------------------------------------------ геометрия
@dataclass(frozen=True)
class CanvasGeometry:
    """Как композиция ложится в виджет и как видео ложится в композицию."""

    comp_width: int
    comp_height: int
    scale: float            # композиция → пиксели виджета
    offset_x: float
    offset_y: float

    @property
    def viewport(self) -> QRectF:
        return QRectF(self.offset_x, self.offset_y,
                      self.comp_width * self.scale, self.comp_height * self.scale)

    def to_widget(self, x: float, y: float) -> QPointF:
        return QPointF(self.offset_x + x * self.scale, self.offset_y + y * self.scale)

    def to_composition(self, x: float, y: float) -> QPointF:
        s = self.scale or 1.0
        return QPointF((x - self.offset_x) / s, (y - self.offset_y) / s)


def composition_size(aspect: str) -> tuple[int, int]:
    """Целевое разрешение рендера для соотношения сторон."""
    return COMPOSITION_SIZES.get(aspect, (1080, 1920))


def viewport_rect(widget_w: int, widget_h: int, comp_w: int, comp_h: int) -> CanvasGeometry:
    """Вписать композицию в виджет с центрированием (letterbox)."""
    if comp_w <= 0 or comp_h <= 0 or widget_w <= 0 or widget_h <= 0:
        return CanvasGeometry(max(1, comp_w), max(1, comp_h), 1.0, 0.0, 0.0)
    s = min(widget_w / comp_w, widget_h / comp_h)
    return CanvasGeometry(comp_w, comp_h, s,
                          (widget_w - comp_w * s) / 2.0, (widget_h - comp_h * s) / 2.0)


def source_rect(src: QSize, comp_w: int, comp_h: int, mode: FitMode = "fit_blur") -> QRectF:
    """Прямоугольник в координатах КОМПОЗИЦИИ, куда рисуется исходный кадр."""
    sw, sh = max(1, src.width()), max(1, src.height())
    if mode == "fill":
        s = max(comp_w / sw, comp_h / sh)       # AspectFill — центральный кроп
    else:
        s = min(comp_w / sw, comp_h / sh)       # AspectFit
    w, h = sw * s, sh * s
    return QRectF((comp_w - w) / 2.0, (comp_h - h) / 2.0, w, h)


def to_normalized(x: float, y: float, comp_w: int, comp_h: int) -> tuple[float, float]:
    return (x / max(1, comp_w), y / max(1, comp_h))


def from_normalized(u: float, v: float, comp_w: int, comp_h: int) -> tuple[float, float]:
    return (u * comp_w, v * comp_h)


# --------------------------------------------------------------------- фон
def render_background(painter: QPainter, frame: QImage | None, comp_w: int, comp_h: int,
                      mode: FitMode = "fit_blur") -> None:
    """Нарисовать кадр в композиции: чёрные поля, размытые поля или кроп."""
    painter.fillRect(QRectF(0, 0, comp_w, comp_h), QColor(0, 0, 0))
    if frame is None or frame.isNull():
        return
    target = source_rect(frame.size(), comp_w, comp_h, mode)
    if mode == "fit_blur" and (target.width() < comp_w - 1 or target.height() < comp_h - 1):
        cover = source_rect(frame.size(), comp_w, comp_h, "fill")
        small = frame.scaled(max(1, int(cover.width() / 14)),
                             max(1, int(cover.height() / 14)),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        blurred = small.scaled(int(cover.width()), int(cover.height()),
                               Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        painter.save()
        painter.setOpacity(0.85)
        painter.drawImage(cover, blurred)
        painter.restore()
    painter.save()
    if mode == "fill":
        painter.setClipRect(QRectF(0, 0, comp_w, comp_h))
    painter.drawImage(target, frame)
    painter.restore()


# ------------------------------------------------------------------ виджет
class VideoCanvas(QWidget):
    """Виджет: фон-кадр в выбранном формате + субтитры сцены поверх."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.scene: SubtitleScene | None = None
        self.frame: QImage | None = None
        self.fit_mode: FitMode = "fit_blur"
        self.aspect: str = "9:16"
        self.t = 0.0

    # -- состояние
    def set_scene(self, scene: SubtitleScene) -> None:
        self.scene = scene
        self.aspect = scene.project.global_style.safe_zone
        self.fit_mode = scene.project.global_style.fit_mode
        self.update()

    def set_aspect(self, aspect: str) -> tuple[int, int]:
        self.aspect = aspect
        size = composition_size(aspect)
        if self.scene is not None:
            size = self.scene.use_aspect(aspect)
        self.update()
        return size

    def set_fit_mode(self, mode: FitMode) -> None:
        self.fit_mode = mode
        if self.scene is not None:
            self.scene.set_style(
                self.scene.project.global_style.model_copy(update={"fit_mode": mode}))
        self.update()

    def set_frame(self, frame: QImage | None) -> None:
        self.frame = frame
        self.update()

    def set_time(self, t: float) -> None:
        if abs(t - self.t) > 1e-4:
            self.t = t
            self.update()

    def geometry_info(self) -> CanvasGeometry:
        cw, ch = (self.scene.width, self.scene.height) if self.scene \
            else composition_size(self.aspect)
        return viewport_rect(self.width(), self.height(), cw, ch)

    def normalized_at(self, x: float, y: float) -> tuple[float, float]:
        g = self.geometry_info()
        p = g.to_composition(x, y)
        return to_normalized(p.x(), p.y(), g.comp_width, g.comp_height)

    # -- отрисовка
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(8, 9, 12))
        g = self.geometry_info()
        p.translate(g.offset_x, g.offset_y)
        p.scale(g.scale, g.scale)
        render_background(p, self.frame, g.comp_width, g.comp_height, self.fit_mode)
        if self.scene is not None:
            self.scene.paint(p, self.t)
        p.end()

    def render_composition(self, t: float) -> QImage:
        """Кадр композиции в полном разрешении (тот же код, что и экспорт)."""
        cw, ch = (self.scene.width, self.scene.height) if self.scene \
            else composition_size(self.aspect)
        img = QImage(cw, ch, QImage.Format_RGBA8888)
        img.fill(QColor(0, 0, 0))
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        render_background(p, self.frame, cw, ch, self.fit_mode)
        if self.scene is not None:
            self.scene.paint(p, t)
        p.end()
        return img

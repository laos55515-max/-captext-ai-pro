"""SubtitleScene: субтитры как чистая функция времени. Один движок: превью == экспорт.

v3: геометрия берётся из `LayoutEngine` (считается один раз на фразу), а анимация
меняет ТОЛЬКО трансформы вокруг фиксированного пивота слова — стиль больше не
«рвётся» и текст не прыгает при пересчёте bbox.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from app.core.models import Phrase, Project, Style
from app.render.animation_physics import clamp
from app.render.effects_contrast import (SmartContrastAI, TextureCache, qcolor,
                                         safe_zone_rect)
from app.render.layout_engine import (LEAD_IN, TAIL, LayoutEngine, PhraseLayout,
                                      apply_word_transform)


class SubtitleScene:
    """``evaluate(t)`` не имеет состояния: перемотка даёт ровно тот же кадр."""

    def __init__(self, project: Project, width: int | None = None,
                 height: int | None = None) -> None:
        self.project = project
        base_w, base_h = project.global_style.composition_size
        self.width = int(width or project.width or base_w)
        self.height = int(height or project.height or base_h)
        self.cache = TextureCache()
        self.contrast = SmartContrastAI()
        self.layout_engine = LayoutEngine(self.cache)

    # ------------------------------------------------------------- geometry
    def resize(self, width: int, height: int) -> None:
        if (width, height) != (self.width, self.height):
            self.width, self.height = int(width), int(height)
            self.layout_engine.invalidate()

    def use_aspect(self, safe_zone: str) -> tuple[int, int]:
        """Переключить соотношение сторон и вернуть новое разрешение композиции."""
        style = self.project.global_style.model_copy(update={"safe_zone": safe_zone})
        self.set_style(style)
        w, h = style.composition_size
        self.resize(w, h)
        return w, h

    def set_style(self, style: Style) -> None:
        self.project.global_style = style
        self.cache.clear()
        self.layout_engine.invalidate()

    def safe_rect(self) -> QRectF:
        return safe_zone_rect(self.project.global_style.safe_zone, self.width, self.height)

    def _style_for(self, phrase: Phrase) -> Style:
        return phrase.resolved_style(self.project.global_style)

    # --------------------------------------------------------------- layout
    def layout_phrase(self, index: int) -> PhraseLayout:
        phrase = self.project.phrases[index]
        style = self._style_for(phrase)
        decision = self.contrast.decide(style)
        eff = style.model_copy(update={
            "fill_color": decision.fill_color,
            "stroke_color": decision.stroke_color,
            "stroke_width": decision.stroke_width,
        })
        layout = self.layout_engine.layout(phrase, index, eff, self.width, self.height,
                                           self.safe_rect())
        layout.decision = decision           # type: ignore[attr-defined]
        return layout

    # ------------------------------------------------------------- evaluate
    def visible_phrases(self, t: float) -> list[int]:
        return [i for i, ph in enumerate(self.project.phrases)
                if ph.words and ph.start - LEAD_IN <= t <= ph.end + TAIL]

    def evaluate(self, t: float) -> list[dict[str, Any]]:
        """Все отрисовываемые элементы с их трансформами в момент ``t``."""
        draw: list[dict[str, Any]] = []
        for idx in self.visible_phrases(t):
            layout = self.layout_phrase(idx)
            style: Style = layout.style
            decision = getattr(layout, "decision", None)
            alpha = layout.phrase_alpha(t)
            if alpha <= 0.001:
                continue

            bx, by, bw, bh = layout.box
            # ---- ПОДЛОЖКА: мастер-тумблер «Полупрозрачная подложка» из UI.
            # Рисуется под ВСЕЙ строкой субтитра. Никаких красных боксов под
            # отдельным словом здесь нет и быть не может.
            card_on, card_color, card_radius = style.line_card
            if not card_on and decision is not None and decision.backing:
                a = int(round(max(0.0, min(1.0, decision.backing_opacity)) * 255))
                card_on, card_color, card_radius = True, f"{style.backing_color[:7]}{a:02X}", 8
            if card_on:
                draw.append({
                    "type": "card", "phrase": idx,
                    "x": bx, "y": by, "w": bw, "h": bh,
                    "radius": float(card_radius),
                    "color": card_color, "opacity": alpha,
                    "blur": bool(style.card_blur),
                    "border_color": style.card_border_color,
                    "border_width": int(style.card_border_width),
                })
            if style.scanlines:
                draw.append({"type": "scanlines", "phrase": idx, "x": bx, "y": by,
                             "w": bw, "h": bh, "opacity": 0.22 * alpha})

            for info in layout.words:
                tr = apply_word_transform(info, t, style, layout.start)
                # Неактивные слова могут быть приглушены (Descript Minimal: 50%)
                fade = 1.0 if tr.active else float(style.inactive_opacity)
                op = tr.opacity * alpha * fade
                if not tr.visible or op <= 0.003:
                    continue
                w = info.word
                cx, cy = info.pivot
                item: dict[str, Any] = {
                    "type": "word", "phrase": idx, "text": w.text,
                    "start": w.start, "end": w.end, "confidence": w.confidence,
                    "is_keyword": w.is_keyword,
                    "texture": info.texture, "texture_hi": info.texture_active,
                    "cx": cx, "cy": cy, "x": info.x, "y": info.y,
                    "advance": info.width,
                    "scale": tr.scale, "rotation": tr.rotation,
                    "x_offset": tr.offset_x, "y_offset": tr.offset_y,
                    "opacity": op, "active": tr.active,
                    "karaoke": bool(style.karaoke) or style.animation == "karaoke",
                    "karaoke_progress": tr.progress,
                    # Цвет активного слова = «Акцент» из UI (текст, не фон!)
                    "karaoke_color": style.accent_color,
                    "chroma": tr.chroma_shift,
                    "chars_visible": tr.chars_visible,
                    "cursor": tr.cursor,
                    "glow": tr.glow,
                    "stroke2_color": style.stroke2_color,
                    "stroke2_width": style.stroke2_width,
                }
                # Плашка под активным словом — ТОЛЬКО если пользователь включил
                # соответствующий тумблер. По умолчанию выключена.
                if tr.active and style.active_card_enabled:
                    pad = max(6.0, style.font_size * 0.12)
                    draw.append({
                        "type": "active_card", "phrase": idx,
                        "x": info.x - pad, "y": info.y - pad * 0.4,
                        "w": info.width + pad * 2, "h": info.height + pad * 0.8,
                        "radius": float(style.card_radius),
                        "color": style.active_card_color, "opacity": alpha,
                        "scale": tr.scale, "cx": cx, "cy": cy,
                        "y_offset": tr.offset_y, "rotation": tr.rotation,
                    })
                draw.append(item)
        return draw

    # --------------------------------------------------------------- render
    def paint(self, painter: QPainter, t: float, scale: float = 1.0) -> None:
        painter.save()
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        if scale != 1.0:
            painter.scale(scale, scale)
        for item in self.evaluate(t):
            kind = item["type"]
            if kind == "card":
                self._paint_card(painter, item)
            elif kind == "active_card":
                self._paint_active_card(painter, item)
            elif kind == "scanlines":
                self._paint_scanlines(painter, item)
            else:
                self._paint_word(painter, item)
        painter.restore()

    # -- элементы
    def _paint_card(self, painter: QPainter, item: dict[str, Any]) -> None:
        painter.save()
        painter.setOpacity(item["opacity"])
        rect = QRectF(item["x"], item["y"], item["w"], item["h"])
        painter.setPen(Qt.NoPen)
        painter.setBrush(qcolor(item["color"]))
        painter.drawRoundedRect(rect, item["radius"], item["radius"])
        if item.get("border_width"):
            pen = QPen(qcolor(item["border_color"]))
            pen.setWidthF(float(item["border_width"]))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, item["radius"], item["radius"])
        painter.restore()

    def _paint_active_card(self, painter: QPainter, item: dict[str, Any]) -> None:
        painter.save()
        painter.setOpacity(item["opacity"])
        painter.translate(item["cx"], item["cy"] + item["y_offset"])
        painter.scale(item["scale"], item["scale"])
        if item["rotation"]:
            painter.rotate(item["rotation"])
        painter.translate(-item["cx"], -item["cy"])
        painter.setPen(Qt.NoPen)
        painter.setBrush(qcolor(item["color"]))
        painter.drawRoundedRect(QRectF(item["x"], item["y"], item["w"], item["h"]),
                                item["radius"], item["radius"])
        painter.restore()

    def _paint_scanlines(self, painter: QPainter, item: dict[str, Any]) -> None:
        painter.save()
        painter.setOpacity(item["opacity"])
        pen = QPen(QColor(0, 0, 0))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        y = item["y"]
        while y < item["y"] + item["h"]:
            painter.drawLine(QPointF(item["x"], y), QPointF(item["x"] + item["w"], y))
            y += 4.0
        painter.restore()

    def _paint_word(self, painter: QPainter, item: dict[str, Any]) -> None:
        tex = item["texture"]
        painter.save()
        painter.setOpacity(item["opacity"])
        painter.translate(item["cx"] + item.get("x_offset", 0.0),
                          item["cy"] + item["y_offset"])
        painter.scale(item["scale"], item["scale"])
        if item["rotation"]:
            painter.rotate(item["rotation"])
        painter.translate(-tex.width / 2.0, -tex.height / 2.0)

        clip = None
        n = item.get("chars_visible", -1)
        if n >= 0 and item["text"]:
            frac = clamp(n / max(1, len(item["text"])))
            clip = QRectF(0, 0, tex.pad + item["advance"] * frac, tex.height)
            painter.setClipRect(clip)

        shift = float(item.get("chroma", 0.0))
        if shift > 0.05:            # RGB-сплит: красный влево, синий вправо
            painter.save()
            painter.setOpacity(item["opacity"] * 0.45)
            painter.setCompositionMode(QPainter.CompositionMode_Plus)
            painter.drawImage(QPointF(-shift, 0), _tinted(tex.image, QColor(255, 0, 0)))
            painter.drawImage(QPointF(shift, 0), _tinted(tex.image, QColor(0, 160, 255)))
            painter.restore()

        painter.drawImage(QPointF(0, 0), tex.image)

        # Glow Pulse: аддитивное свечение акцентным цветом поверх слова
        glow = float(item.get("glow", 0.0))
        if glow > 0.01 and item.get("texture_hi") is not None:
            painter.save()
            painter.setOpacity(item["opacity"] * 0.65 * glow)
            painter.setCompositionMode(QPainter.CompositionMode_Plus)
            painter.drawImage(QPointF(0, 0), item["texture_hi"].image)
            painter.restore()

        hi = item.get("texture_hi")
        if item["karaoke"] and hi is not None and item["karaoke_progress"] > 0.0:
            p = clamp(item["karaoke_progress"])
            painter.save()
            painter.setClipRect(QRectF(0, 0, tex.pad + item["advance"] * p, tex.height))
            painter.drawImage(QPointF(0, 0), hi.image)
            painter.restore()

        if item.get("cursor"):
            painter.fillRect(QRectF(tex.pad + item["advance"] + 4, tex.pad * 0.5,
                                    max(3.0, item["advance"] * 0.06),
                                    tex.height - tex.pad),
                             qcolor(item["karaoke_color"]))
        painter.restore()

    # ------------------------------------------------------------- frames
    def render_frame(self, t: float, transparent: bool = True) -> QImage:
        img = QImage(self.width, self.height, QImage.Format_RGBA8888)
        img.fill(Qt.transparent if transparent else QColor(0, 0, 0))
        p = QPainter(img)
        self.paint(p, t)
        p.end()
        return img

    def render_rgba(self, t: float) -> bytes:
        """Сырые RGBA-байты для пайпа ffmpeg (пиксель-в-пиксель как превью)."""
        img = self.render_frame(t, transparent=True).convertToFormat(QImage.Format_RGBA8888)
        bpl, w, h = img.bytesPerLine(), img.width(), img.height()
        raw = bytes(img.constBits())[: bpl * h]
        if bpl == w * 4:
            return raw
        arr = np.frombuffer(raw, np.uint8).reshape(h, bpl)[:, : w * 4]
        return np.ascontiguousarray(arr).tobytes()

    # ------------------------------------------------------------- contrast
    def update_contrast(self, frame_rgb: Optional[np.ndarray]) -> None:
        """Передать уменьшенный RGB-кадр; сэмплируется область под субтитром."""
        if frame_rgb is None or frame_rgb.size == 0 or not self.project.phrases:
            return
        h, w = frame_rgb.shape[:2]
        style = self.project.global_style
        y0 = int(clamp(style.y_offset_pct - 0.09) * h)
        y1 = int(clamp(style.y_offset_pct + 0.09) * h)
        crop = frame_rgb[max(0, y0):max(1, y1), int(w * 0.08):int(w * 0.92), :3]
        self.contrast.update_background(crop)
        self.layout_engine.invalidate()


def _tinted(image: QImage, color: QColor) -> QImage:
    """Копия текстуры, перекрашенная в один цвет (для хроматической аберрации)."""
    out = QImage(image.size(), QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.drawImage(0, 0, image)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(out.rect(), color)
    p.end()
    return out

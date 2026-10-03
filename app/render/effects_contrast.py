"""Text effects (stroke / glow / shadow / 3D) and WCAG Smart Contrast AI."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPainterPathStroker, QPen)

from app.core.models import Style

# --------------------------------------------------------------------- utils


def qcolor(hex_color: str, alpha: float = 1.0) -> QColor:
    """Принимает #RRGGBB и #RRGGBBAA (Qt сам понимает только #AARRGGBB)."""
    text = (hex_color or "").strip()
    if len(text) == 9 and text.startswith("#"):
        c = QColor("#" + text[7:9] + text[1:7])
    else:
        c = QColor(text)
    if not c.isValid():
        c = QColor("#FFFFFF")
    c.setAlphaF(max(0.0, min(1.0, alpha * c.alphaF())))
    return c


def make_font(style: Style) -> QFont:
    f = QFont(style.font_family, -1)
    f.setPixelSize(style.font_size)
    f.setWeight(QFont.Weight(max(1, min(999, style.font_weight))))
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


def _gaussian_kernel(sigma: float) -> np.ndarray:
    r = max(1, int(round(sigma * 3)))
    x = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-(x ** 2) / (2 * sigma * sigma))
    return k / k.sum()


def blur_alpha(alpha: np.ndarray, sigma: float) -> np.ndarray:
    """Separable gaussian blur of a float32 alpha mask."""
    if sigma <= 0.05:
        return alpha
    k = _gaussian_kernel(sigma)
    pad = len(k) // 2
    a = np.pad(alpha, ((0, 0), (pad, pad)), mode="constant")
    a = np.apply_along_axis(lambda m: np.convolve(m, k, mode="valid"), 1, a)
    a = np.pad(a, ((pad, pad), (0, 0)), mode="constant")
    a = np.apply_along_axis(lambda m: np.convolve(m, k, mode="valid"), 0, a)
    return a.astype(np.float32)


def image_to_array(img: QImage) -> np.ndarray:
    """QImage (Format_RGBA8888) -> HxWx4 uint8 array (copy)."""
    img = img.convertToFormat(QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    arr = np.frombuffer(bytes(ptr)[: h * img.bytesPerLine()], dtype=np.uint8)
    arr = arr.reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :]
    return np.ascontiguousarray(arr)


def array_to_image(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr.astype(np.uint8))
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, 4 * w, QImage.Format_RGBA8888)
    return img.copy()


# ------------------------------------------------------------ word textures
@dataclass(frozen=True)
class WordTexture:
    image: QImage           # premultiplied-free RGBA8888
    advance: float          # layout advance width in px
    ascent: float
    descent: float
    pad: int

    @property
    def width(self) -> int:
        return self.image.width()

    @property
    def height(self) -> int:
        return self.image.height()


def _text_path(text: str, font: QFont, pad: float) -> tuple[QPainterPath, QFontMetricsF]:
    fm = QFontMetricsF(font)
    path = QPainterPath()
    path.addText(QPointF(pad, pad + fm.ascent()), font, text)
    return path, fm


def render_word_texture(text: str, style: Style, color_hex: str | None = None) -> WordTexture:
    """Render one word once (fill + stroke + 3D + glow + shadow) into an RGBA texture."""
    if style.uppercase:
        text = text.upper()
    font = make_font(style)
    fm = QFontMetricsF(font)
    glow_sigma = 0.04 * style.font_size * (style.glow_radius / 12.0) if style.glow_radius else 0.0
    pad = int(max(style.stroke_width * 1.6 + 4,
                  style.glow_radius * 2.2,
                  abs(style.shadow_offset[0]) + style.shadow_blur * 2,
                  abs(style.shadow_offset[1]) + style.shadow_blur * 2,
                  style.three_d_depth + 4))
    adv = fm.horizontalAdvance(text)
    w = int(adv + 2 * pad) + 2
    h = int(fm.height() + 2 * pad) + 2

    path, _ = _text_path(text, font, pad)
    fill_hex = color_hex or style.fill_color

    img = QImage(w, h, QImage.Format_RGBA8888)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing |
                     QPainter.SmoothPixmapTransform)

    # 1) drop shadow (blurred alpha mask, drawn first)
    if style.shadow_opacity > 0 and (style.shadow_blur > 0 or any(style.shadow_offset)):
        mask = _path_mask(path, w, h, style.stroke_width)
        sh = blur_alpha(mask, max(0.5, style.shadow_blur / 2.0)) * style.shadow_opacity
        sh = _shift(sh, style.shadow_offset[0], style.shadow_offset[1])
        p.drawImage(0, 0, _tint(sh, QColor(0, 0, 0)))

    # 2) neon glow (two passes: sigma and 2*sigma, additive look)
    if glow_sigma > 0 and style.glow_intensity > 0:
        mask = _path_mask(path, w, h, style.stroke_width)
        g = (blur_alpha(mask, glow_sigma) * 0.75 + blur_alpha(mask, glow_sigma * 2.0) * 0.55)
        g = np.clip(g * style.glow_intensity, 0.0, 1.0)
        p.setCompositionMode(QPainter.CompositionMode_Plus)
        p.drawImage(0, 0, _tint(g, qcolor(style.glow_color)))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)

    # 3) 3D extrusion
    if style.three_d_depth > 0:
        base = qcolor(style.three_d_color)
        for d in range(style.three_d_depth, 0, -1):
            f = d / style.three_d_depth
            c = QColor(base)
            c.setRedF(base.redF() * (1 - 0.4 * f))
            c.setGreenF(base.greenF() * (1 - 0.4 * f))
            c.setBlueF(base.blueF() * (1 - 0.4 * f))
            p.translate(d * 0.7, d * 0.7)
            p.fillPath(path, c)
            p.resetTransform()

    # 4) stroke under the fill
    if style.stroke_width > 0:
        stroker = QPainterPathStroker()
        stroker.setWidth(style.stroke_width * 2.0)
        stroker.setJoinStyle(Qt.RoundJoin)
        stroker.setCapStyle(Qt.RoundCap)
        p.fillPath(stroker.createStroke(path), qcolor(style.stroke_color))

    # 5) fill (subtle vertical gradient for depth)
    grad = QLinearGradient(0, pad, 0, pad + fm.height())
    top = qcolor(fill_hex)
    bot = QColor(top)
    bot.setRedF(min(1.0, top.redF() * 0.88))
    bot.setGreenF(min(1.0, top.greenF() * 0.88))
    bot.setBlueF(min(1.0, top.blueF() * 0.92))
    grad.setColorAt(0.0, top)
    grad.setColorAt(1.0, bot)
    p.fillPath(path, QBrush(grad))
    p.end()

    return WordTexture(image=img, advance=float(adv), ascent=float(fm.ascent()),
                       descent=float(fm.descent()), pad=pad)


def _path_mask(path: QPainterPath, w: int, h: int, stroke_width: int) -> np.ndarray:
    img = QImage(w, h, QImage.Format_RGBA8888)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    if stroke_width > 0:
        p.setPen(QPen(QColor(255, 255, 255), stroke_width * 2.0, Qt.SolidLine, Qt.RoundCap,
                      Qt.RoundJoin))
        p.drawPath(path)
    p.fillPath(path, QColor(255, 255, 255))
    p.end()
    return image_to_array(img)[:, :, 3].astype(np.float32) / 255.0


def _tint(mask: np.ndarray, color: QColor) -> QImage:
    h, w = mask.shape
    out = np.zeros((h, w, 4), dtype=np.uint8)
    out[:, :, 0] = color.red()
    out[:, :, 1] = color.green()
    out[:, :, 2] = color.blue()
    out[:, :, 3] = np.clip(mask * 255.0, 0, 255).astype(np.uint8)
    return array_to_image(out)


def _shift(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(mask)
    h, w = mask.shape
    xs, xd = (0, dx) if dx >= 0 else (-dx, 0)
    ys, yd = (0, dy) if dy >= 0 else (-dy, 0)
    ww, hh = w - abs(dx), h - abs(dy)
    if ww > 0 and hh > 0:
        out[yd:yd + hh, xd:xd + ww] = mask[ys:ys + hh, xs:xs + ww]
    return out


class TextureCache:
    """Caches word textures; invalidated whenever the style fingerprint changes."""

    def __init__(self) -> None:
        self._cache: dict[tuple, WordTexture] = {}
        self._fingerprint: tuple | None = None

    @staticmethod
    def fingerprint(style: Style) -> tuple:
        d = style.model_dump()
        for k in ("words_per_block", "max_chars_per_block", "y_offset_pct", "x_offset_pct",
                  "safe_zone", "alignment", "karaoke", "pop_in_per_word", "kick_pulse_amp",
                  "kick_pulse_tau", "max_width_pct", "line_spacing"):
            d.pop(k, None)
        d.pop("spring", None)
        return tuple(sorted((k, str(v)) for k, v in d.items()))

    def get(self, text: str, style: Style, color_hex: str | None = None) -> WordTexture:
        fp = self.fingerprint(style)
        if fp != self._fingerprint:
            self._cache.clear()
            self._fingerprint = fp
        key = (text, color_hex or style.fill_color)
        tex = self._cache.get(key)
        if tex is None:
            tex = render_word_texture(text, style, color_hex)
            self._cache[key] = tex
        return tex

    def clear(self) -> None:
        self._cache.clear()
        self._fingerprint = None


# ---------------------------------------------------------- Smart Contrast AI
def srgb_to_linear(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(r: float, g: float, b: float) -> float:
    return 0.2126 * srgb_to_linear(r) + 0.7152 * srgb_to_linear(g) + 0.0722 * srgb_to_linear(b)


def contrast_ratio(l1: float, l2: float) -> float:
    a, b = max(l1, l2), min(l1, l2)
    return (a + 0.05) / (b + 0.05)


@dataclass
class ContrastDecision:
    fill_color: str
    stroke_color: str
    stroke_width: int
    backing: bool
    backing_opacity: float
    ratio: float
    bg_luminance: float


class SmartContrastAI:
    """Keeps subtitles readable over any background (WCAG 2.1, EMA-smoothed)."""

    def __init__(self, target_ratio: float = 4.5, ema: float = 0.25) -> None:
        self.target_ratio = target_ratio
        self.ema = ema
        self._lum: float | None = None
        self._std: float = 0.0

    def reset(self) -> None:
        self._lum = None
        self._std = 0.0

    def update_background(self, crop_rgb: np.ndarray) -> tuple[float, float]:
        """``crop_rgb``: HxWx3 uint8 crop under the subtitle box (downscale to ~64 px first)."""
        if crop_rgb.size == 0:
            return (self._lum or 0.5), self._std
        a = crop_rgb.reshape(-1, 3).astype(np.float32)
        lin = np.where(a / 255.0 <= 0.03928, (a / 255.0) / 12.92,
                       ((a / 255.0 + 0.055) / 1.055) ** 2.4)
        lum = 0.2126 * lin[:, 0] + 0.7152 * lin[:, 1] + 0.0722 * lin[:, 2]
        med = float(np.median(lum))
        std = float(np.std(lum))
        self._lum = med if self._lum is None else (1 - self.ema) * self._lum + self.ema * med
        self._std = std if self._std == 0.0 else (1 - self.ema) * self._std + self.ema * std
        return self._lum, self._std

    def decide(self, style: Style) -> ContrastDecision:
        if self._lum is None:
            # No background sampled yet (audio-only project, export of an alpha layer,
            # first frame): trust the designed style instead of guessing.
            c0 = QColor(style.fill_color)
            return ContrastDecision(style.fill_color, style.stroke_color, style.stroke_width,
                                    style.backing_enabled, style.backing_opacity,
                                    contrast_ratio(relative_luminance(c0.red(), c0.green(),
                                                                      c0.blue()), 0.0),
                                    0.0)
        bg = self._lum
        c = QColor(style.fill_color)
        fg = relative_luminance(c.red(), c.green(), c.blue())
        ratio = contrast_ratio(fg, bg)
        fill = style.fill_color
        stroke = style.stroke_color
        width = style.stroke_width
        backing = style.backing_enabled
        if style.smart_contrast and ratio < self.target_ratio:
            white = contrast_ratio(1.0, bg)
            black = contrast_ratio(0.0, bg)
            fill = "#FFFFFF" if white >= black else "#000000"
            stroke = "#000000" if fill == "#FFFFFF" else "#FFFFFF"
            width = max(width, 6)
            ratio = max(white, black)
        if style.smart_contrast and self._std > 0.12:   # busy background
            width = max(width, 8)
            backing = backing or self._std > 0.2
        return ContrastDecision(fill_color=fill, stroke_color=stroke, stroke_width=width,
                                backing=backing,
                                backing_opacity=style.backing_opacity,
                                ratio=ratio, bg_luminance=bg)


@lru_cache(maxsize=8)
def safe_zone_rect(zone: str, width: int, height: int) -> QRectF:
    """Safe area for the given target aspect: 90% wide, action-safe vertical margins."""
    margins = {"9:16": (0.06, 0.12), "16:9": (0.05, 0.08), "1:1": (0.06, 0.08), "4:3": (0.05, 0.08)}
    mx, my = margins.get(zone, (0.06, 0.1))
    return QRectF(width * mx, height * my, width * (1 - 2 * mx), height * (1 - 2 * my))

"""Layout Engine — статичная раскладка фразы + чисто визуальные трансформации слов.

КЛЮЧЕВОЕ ПРАВИЛО (чинит «рваный стиль» и прыгающий текст):
    Координаты считаются РОВНО ОДИН РАЗ на фразу и кэшируются.
    Анимация НИКОГДА не меняет layout — только визуальные трансформы
    (scale, opacity, offset, цвет, поворот) вокруг фиксированного пивота слова.

Структуры:
    WordLayoutInfo  — x, y, width, height, baseline, pivot, номер строки
    PhraseLayout    — строки, общий bbox, карточка-подложка, стиль
    apply_word_transform(info, progress, style) → WordTransform
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.models import Phrase, Style, Word
from app.render.animation_physics import (SpringParams, clamp, ease_out_cubic,
                                          ease_in_out_cubic, flash_decay, glitch_pulse,
                                          karaoke_progress, kick_pulse, lerp_color,
                                          pulse_wave, smoothstep, spring_normalized,
                                          spring_scale, stable_jitter, typewriter_state,
                                          wave_offset)

__all__ = ["WordLayoutInfo", "LineLayout", "PhraseLayout", "WordTransform",
           "LayoutEngine", "apply_word_transform", "spring_params_of"]

LEAD_IN = 0.12          # фраза появляется чуть раньше первого слова
TAIL = 0.28             # и немного задерживается после последнего


# ============================================================ СТРУКТУРЫ
@dataclass
class WordLayoutInfo:
    """Неизменяемая геометрия слова внутри фразы (в координатах композиции)."""

    word: Word
    index: int                       # порядковый номер внутри фразы
    line: int
    x: float                         # левый край текста
    y: float                         # верх строки
    width: float                     # advance
    height: float
    baseline: float
    texture: object = None           # WordTexture (заливка обычного состояния)
    texture_active: object = None    # WordTexture (заливка активного/караоке-состояния)

    @property
    def pivot(self) -> tuple[float, float]:
        """Фиксированная точка вращения/масштабирования — центр слова."""
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)


@dataclass
class LineLayout:
    index: int
    words: list[WordLayoutInfo] = field(default_factory=list)
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass
class PhraseLayout:
    phrase_index: int
    style: Style
    lines: list[LineLayout] = field(default_factory=list)
    words: list[WordLayoutInfo] = field(default_factory=list)
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # x, y, w, h (с паддингом)
    start: float = 0.0
    end: float = 0.0

    def visible_at(self, t: float) -> bool:
        return (self.start - LEAD_IN) <= t <= (self.end + TAIL)

    def phrase_alpha(self, t: float) -> float:
        if t < self.start - LEAD_IN or t > self.end + TAIL:
            return 0.0
        if t > self.end:
            return 1.0 - smoothstep((t - self.end) / TAIL)
        return 1.0


@dataclass
class WordTransform:
    """Только визуальное состояние. Геометрия не трогается никогда."""

    visible: bool = True
    scale: float = 1.0
    opacity: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    rotation: float = 0.0
    active: bool = False
    progress: float = 0.0                    # прогресс караоке внутри слова
    fill_override: Optional[str] = None      # цвет для плавного перехода
    chroma_shift: float = 0.0                # RGB-сплит (Retro VHS / glitch)
    chars_visible: int = -1                  # typewriter: -1 = всё слово
    cursor: bool = False
    glow: float = 0.0                        # доп. аддитивное свечение [0..1]


def spring_params_of(style: Style) -> SpringParams:
    return SpringParams(stiffness_k=style.spring.k, mass_m=style.spring.m,
                        damping_c=style.spring.c)


# ============================================== ТРАНСФОРМАЦИЯ СЛОВА
def apply_word_transform(info: WordLayoutInfo, t: float, style: Style,
                         phrase_start: float = 0.0) -> WordTransform:
    """Визуальное состояние слова в момент `t`. Чистая функция, без состояния.

    `progress` — непрерывный караоке-прогресс, `scale`/`offset` считаются
    относительно фиксированного пивота (`info.pivot`), поэтому текст не дрожит.
    """
    w = info.word
    mode = style.animation
    tr = WordTransform()
    tr.progress = karaoke_progress(t, w.start, w.end, softness=0.35)
    tr.active = w.start <= t <= w.end

    if mode == "static":
        tr.visible = t >= phrase_start - LEAD_IN
        tr.opacity = 1.0 if tr.visible else 0.0
        tr.scale = 1.0 + (style.active_scale - 1.0 if tr.active else 0.0)
        tr.fill_override = _active_fill(style, tr)
        return tr

    if mode == "fade":
        # мягкое появление всей фразы + лёгкий подъём (ease-out cubic)
        dt = t - phrase_start
        e = ease_out_cubic(clamp(dt / 0.42))
        tr.opacity = e
        tr.offset_y = (1.0 - e) * style.slide_up_px
        tr.scale = 1.0 + (style.active_scale - 1.0) * (1.0 if tr.active else 0.0)
        tr.fill_override = _active_fill(style, tr)
        tr.visible = dt > -LEAD_IN
        return tr

    if mode == "typewriter":
        n = len(w.text)
        shown, cursor = typewriter_state(t, w.start, w.end, n)
        tr.chars_visible = n if t > w.end else shown
        tr.cursor = cursor and tr.active
        tr.opacity = 1.0 if t >= w.start else 0.0
        tr.visible = t >= w.start
        tr.fill_override = _active_fill(style, tr)
        return tr

    if mode in ("pop_in", "slide_up", "glow_pulse", "fade_zoom", "wave_bounce",
                "glitch_flash"):
        _extended_transform(tr, info, t, style, phrase_start, mode)
        tr.scale += kick_pulse(t, w.kick, style.kick_pulse_amp, style.kick_pulse_tau)
        if style.rotation_jitter:
            tr.rotation += stable_jitter(w.text, style.rotation_jitter)
        tr.fill_override = _active_fill(style, tr)
        return tr

    # --- bounce / karaoke: пружина по появлению слова, пивот фиксирован ---
    dt = t - w.start
    params = spring_params_of(style)
    if mode == "bounce":
        base = spring_scale(dt, params, target=1.0, start=0.45)
        norm = spring_normalized(dt, params)
        tr.scale = base
        tr.offset_y = (1.0 - norm) * style.slide_up_px
        tr.rotation = (1.0 - norm) * -3.0
        tr.opacity = ease_out_cubic(clamp(dt / 0.10))
        tr.visible = dt > -1e-6
    else:  # karaoke — фраза стоит на месте, активное слово подсвечивается и растёт
        appear = t - phrase_start
        tr.opacity = ease_out_cubic(clamp(appear / 0.22))
        tr.offset_y = (1.0 - ease_out_cubic(clamp(appear / 0.3))) * style.slide_up_px * 0.35
        grow = style.active_scale - 1.0
        if grow:
            # плавный рост/спад активного слова, без скачков на границе
            up = smoothstep(clamp((t - w.start) / 0.12))
            down = 1.0 - smoothstep(clamp((t - w.end) / 0.18))
            tr.scale = 1.0 + grow * clamp(up * down)
        tr.visible = appear > -LEAD_IN

    # общие модификаторы
    tr.scale += kick_pulse(t, w.kick, style.kick_pulse_amp, style.kick_pulse_tau)
    if style.rotation_jitter:
        tr.rotation += stable_jitter(w.text, style.rotation_jitter) * \
            (1.0 if tr.active else 0.35)
    if style.chromatic_shift:
        tr.chroma_shift = style.chromatic_shift * (
            1.0 + 0.6 * abs(glitch_pulse(t, w.kick, 1.0)) if w.kick >= 0 else 1.0)
    if t > w.end:                          # аккуратное затухание хвоста слова
        tr.opacity *= 1.0 - smoothstep((t - w.end) / 0.9) * 0.0   # фраза гаснет целиком
    tr.fill_override = _active_fill(style, tr)
    return tr


def _extended_transform(tr: WordTransform, info: WordLayoutInfo, t: float, style: Style,
                        phrase_start: float, mode: str) -> None:
    """Шесть динамических режимов (Pop-In, Slide Up, Glow Pulse, Fade&Zoom,
    Wave Bounce, Glitch Flash). Все они меняют ТОЛЬКО трансформы."""
    w = info.word
    dt = t - w.start
    appear = t - phrase_start
    grow = max(0.0, style.active_scale - 1.0)
    params = spring_params_of(style)

    if mode == "pop_in":
        # резкое всплытие: слово стартует крупнее и ужимается до нормы
        e = ease_out_cubic(clamp(dt / 0.18))
        tr.scale = 1.35 - 0.35 * e
        tr.opacity = clamp(dt / 0.08)
        tr.visible = dt > -1e-6
    elif mode == "slide_up":
        # плавный вылет каждого слова снизу вверх
        e = ease_out_cubic(clamp(dt / 0.34))
        tr.offset_y = (1.0 - e) * max(24.0, style.slide_up_px)
        tr.opacity = e
        tr.scale = 1.0 + grow * smoothstep(clamp((t - w.start) / 0.12)) * \
            (1.0 - smoothstep(clamp((t - w.end) / 0.2)))
        tr.visible = dt > -1e-6
    elif mode == "glow_pulse":
        # пульсация неонового свечения на активном слове, геометрия стабильна
        tr.opacity = ease_out_cubic(clamp(appear / 0.25))
        tr.scale = 1.0 + grow * 0.5 * (1.0 if tr.active else 0.0)
        tr.glow = pulse_wave(t, hz=2.4, lo=0.25, hi=1.0) if tr.active else 0.0
        tr.visible = appear > -0.12
    elif mode == "fade_zoom":
        # мягкое проявление всей фразы с лёгким наездом
        e = ease_in_out_cubic(clamp(appear / 0.55))
        tr.opacity = e
        tr.scale = 0.92 + 0.08 * e + grow * 0.4 * (1.0 if tr.active else 0.0)
        tr.visible = appear > -0.12
    elif mode == "wave_bounce":
        # волнообразное покачивание: сдвиг фазы по индексу слова
        tr.opacity = ease_out_cubic(clamp(appear / 0.22))
        amp = max(6.0, style.slide_up_px * 0.25)
        tr.offset_y = wave_offset(t, info.index, amplitude=amp)
        tr.rotation = wave_offset(t, info.index, amplitude=2.2, phase_step=0.7)
        tr.scale = spring_scale(dt, params, start=0.75) if dt < 0.6 else 1.0
        tr.scale += grow * (1.0 if tr.active else 0.0)
        tr.visible = appear > -0.12
    else:  # glitch_flash — цифровой глитч в момент появления фразы
        flash = flash_decay(t, phrase_start, 0.26)
        tr.opacity = clamp(appear / 0.05) * (0.55 + 0.45 * (1.0 - flash))
        tr.offset_x = glitch_pulse(t, phrase_start, 14.0 * flash)
        tr.chroma_shift = max(style.chromatic_shift, 10.0 * flash)
        tr.scale = 1.0 + 0.06 * flash + grow * (1.0 if tr.active else 0.0)
        tr.visible = appear > -0.02


def _active_fill(style: Style, tr: WordTransform) -> Optional[str]:
    """Плавный цветовой переход обычное → активное состояние (без «щелчка»)."""
    if style.animation == "static" and not tr.active:
        return None
    if style.active_fill_color and style.active_fill_color != style.fill_color:
        if tr.active:
            return lerp_color(style.fill_color, style.active_fill_color,
                              smoothstep(clamp(tr.progress * 3.0)))
        return None
    return None


# ================================================================ ДВИЖОК
class LayoutEngine:
    """Считает статическую раскладку фраз и кэширует её до смены стиля/размера."""

    def __init__(self, cache) -> None:
        self.cache = cache                       # TextureCache
        self._layouts: dict[int, PhraseLayout] = {}
        self._signature: tuple | None = None

    def invalidate(self) -> None:
        self._layouts.clear()
        self._signature = None

    def signature(self, style: Style, width: int, height: int) -> tuple:
        from app.render.effects_contrast import TextureCache
        return (TextureCache.fingerprint(style), style.alignment, style.y_offset_pct,
                style.x_offset_pct, style.max_width_pct, style.line_spacing,
                style.safe_zone, style.card_enabled, style.card_padding,
                style.words_per_block, width, height)

    def layout(self, phrase: Phrase, index: int, style: Style, width: int, height: int,
               safe_rect) -> PhraseLayout:
        """Получить (и при необходимости посчитать) раскладку фразы."""
        sig = self.signature(style, width, height)
        if sig != self._signature:
            self._layouts.clear()
            self._signature = sig
        cached = self._layouts.get(index)
        if cached is not None:
            return cached
        layout = self._build(phrase, index, style, width, height, safe_rect)
        self._layouts[index] = layout
        return layout

    # ------------------------------------------------------------ расчёт
    def _build(self, phrase: Phrase, index: int, style: Style, width: int, height: int,
               safe_rect) -> PhraseLayout:
        max_w = min(safe_rect.width(), width * style.max_width_pct)
        space = max(8.0, style.font_size * 0.38)
        line_h = style.font_size * style.line_spacing

        # 1) переносим слова по строкам, измеряя текстуры ОДИН раз
        raw_lines: list[list[WordLayoutInfo]] = [[]]
        cur_w = 0.0
        for i, word in enumerate(phrase.words):
            fill = style.keyword_color if word.is_keyword else style.fill_color
            tex = self.cache.get(word.text, style, fill)
            active_color = (style.active_fill_color if style.active_fill_color
                            else style.karaoke_color)
            tex_active = self.cache.get(word.text, style, active_color)
            adv = tex.advance
            if cur_w > 0 and cur_w + space + adv > max_w:
                raw_lines.append([])
                cur_w = 0.0
            info = WordLayoutInfo(word=word, index=i, line=len(raw_lines) - 1,
                                  x=0.0, y=0.0, width=adv, height=float(tex.height),
                                  baseline=float(tex.ascent), texture=tex,
                                  texture_active=tex_active)
            raw_lines[-1].append(info)
            cur_w += (space if cur_w > 0 else 0.0) + adv

        # 2) фиксируем координаты: вертикальный центр блока привязан к y_offset_pct
        total_h = line_h * len(raw_lines)
        top = height * style.y_offset_pct - total_h / 2.0
        lines: list[LineLayout] = []
        all_words: list[WordLayoutInfo] = []
        for li, items in enumerate(raw_lines):
            line_w = sum(w.width for w in items) + space * max(0, len(items) - 1)
            if style.alignment == "left":
                x = safe_rect.left()
            elif style.alignment == "right":
                x = safe_rect.right() - line_w
            else:
                x = width * style.x_offset_pct - line_w / 2.0
            x = max(safe_rect.left(), min(x, safe_rect.right() - line_w))
            y = top + li * line_h
            line = LineLayout(index=li, x=x, y=y, width=line_w, height=line_h)
            cursor = x
            for info in items:
                info.x = cursor
                info.y = y
                info.line = li
                cursor += info.width + space
                line.words.append(info)
                all_words.append(info)
            lines.append(line)

        # 3) градиентная заливка: цвет слова = точка градиента по его позиции
        if style.gradient_enabled and style.gradient_stops and all_words:
            from app.render.animation_physics import gradient_color
            left_x = min(w.x for w in all_words)
            right_x = max(w.x + w.width for w in all_words)
            span = max(1.0, right_x - left_x)
            for info in all_words:
                u = ((info.x + info.width / 2.0) - left_x) / span
                info.texture = self.cache.get(info.word.text, style,
                                              gradient_color(style.gradient_stops, u))

        pad = float(style.card_padding if style.card_enabled else 8)
        if all_words:
            left = min(w.x for w in all_words) - pad
            right = max(w.x + w.width for w in all_words) + pad
        else:
            left, right = safe_rect.left(), safe_rect.right()
        box = (left, top - pad * 0.6, right - left, total_h + pad * 1.2)

        return PhraseLayout(phrase_index=index, style=style, lines=lines, words=all_words,
                            box=box, start=phrase.start, end=phrase.end)

"""Пресеты в духе CapCut: стиль текста, анимация, позиция, размер.

Каталог стилей (15 шт.) живёт в `app.render.presets_catalog`; здесь он
ре-экспортируется ради обратной совместимости с UI и тестами.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import SpringConfig, Style
from app.render.presets_catalog import (PRESET_BY_KEY, PRESET_CATALOG, StylePreset,
                                        preset_categories)

STYLE_PRESETS: tuple[StylePreset, ...] = PRESET_CATALOG
STYLE_BY_KEY = {p.key: p for p in STYLE_PRESETS}


# ------------------------------------------------------------------ АНИМАЦИЯ
@dataclass(frozen=True)
class AnimationPreset:
    key: str
    title: str
    description: str
    values: dict[str, Any]

    def apply(self, style: Style) -> Style:
        data = style.model_dump()
        data.update({k: v for k, v in self.values.items() if k != "spring"})
        if "spring" in self.values:
            data["spring"] = dict(self.values["spring"])
        return Style.model_validate(data)


ANIMATION_PRESETS: tuple[AnimationPreset, ...] = (
    AnimationPreset(
        "bounce", "Появление (Bounce)", "Слово впрыгивает с упругим отскоком",
        dict(spring=SpringConfig(k=320.0, m=1.0, c=16.0).model_dump(),
             pop_in_per_word=True, karaoke=False, kick_pulse_amp=0.12,
             animation="bounce", active_scale=1.16, slide_up_px=48.0),
    ),
    AnimationPreset(
        "karaoke", "Караоке (Pop-in)", "Слова подсвечиваются по мере произнесения",
        dict(spring=SpringConfig(k=220.0, m=1.0, c=20.0).model_dump(),
             pop_in_per_word=True, karaoke=True, kick_pulse_amp=0.06,
             animation="karaoke", active_scale=1.12, slide_up_px=28.0),
    ),
    AnimationPreset(
        "fade", "Плавный (Fade)", "Мягкое появление без прыжков",
        dict(spring=SpringConfig(k=90.0, m=1.0, c=19.0).model_dump(),
             pop_in_per_word=True, karaoke=True, kick_pulse_amp=0.0,
             animation="fade", active_scale=1.0, slide_up_px=40.0),
    ),
    AnimationPreset(
        "static", "Static", "Без анимации — просто текст",
        dict(spring=SpringConfig(k=400.0, m=1.0, c=40.0).model_dump(),
             pop_in_per_word=False, karaoke=False, kick_pulse_amp=0.0,
             animation="static", active_scale=1.0, slide_up_px=0.0),
    ),
    AnimationPreset(
        "pop_in", "Pop-In Scale", "Резкое всплытие: слово ужимается до нормы",
        dict(spring=SpringConfig(k=320.0, m=1.0, c=22.0).model_dump(),
             pop_in_per_word=True, karaoke=True, kick_pulse_amp=0.08,
             animation="pop_in", active_scale=1.12, slide_up_px=0.0),
    ),
    AnimationPreset(
        "slide_up", "Slide Up Word", "Каждое слово плавно вылетает снизу вверх",
        dict(spring=SpringConfig(k=200.0, m=1.0, c=24.0).model_dump(),
             pop_in_per_word=True, karaoke=True, kick_pulse_amp=0.05,
             animation="slide_up", active_scale=1.10, slide_up_px=60.0),
    ),
    AnimationPreset(
        "glow_pulse", "Glow Pulse", "Пульсация неонового свечения на активном слове",
        dict(spring=SpringConfig(k=180.0, m=1.0, c=26.0).model_dump(),
             pop_in_per_word=False, karaoke=True, kick_pulse_amp=0.04,
             animation="glow_pulse", active_scale=1.08, slide_up_px=0.0),
    ),
    AnimationPreset(
        "fade_zoom", "Fade & Zoom", "Мягкое проявление с лёгким наездом кадра",
        dict(spring=SpringConfig(k=120.0, m=1.0, c=24.0).model_dump(),
             pop_in_per_word=False, karaoke=True, kick_pulse_amp=0.0,
             animation="fade_zoom", active_scale=1.08, slide_up_px=0.0),
    ),
    AnimationPreset(
        "wave_bounce", "Wave Bounce", "Волнообразное покачивание слов по фразе",
        dict(spring=SpringConfig(k=240.0, m=1.0, c=18.0).model_dump(),
             pop_in_per_word=True, karaoke=True, kick_pulse_amp=0.08,
             animation="wave_bounce", active_scale=1.12, slide_up_px=40.0),
    ),
    AnimationPreset(
        "glitch_flash", "Glitch Flash", "Короткий цифровой глитч при смене фразы",
        dict(spring=SpringConfig(k=300.0, m=1.0, c=20.0).model_dump(),
             pop_in_per_word=False, karaoke=True, kick_pulse_amp=0.1,
             animation="glitch_flash", active_scale=1.1, slide_up_px=0.0),
    ),
    AnimationPreset(
        "typewriter", "Печатная машинка", "Буквы появляются по одной с курсором",
        dict(spring=SpringConfig(k=300.0, m=1.0, c=30.0).model_dump(),
             pop_in_per_word=False, karaoke=False, kick_pulse_amp=0.0,
             animation="typewriter", active_scale=1.0, slide_up_px=0.0),
    ),
)

ANIMATION_BY_KEY = {p.key: p for p in ANIMATION_PRESETS}


# ------------------------------------------------------------------ ПОЗИЦИЯ
POSITION_PRESETS: dict[str, float] = {
    "top": 0.18,
    "center": 0.50,
    "bottom": 0.78,
}
POSITION_TITLES: dict[str, str] = {"top": "Сверху", "center": "По центру", "bottom": "Снизу"}


def position_key_for(y: float) -> str:
    """Определить, какая кнопка позиции должна быть подсвечена."""
    return min(POSITION_PRESETS, key=lambda k: abs(POSITION_PRESETS[k] - y))


def apply_position(style: Style, key: str, offset: float = 0.0) -> Style:
    """Позиция = пресет + аккуратное смещение (−0.15…+0.15 высоты кадра)."""
    base = POSITION_PRESETS.get(key, 0.78)
    y = min(0.97, max(0.03, base + offset))
    return style.model_copy(update={"y_offset_pct": y})


# ------------------------------------------------------- РАЗМЕР / ЧИТАЕМОСТЬ
SIZE_PRESETS: dict[str, int] = {"S": 62, "M": 80, "L": 100, "XL": 124}


def size_key_for(px: int) -> str:
    return min(SIZE_PRESETS, key=lambda k: abs(SIZE_PRESETS[k] - px))

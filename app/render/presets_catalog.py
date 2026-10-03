"""Каталог из 15 готовых стилей субтитров (CapCut / Submagic / Opus-класс).

Каждый пресет — полный набор параметров `Style`: шрифт, цвета, обводки,
карточка-подложка, физика активного слова и режим анимации.
`style_presets.STYLE_PRESETS` является ре-экспортом этого каталога.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import Style

__all__ = ["StylePreset", "PRESET_CATALOG", "PRESET_BY_KEY", "preset_categories"]


@dataclass(frozen=True)
class StylePreset:
    key: str
    title: str
    subtitle: str
    sample: str
    values: dict[str, Any]
    category: str = "Популярные"

    def apply(self, style: Style) -> Style:
        data = style.model_dump()
        data.update(self.values)
        return Style.model_validate(data)

    # ----- цвета для миниатюры-превью в сайдбаре
    @property
    def preview_fill(self) -> str:
        return str(self.values.get("fill_color", "#FFFFFF"))

    @property
    def preview_stroke(self) -> str:
        return str(self.values.get("stroke_color", "#000000"))

    @property
    def preview_glow(self) -> str:
        return str(self.values.get("glow_color", "#000000"))

    @property
    def preview_accent(self) -> str:
        return str(self.values.get("karaoke_color",
                                   self.values.get("keyword_color", "#FFD600")))

    @property
    def preview_bg(self) -> str:
        if self.values.get("card_enabled"):
            return str(self.values.get("card_color", "#000000A6"))[:7]
        return "#12141A"


_BASE_SPRING = dict(spring={"k": 220.0, "m": 1.0, "c": 20.0})

# Каждый пресет ЯВНО выключает плашку активного слова и приглушение —
# иначе переключение с «Submagic Pop» на «Glassmorphism» тащило бы за собой
# красный бокс под активным словом. Пресет = полный, самодостаточный набор.
_NO_WORD_BOX = dict(active_card_enabled=False, inactive_opacity=1.0)


PRESET_CATALOG: tuple[StylePreset, ...] = (
    StylePreset(
        "capcut_yellow", "CapCut Yellow", "Жёлтый акцент, жирная обводка", "ЯРКО",
        dict(font_family="Montserrat", font_weight=900, uppercase=True, font_size=86,
             fill_color="#FFFFFF", keyword_color="#FFD600", karaoke_color="#FFD600",
             active_fill_color="#FFD600", active_scale=1.14,
             stroke_color="#000000", stroke_width=9, stroke2_width=0,
             glow_radius=0, glow_intensity=0.0,
             shadow_offset=(0, 5), shadow_blur=10, shadow_opacity=0.55,
             three_d_depth=0, card_enabled=False, animation="karaoke",
             smart_contrast=True, **_NO_WORD_BOX, **_BASE_SPRING), "Популярные"),
    StylePreset(
        "tiktok_glow", "TikTok Glow", "Белый текст с неоновым свечением", "Свет",
        dict(font_family="Poppins", font_weight=800, uppercase=False, font_size=80,
             fill_color="#FFFFFF", keyword_color="#8AF0FF", karaoke_color="#00E5FF",
             active_fill_color="#00E5FF", active_scale=1.10,
             stroke_color="#0A0F1A", stroke_width=6,
             glow_color="#7FE9FF", glow_radius=18, glow_intensity=1.0,
             shadow_offset=(0, 4), shadow_blur=12, shadow_opacity=0.5,
             animation="karaoke", smart_contrast=True, **_NO_WORD_BOX, **_BASE_SPRING), "Популярные"),
    StylePreset(
        "submagic_pop", "Submagic Pop", "Слово-в-слово с красной плашкой", "POP",
        dict(font_family="Inter", font_weight=900, uppercase=True, font_size=84,
             fill_color="#FFFFFF", keyword_color="#FFFFFF", karaoke_color="#FFFFFF",
             active_fill_color="#FFFFFF", active_scale=1.18,
             active_card_enabled=True, active_card_color="#FF2A2A",  # фирменный бокс
             inactive_opacity=1.0,
             stroke_color="#000000", stroke_width=7, glow_radius=0, glow_intensity=0.0,
             shadow_offset=(0, 6), shadow_blur=14, shadow_opacity=0.6,
             animation="bounce", words_per_block=3, **_BASE_SPRING), "Популярные"),
    StylePreset(
        "opus_impact", "Opus Impact", "Крупный ударный шрифт, двойная обводка", "IMPACT",
        dict(font_family="Anton", font_weight=900, uppercase=True, font_size=98,
             fill_color="#FFFFFF", keyword_color="#00FF85", karaoke_color="#00FF85",
             active_fill_color="#00FF85", active_scale=1.16,
             stroke_color="#000000", stroke_width=10,
             stroke2_color="#00FF85", stroke2_width=4,
             glow_radius=6, glow_color="#00FF85", glow_intensity=0.5,
             shadow_offset=(0, 8), shadow_blur=16, shadow_opacity=0.65,
             animation="bounce", words_per_block=2, **_NO_WORD_BOX, **_BASE_SPRING), "Популярные"),
    StylePreset(
        "descript_minimal", "Descript Minimal", "Чистый белый текст без эффектов", "Чисто",
        dict(font_family="Inter", font_weight=600, uppercase=False, font_size=66,
             fill_color="#FFFFFF", keyword_color="#FFFFFF", karaoke_color="#FFFFFF",
             active_fill_color="#FFFFFF", active_scale=1.0,
             stroke_color="#000000", stroke_width=3, glow_radius=0, glow_intensity=0.0,
             shadow_offset=(0, 2), shadow_blur=6, shadow_opacity=0.4,
             card_enabled=False, animation="fade", words_per_block=6,
             active_card_enabled=False, inactive_opacity=0.5,
             **_BASE_SPRING), "Минимализм"),
    StylePreset(
        "hormozi_gold", "Hormozi Gold", "Золото + чёрная плашка, подкаст-стиль", "ДЕНЬГИ",
        dict(font_family="Montserrat", font_weight=900, uppercase=True, font_size=90,
             fill_color="#FFFFFF", keyword_color="#FFC300", karaoke_color="#FFC300",
             active_fill_color="#FFC300", active_scale=1.2,
             stroke_color="#000000", stroke_width=8,
             card_enabled=True, card_color="#000000CC", card_radius=16, card_padding=18,
             shadow_offset=(0, 6), shadow_blur=12, shadow_opacity=0.6,
             animation="bounce", words_per_block=3, **_NO_WORD_BOX, **_BASE_SPRING), "Популярные"),
    StylePreset(
        "cyberpunk_neon", "Cyberpunk Neon", "Розово-циановый неон и RGB-сплит", "NEON",
        dict(font_family="Orbitron", font_weight=800, uppercase=True, font_size=82,
             fill_color="#F5F7FF", keyword_color="#FF2BD1", karaoke_color="#00F0FF",
             active_fill_color="#00F0FF", active_scale=1.12,
             stroke_color="#12002B", stroke_width=6,
             stroke2_color="#FF2BD1", stroke2_width=3,
             glow_color="#FF2BD1", glow_radius=22, glow_intensity=1.0,
             chromatic_shift=4, animation="karaoke", **_NO_WORD_BOX, **_BASE_SPRING), "Неон"),
    StylePreset(
        "modern_red_accent", "Modern Red Accent", "Белый текст, красное активное слово", "ФОКУС",
        dict(font_family="Inter", font_weight=800, uppercase=True, font_size=80,
             fill_color="#FFFFFF", keyword_color="#FF3B30", karaoke_color="#FF3B30",
             active_fill_color="#FF3B30", active_scale=1.15,
             stroke_color="#000000", stroke_width=7,
             shadow_offset=(0, 5), shadow_blur=10, shadow_opacity=0.5,
             animation="karaoke", words_per_block=4, **_NO_WORD_BOX, **_BASE_SPRING), "Популярные"),
    StylePreset(
        "glassmorphism", "Glassmorphism", "Матовое стекло под текстом", "Стекло",
        dict(font_family="Poppins", font_weight=700, uppercase=False, font_size=72,
             fill_color="#FFFFFF", keyword_color="#B9E4FF", karaoke_color="#B9E4FF",
             active_fill_color="#FFFFFF", active_scale=1.08,
             stroke_color="#0A0F1A", stroke_width=2,
             card_enabled=True, card_color="#FFFFFF2E", card_radius=26, card_padding=22,
             card_blur=True, card_border_color="#FFFFFF66", card_border_width=2,
             glow_color="#A7D8FF", glow_radius=10, glow_intensity=0.4,
             animation="fade", words_per_block=5, **_NO_WORD_BOX, **_BASE_SPRING), "Минимализм"),
    StylePreset(
        "retro_vhs", "Retro VHS", "Хроматическая аберрация и скан-линии", "VHS",
        dict(font_family="VT323", font_weight=700, uppercase=True, font_size=84,
             fill_color="#EDEDED", keyword_color="#FF4D4D", karaoke_color="#4DFFE0",
             active_fill_color="#4DFFE0", active_scale=1.1,
             stroke_color="#101010", stroke_width=5,
             chromatic_shift=8, scanlines=True, rotation_jitter=1.5,
             glow_color="#4DFFE0", glow_radius=8, glow_intensity=0.5,
             animation="karaoke", **_NO_WORD_BOX, **_BASE_SPRING), "Ретро"),
    StylePreset(
        "gradient_sunset", "Gradient Sunset", "Градиентная заливка текста", "ЗАКАТ",
        dict(font_family="Montserrat", font_weight=900, uppercase=True, font_size=88,
             fill_color="#FFD600", keyword_color="#FF6A00", karaoke_color="#FF007F",
             active_fill_color="#FF007F", active_scale=1.14,
             gradient_enabled=True,
             gradient_stops=["#FFD600", "#FF6A00", "#FF007F"],
             stroke_color="#1A0016", stroke_width=7,
             glow_color="#FF6A00", glow_radius=14, glow_intensity=0.7,
             animation="bounce", **_NO_WORD_BOX, **_BASE_SPRING), "Неон"),
    StylePreset(
        "minimal_dark_slate", "Minimal Dark Slate", "Тёмная плашка, спокойный текст", "Slate",
        dict(font_family="Inter", font_weight=600, uppercase=False, font_size=64,
             fill_color="#E8ECF2", keyword_color="#9BD1FF", karaoke_color="#9BD1FF",
             active_fill_color="#FFFFFF", active_scale=1.04,
             stroke_color="#000000", stroke_width=0,
             card_enabled=True, card_color="#14181FD9", card_radius=14, card_padding=16,
             shadow_offset=(0, 3), shadow_blur=8, shadow_opacity=0.45,
             animation="fade", words_per_block=6, **_NO_WORD_BOX, **_BASE_SPRING), "Минимализм"),
    StylePreset(
        "comic_bounce", "Comic Bounce", "Мультяшный прыжок с наклоном", "БУМ!",
        dict(font_family="Bangers", font_weight=900, uppercase=True, font_size=94,
             fill_color="#FFFFFF", keyword_color="#FFE14D", karaoke_color="#FFE14D",
             active_fill_color="#FFE14D", active_scale=1.25,
             stroke_color="#000000", stroke_width=11,
             stroke2_color="#FF3B30", stroke2_width=5,
             rotation_jitter=6.0, shadow_offset=(4, 8), shadow_blur=0, shadow_opacity=1.0,
             animation="bounce", words_per_block=2,
             spring={"k": 260.0, "m": 1.0, "c": 16.0}, **_NO_WORD_BOX), "Игровые"),
    StylePreset(
        "subtle_clean_bottom", "Subtle Clean Bottom", "Классические нижние субтитры", "Текст",
        dict(font_family="Inter", font_weight=500, uppercase=False, font_size=58,
             fill_color="#FFFFFF", keyword_color="#FFFFFF", karaoke_color="#FFFFFF",
             active_fill_color="#FFFFFF", active_scale=1.0,
             stroke_color="#000000", stroke_width=2,
             card_enabled=True, card_color="#000000A6", card_radius=8, card_padding=12,
             y_offset_pct=0.88, animation="static", words_per_block=8,
             **_NO_WORD_BOX, **_BASE_SPRING), "Минимализм"),
    StylePreset(
        "typewriter_mono", "Typewriter Mono", "Печатная машинка с курсором", "Печать_",
        dict(font_family="JetBrains Mono", font_weight=700, uppercase=False, font_size=62,
             fill_color="#D7FFB8", keyword_color="#FFFFFF", karaoke_color="#FFFFFF",
             active_fill_color="#FFFFFF", active_scale=1.0,
             stroke_color="#02150A", stroke_width=3,
             card_enabled=True, card_color="#02150ACC", card_radius=6, card_padding=14,
             glow_color="#7CFF4D", glow_radius=8, glow_intensity=0.45,
             animation="typewriter", words_per_block=5, **_NO_WORD_BOX, **_BASE_SPRING), "Ретро"),
)

PRESET_BY_KEY: dict[str, StylePreset] = {p.key: p for p in PRESET_CATALOG}


def preset_categories() -> dict[str, list[StylePreset]]:
    """Пресеты, сгруппированные по категориям — для вкладок сайдбара."""
    out: dict[str, list[StylePreset]] = {}
    for p in PRESET_CATALOG:
        out.setdefault(p.category, []).append(p)
    return out

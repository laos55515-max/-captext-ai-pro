"""AutoStyleEngine: genre -> ready-to-use kinetic subtitle Style."""
from __future__ import annotations

from app.core.models import AudioProfile, SpringConfig, Style

_PRESETS: dict[str, dict] = {
    # Hard, punchy, 1-2 words, neon, pulses on kick
    "rap": dict(
        font_family="Montserrat", font_size=110, font_weight=900, uppercase=True,
        fill_color="#FFFFFF", keyword_color="#FFE44D", karaoke_color="#FF2D95",
        stroke_color="#000000", stroke_width=10,
        glow_color="#FF2D95", glow_radius=18, glow_intensity=1.1,
        shadow_offset=(0, 6), shadow_blur=10, three_d_depth=6,
        words_per_block=2, max_chars_per_block=18,
        spring=SpringConfig(k=320.0, m=1.0, c=16.0),
        kick_pulse_amp=0.15, kick_pulse_tau=0.10,
        karaoke=False, pop_in_per_word=True, y_offset_pct=0.62,
    ),
    # Club / phonk: steady 4/4, wide tracking, strong bass pulse
    "deep_house_phonk": dict(
        font_family="Anton", font_size=96, font_weight=900, uppercase=True,
        fill_color="#E8FBFF", keyword_color="#7CFFCB", karaoke_color="#00E5FF",
        stroke_color="#06121A", stroke_width=8,
        glow_color="#00E5FF", glow_radius=26, glow_intensity=1.4,
        shadow_offset=(0, 4), shadow_blur=14, three_d_depth=0,
        words_per_block=2, max_chars_per_block=20,
        spring=SpringConfig(k=260.0, m=1.0, c=13.0),
        kick_pulse_amp=0.18, kick_pulse_tau=0.14,
        karaoke=True, pop_in_per_word=True, y_offset_pct=0.68,
    ),
    # Pop / dance: smooth wave, karaoke fill
    "pop_dance": dict(
        font_family="Poppins", font_size=88, font_weight=700, uppercase=False,
        fill_color="#FFFFFF", keyword_color="#FF8AD8", karaoke_color="#FFD166",
        stroke_color="#1A1030", stroke_width=6,
        glow_color="#FF8AD8", glow_radius=12, glow_intensity=0.9,
        shadow_offset=(0, 5), shadow_blur=10, three_d_depth=0,
        words_per_block=3, max_chars_per_block=26,
        spring=SpringConfig(k=90.0, m=1.0, c=9.0),
        kick_pulse_amp=0.08, kick_pulse_tau=0.16,
        karaoke=True, pop_in_per_word=True, y_offset_pct=0.74,
    ),
    # Chanson / acoustic: serif, calm, line-based
    "chanson_acoustic": dict(
        font_family="Playfair Display", font_size=76, font_weight=600, uppercase=False,
        fill_color="#FFF4E0", keyword_color="#F2B279", karaoke_color="#FFD9A0",
        stroke_color="#2B1B10", stroke_width=4,
        glow_color="#F2B279", glow_radius=6, glow_intensity=0.5,
        shadow_offset=(0, 3), shadow_blur=8, three_d_depth=0,
        words_per_block=5, max_chars_per_block=34,
        spring=SpringConfig(k=140.0, m=1.0, c=24.0),
        kick_pulse_amp=0.0, kick_pulse_tau=0.2,
        karaoke=True, pop_in_per_word=False, y_offset_pct=0.78,
    ),
    # Speech / podcast: readable blocks, keyword highlight
    "speech_podcast": dict(
        font_family="Inter", font_size=80, font_weight=800, uppercase=False,
        fill_color="#FFFFFF", keyword_color="#41E08A", karaoke_color="#41E08A",
        stroke_color="#000000", stroke_width=6,
        glow_color="#000000", glow_radius=0, glow_intensity=0.0,
        shadow_offset=(0, 4), shadow_blur=8, three_d_depth=0,
        words_per_block=4, max_chars_per_block=30,
        spring=SpringConfig(k=140.0, m=1.0, c=22.0),
        kick_pulse_amp=0.0, kick_pulse_tau=0.12,
        karaoke=True, pop_in_per_word=True, y_offset_pct=0.76,
    ),
}


class AutoStyleEngine:
    """Maps an :class:`AudioProfile` to a fully populated :class:`Style`."""

    @staticmethod
    def genres() -> list[str]:
        return list(_PRESETS)

    @staticmethod
    def get_style_for_genre(genre: str, safe_zone: str = "9:16") -> Style:
        data = dict(_PRESETS.get(genre, _PRESETS["speech_podcast"]))
        data["safe_zone"] = safe_zone
        return Style.model_validate(data)

    @staticmethod
    def get_style_for_profile(profile: AudioProfile | None, safe_zone: str = "9:16") -> Style:
        if profile is None:
            return AutoStyleEngine.get_style_for_genre("speech_podcast", safe_zone)
        style = AutoStyleEngine.get_style_for_genre(profile.genre, safe_zone)

        # --- adaptive tuning from measured signal properties ---
        if profile.bpm >= 150:
            style.spring.k *= 1.15
            style.kick_pulse_tau *= 0.85
        elif 0 < profile.bpm < 85:
            style.spring.k *= 0.9
            style.kick_pulse_tau *= 1.2

        if profile.words_per_sec > 4.0:          # very fast flow -> shorter blocks
            style.words_per_block = max(1, style.words_per_block - 1)
            style.max_chars_per_block = max(12, style.max_chars_per_block - 6)
        elif 0 < profile.words_per_sec < 2.0:
            style.words_per_block = min(6, style.words_per_block + 1)

        if profile.low_energy > 0.35:            # bass heavy -> stronger pulse
            style.kick_pulse_amp = min(0.25, style.kick_pulse_amp + 0.04)
        if profile.high_energy > 0.35:           # bright mix -> thicker stroke for legibility
            style.stroke_width = min(14, style.stroke_width + 2)
        return style

"""Pydantic v2 data models for CapText AI Pro."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = 3

Genre = Literal["rap", "deep_house_phonk", "pop_dance", "chanson_acoustic", "speech_podcast"]
SafeZone = Literal["9:16", "16:9", "1:1", "4:3"]
Alignment = Literal["center", "left", "right"]
AnimationMode = Literal[
    "bounce", "karaoke", "fade", "static", "typewriter",
    "pop_in", "slide_up", "glow_pulse", "fade_zoom", "wave_bounce", "glitch_flash",
]
FitMode = Literal["fit_blur", "fit", "fill"]

# Целевое разрешение композиции для каждого соотношения сторон
COMPOSITION_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
}

SAFE_ZONE_RATIOS: dict[str, float] = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:3": 4 / 3}


class Word(BaseModel):
    text: str
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    pos: str = "X"          # POS tag if available (spaCy), else "X"
    is_keyword: bool = False
    kick: float = -1.0      # nearest kick onset used for pulse, -1 = none

    @model_validator(mode="after")
    def _check_order(self) -> "Word":
        if self.end < self.start:
            object.__setattr__(self, "end", self.start + 0.06)
        return self

    @property
    def duration(self) -> float:
        return max(1e-6, self.end - self.start)


class SpringConfig(BaseModel):
    k: float = Field(default=180.0, gt=0.0)
    m: float = Field(default=1.0, gt=0.0)
    c: float = Field(default=14.0, ge=0.0)

    @property
    def omega(self) -> float:
        return (self.k / self.m) ** 0.5

    @property
    def zeta(self) -> float:
        return self.c / (2.0 * (self.k * self.m) ** 0.5)


class Style(BaseModel):
    font_family: str = "Montserrat"
    font_size: int = Field(default=84, gt=4)
    font_weight: int = Field(default=800, ge=100, le=1000)
    uppercase: bool = False

    fill_color: str = "#FFFFFF"
    keyword_color: str = "#FFE44D"
    karaoke_color: str = "#00E5FF"
    stroke_color: str = "#000000"
    stroke_width: int = Field(default=6, ge=0)
    glow_color: str = "#00E5FF"
    glow_radius: int = Field(default=0, ge=0)
    glow_intensity: float = Field(default=1.0, ge=0.0)
    shadow_offset: tuple[int, int] = (0, 4)
    shadow_blur: int = Field(default=8, ge=0)
    shadow_opacity: float = Field(default=0.6, ge=0.0, le=1.0)
    three_d_depth: int = Field(default=0, ge=0)
    three_d_color: str = "#101010"

    # --- активное слово (караоке / pop-in) ---
    animation: AnimationMode = "karaoke"
    active_scale: float = Field(default=1.0, ge=0.5, le=2.0)
    active_fill_color: str = "#FFFFFF"
    # Плашка под активным словом — ВЫКЛЮЧЕНА по умолчанию и никогда не красная
    # «по умолчанию»: цвет берётся отсюда и управляется из UI.
    active_card_enabled: bool = False
    active_card_color: str = "#000000A6"
    # Прозрачность НЕактивных слов (Descript Minimal: 0.5 → активное «выступает»)
    inactive_opacity: float = Field(default=1.0, ge=0.05, le=1.0)

    # --- карточка/плашка под текстом ---
    card_enabled: bool = False
    card_color: str = "#000000A6"
    card_radius: int = Field(default=12, ge=0)
    card_padding: int = Field(default=10, ge=0)
    card_blur: bool = False                 # Glassmorphism: размытие подложки
    card_border_color: str = "#FFFFFF33"
    card_border_width: int = Field(default=0, ge=0)

    # --- расширенные эффекты ---
    stroke2_color: str = "#00E5FF"          # внешняя (вторая) обводка
    stroke2_width: int = Field(default=0, ge=0)
    gradient_enabled: bool = False
    gradient_stops: list[str] = Field(default_factory=list)
    rotation_jitter: float = Field(default=0.0, ge=0.0, le=20.0)
    chromatic_shift: int = Field(default=0, ge=0, le=20)
    scanlines: bool = False
    slide_up_px: float = Field(default=40.0, ge=0.0)
    fit_mode: FitMode = "fit_blur"

    words_per_block: int = Field(default=3, ge=1, le=12)
    max_chars_per_block: int = Field(default=28, ge=4)
    spring: SpringConfig = SpringConfig()
    kick_pulse_amp: float = Field(default=0.10, ge=0.0, le=1.0)
    kick_pulse_tau: float = Field(default=0.12, gt=0.0)
    karaoke: bool = True
    pop_in_per_word: bool = True

    safe_zone: SafeZone = "9:16"
    alignment: Alignment = "center"
    y_offset_pct: float = Field(default=0.72, ge=0.0, le=1.0)
    x_offset_pct: float = Field(default=0.5, ge=0.0, le=1.0)
    max_width_pct: float = Field(default=0.86, gt=0.05, le=1.0)
    line_spacing: float = Field(default=1.12, gt=0.2)
    smart_contrast: bool = True
    backing_enabled: bool = False
    backing_color: str = "#000000"
    backing_opacity: float = Field(default=0.35, ge=0.0, le=1.0)

    @field_validator("fill_color", "stroke_color", "glow_color", "keyword_color",
                     "karaoke_color", "three_d_color", "backing_color",
                     "active_fill_color", "active_card_color", "card_color",
                     "card_border_color", "stroke2_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("#") or len(v) not in (7, 9):
            raise ValueError(f"color must be #RRGGBB or #RRGGBBAA, got {v!r}")
        int(v[1:], 16)
        return v.upper()

    @field_validator("gradient_stops")
    @classmethod
    def _stops(cls, v: list[str]) -> list[str]:
        out = []
        for c in v:
            c = c.strip().upper()
            if not c.startswith("#") or len(c) not in (7, 9):
                raise ValueError(f"gradient stop must be #RRGGBB(AA), got {c!r}")
            int(c[1:], 16)
            out.append(c)
        return out

    @property
    def accent_color(self) -> str:
        """Единый «Акцент» из UI: цвет ТЕКСТА активного слова."""
        return self.active_fill_color or self.karaoke_color or self.keyword_color

    def with_accent(self, color: str) -> "Style":
        """Акцент из UI — источник истины для активного слова и ключевых слов."""
        return self.model_copy(update={"active_fill_color": color,
                                       "karaoke_color": color,
                                       "keyword_color": color})

    @property
    def line_card(self) -> tuple[bool, str, int]:
        """Мастер-тумблер «Полупрозрачная подложка»: (вкл, цвет, радиус).

        Подложка рисуется под ВСЕЙ строкой субтитра, а не под отдельным словом.
        """
        if self.card_enabled:
            return True, self.card_color, self.card_radius
        if self.backing_enabled:
            alpha = int(round(max(0.0, min(1.0, self.backing_opacity)) * 255))
            return True, f"{self.backing_color[:7]}{alpha:02X}", 8
        return False, self.card_color, self.card_radius

    @property
    def composition_size(self) -> tuple[int, int]:
        """Целевое разрешение композиции для выбранного соотношения сторон."""
        return COMPOSITION_SIZES.get(self.safe_zone, (1080, 1920))

    @field_validator("shadow_offset", mode="before")
    @classmethod
    def _tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v


class Phrase(BaseModel):
    words: list[Word]
    style_override: Optional[dict[str, Any]] = None

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def resolved_style(self, base: Style) -> Style:
        if not self.style_override:
            return base
        data = base.model_dump()
        data.update(self.style_override)
        return Style.model_validate(data)


class AudioProfile(BaseModel):
    genre: Genre = "speech_podcast"
    probabilities: dict[str, float] = Field(default_factory=dict)
    bpm: float = 0.0
    onsets: list[float] = Field(default_factory=list)
    kick_events: list[float] = Field(default_factory=list)
    snare_events: list[float] = Field(default_factory=list)
    rms_energy: list[float] = Field(default_factory=list)
    rms_hop_seconds: float = 256 / 22050
    onset_density: float = 0.0
    spectral_centroid: float = 0.0
    zero_crossing_rate: float = 0.0
    low_energy: float = 0.0
    mid_energy: float = 0.0
    high_energy: float = 0.0
    beat_regularity: float = 0.0
    words_per_sec: float = 0.0
    duration: float = 0.0


class Project(BaseModel):
    schema_version: int = SCHEMA_VERSION
    video_path: str = ""
    duration: float = 0.0
    fps: float = 30.0
    width: int = 1080
    height: int = 1920
    language: str = "auto"
    audio_profile: Optional[AudioProfile] = None
    phrases: list[Phrase] = Field(default_factory=list)
    global_style: Style = Style()

    # ---- io ----
    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2),
                     encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw = migrate(raw)
        return cls.model_validate(raw)

    @property
    def words(self) -> list[Word]:
        return [w for ph in self.phrases for w in ph.words]


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Forward-migrate an older project dict to the current schema."""
    v = int(raw.get("schema_version", 1))
    if v < 3 and v >= 2:
        style = raw.get("global_style", {})
        style.setdefault("animation", "karaoke")
        style.setdefault("fit_mode", "fit_blur")
        raw["global_style"] = style
        v = 3
    if v < 2:
        style = raw.get("global_style", {})
        style.setdefault("max_chars_per_block", 28)
        style.setdefault("x_offset_pct", 0.5)
        style.setdefault("animation", "karaoke")
        style.setdefault("fit_mode", "fit_blur")
        raw["global_style"] = style
        v = 3
    raw["schema_version"] = SCHEMA_VERSION
    return raw

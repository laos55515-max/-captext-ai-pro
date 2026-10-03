"""AnimationPhysics — точная математика движения субтитров.

Все функции ЧИСТЫЕ и зависят только от времени: любой кадр вычисляется независимо,
поэтому перемотка, превью и экспорт дают идентичный результат, а анимация не «рвётся».

Состав:
  * ease_out_cubic / ease_in_out / smoothstep  — сглаживание fade и slide
  * spring_scale  — недодемпфированная пружина второго порядка (pop-in / bounce)
  * karaoke_progress + lerp_color / gradient_color — плавная караоке-подсветка
  * glitch_pulse / typewriter_state — служебные генераторы для стилевых пресетов
"""
from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "wave_offset", "pulse_wave", "flash_decay",
    "clamp", "smoothstep", "ease_out_cubic", "ease_in_cubic", "ease_in_out_cubic",
    "cubic_bezier_ease", "SpringParams", "spring_scale", "spring_normalized",
    "karaoke_progress", "lerp", "lerp_color", "gradient_color", "hex_to_rgb",
    "rgb_to_hex", "kick_pulse", "glitch_pulse", "typewriter_state", "stable_jitter",
]


# ------------------------------------------------------------------ базовые
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def smoothstep(x: float) -> float:
    x = clamp(x)
    return x * x * (3.0 - 2.0 * x)


def ease_out_cubic(t: float) -> float:
    """f(t) = 1 - (1 - t)^3 — основная кривая для fade и slide."""
    t = clamp(t)
    u = 1.0 - t
    return 1.0 - u * u * u


def ease_in_cubic(t: float) -> float:
    t = clamp(t)
    return t * t * t


def ease_in_out_cubic(t: float) -> float:
    t = clamp(t)
    if t < 0.5:
        return 4.0 * t * t * t
    u = -2.0 * t + 2.0
    return 1.0 - u * u * u / 2.0


def _bez_x(s: float, x1: float, x2: float) -> float:
    u = 1.0 - s
    return 3.0 * u * u * s * x1 + 3.0 * u * s * s * x2 + s ** 3


def _bez_dx(s: float, x1: float, x2: float) -> float:
    u = 1.0 - s
    return 3.0 * u * u * x1 + 6.0 * u * s * (x2 - x1) + 3.0 * s * s * (1.0 - x2)


def cubic_bezier_ease(t: float, x1: float = 0.25, y1: float = 0.1,
                      x2: float = 0.25, y2: float = 1.0) -> float:
    """CSS-подобная cubic-bezier: решаем x(s)=t методом Ньютона, возвращаем y(s)."""
    t = clamp(t)
    if t in (0.0, 1.0):
        return t
    s = t
    for _ in range(6):
        err = _bez_x(s, x1, x2) - t
        if abs(err) < 1e-6:
            break
        d = _bez_dx(s, x1, x2)
        if abs(d) < 1e-9:
            break
        s = clamp(s - err / d)
    u = 1.0 - s
    return 3.0 * u * u * s * y1 + 3.0 * u * s * s * y2 + s ** 3


# ------------------------------------------------------------------ пружина
@dataclass(frozen=True)
class SpringParams:
    """Параметры пружины второго порядка."""

    stiffness_k: float = 220.0
    mass_m: float = 1.0
    damping_c: float = 20.0
    overshoot: float = 1.0            # множитель амплитуды колебаний

    @property
    def omega_n(self) -> float:
        return math.sqrt(max(1e-9, self.stiffness_k / max(1e-9, self.mass_m)))

    @property
    def zeta(self) -> float:
        return self.damping_c / (2.0 * math.sqrt(max(1e-9, self.stiffness_k * self.mass_m)))

    @property
    def omega_d(self) -> float:
        z = self.zeta
        return self.omega_n * math.sqrt(max(0.0, 1.0 - z * z))


def spring_scale(t: float, params: SpringParams | None = None,
                 target: float = 1.0, start: float = 0.0) -> float:
    """S(t) = target + e^(−ζ·ωn·t)·(A·cos(ωd·t) + B·sin(ωd·t)).

    Начальные условия S(0)=start, S'(0)=0 ⇒ A = start − target, B = A·ζ·ωn/ωd.
    Поддержаны все три режима: ζ<1 (упругий отскок), ζ=1, ζ>1.
    """
    p = params or SpringParams()
    if t <= 0.0:
        return start
    wn, z = p.omega_n, p.zeta
    a = (start - target) * p.overshoot
    wt = wn * t
    if wt > 80.0:
        return target
    if z < 1.0 - 1e-9:                               # underdamped — упругий отскок
        wd = p.omega_d
        b = a * z * wn / max(1e-9, wd)
        return target + math.exp(-z * wt) * (a * math.cos(wd * t) + b * math.sin(wd * t))
    if abs(z - 1.0) <= 1e-9:                         # critically damped
        return target + math.exp(-wt) * (a + a * wn * t)
    s = math.sqrt(z * z - 1.0)                       # overdamped
    r1, r2 = -wn * (z - s), -wn * (z + s)
    c1 = a * r2 / (r2 - r1)
    c2 = -a * r1 / (r2 - r1)
    return target + c1 * math.exp(r1 * t) + c2 * math.exp(r2 * t)


def spring_normalized(t: float, params: SpringParams | None = None) -> float:
    """Нормированный отклик 0 → 1 (используется для сдвига/прозрачности)."""
    return spring_scale(t, params, target=1.0, start=0.0)


def kick_pulse(t: float, t_hit: float, amp: float = 0.10, tau: float = 0.12) -> float:
    """Экспоненциально затухающий импульс от удара бочки."""
    if t_hit < 0.0 or t < t_hit or amp <= 0.0:
        return 0.0
    return amp * math.exp(-(t - t_hit) / max(1e-6, tau))


def glitch_pulse(t: float, t_hit: float, amp: float = 1.0, tau: float = 0.09,
                 freq: float = 38.0) -> float:
    """Короткая затухающая «дрожь» для Cyberpunk-пресета (в пикселях сдвига)."""
    if t_hit < 0.0 or t < t_hit or amp <= 0.0:
        return 0.0
    dt = t - t_hit
    return amp * math.exp(-dt / max(1e-6, tau)) * math.sin(2.0 * math.pi * freq * dt)


def stable_jitter(seed_text: str, amplitude: float = 4.0) -> float:
    """Детерминированный «случайный» угол ±amplitude для Comic-пресета.

    Зависит только от текста слова, поэтому НЕ меняется от кадра к кадру
    (иначе получится дрожание, ровно та проблема, которую мы чиним).
    """
    h = 2166136261
    for ch in seed_text:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return ((h % 2001) / 1000.0 - 1.0) * amplitude


# ------------------------------------------------------------------ караоке
def karaoke_progress(t: float, t_start: float, t_end: float, softness: float = 0.0) -> float:
    """Непрерывный прогресс p ∈ [0,1] внутри слова.

    ``softness`` > 0 сглаживает края (smoothstep), чтобы заливка не «щёлкала».
    """
    dur = max(1e-6, t_end - t_start)
    p = clamp((t - t_start) / dur)
    if softness > 0.0:
        return p * (1.0 - softness) + smoothstep(p) * softness
    return p


def hex_to_rgb(color: str) -> tuple[int, int, int, int]:
    c = color.strip().lstrip("#")
    if len(c) == 6:
        c += "FF"
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(c[6:8], 16))


def rgb_to_hex(rgb: tuple[int, int, int, int] | tuple[int, int, int]) -> str:
    if len(rgb) == 3:
        return "#{:02X}{:02X}{:02X}".format(*rgb)
    r, g, b, a = rgb
    return "#{:02X}{:02X}{:02X}".format(r, g, b) if a >= 255 else \
        "#{:02X}{:02X}{:02X}{:02X}".format(r, g, b, a)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * clamp(t)


def lerp_color(c1: str, c2: str, t: float) -> str:
    """Линейная интерполяция цвета в sRGB (достаточно для подсветки)."""
    t = clamp(t)
    a, b = hex_to_rgb(c1), hex_to_rgb(c2)
    return rgb_to_hex(tuple(int(round(lerp(a[i], b[i], t))) for i in range(4)))  # type: ignore


def gradient_color(stops: list[str], t: float) -> str:
    """Многоточечный градиент: равномерные стопы, плавный переход по всей длине."""
    if not stops:
        return "#FFFFFF"
    if len(stops) == 1:
        return stops[0]
    t = clamp(t)
    span = 1.0 / (len(stops) - 1)
    idx = min(len(stops) - 2, int(t / span))
    local = (t - idx * span) / span
    return lerp_color(stops[idx], stops[idx + 1], local)


# --------------------------------------------------------------- typewriter
def typewriter_state(t: float, t_start: float, t_end: float, n_chars: int,
                     cursor_hz: float = 2.6) -> tuple[int, bool]:
    """(сколько символов уже напечатано, видим ли курсор) для Typewriter-пресета."""
    if n_chars <= 0:
        return 0, False
    p = karaoke_progress(t, t_start, t_end)
    shown = int(math.floor(p * n_chars + 1e-6))
    cursor = (math.sin(2.0 * math.pi * cursor_hz * max(0.0, t)) > 0.0) and shown < n_chars + 1
    return max(0, min(n_chars, shown)), cursor


# ------------------------------------------------- дополнительные осцилляторы
def wave_offset(t: float, index: int, amplitude: float = 10.0, hz: float = 1.6,
                phase_step: float = 0.55) -> float:
    """Волна по словам: соседние слова качаются со сдвигом фазы."""
    return amplitude * math.sin(2.0 * math.pi * hz * t + index * phase_step)


def pulse_wave(t: float, hz: float = 2.2, lo: float = 0.35, hi: float = 1.0) -> float:
    """Плавная пульсация в диапазоне [lo, hi] — для неонового свечения."""
    s = 0.5 + 0.5 * math.sin(2.0 * math.pi * hz * t)
    return lo + (hi - lo) * s


def flash_decay(t: float, t_hit: float, duration: float = 0.22) -> float:
    """Короткая вспышка 1→0 от момента t_hit (глитч при смене фразы)."""
    if t_hit < 0 or t < t_hit:
        return 0.0
    k = (t - t_hit) / max(1e-6, duration)
    return 0.0 if k >= 1.0 else (1.0 - k) ** 2

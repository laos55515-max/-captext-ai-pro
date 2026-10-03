"""Analytic spring physics + easing. Pure functions, fully stateless."""
from __future__ import annotations

import math
from typing import Any

from app.core.models import Style, Word

__all__ = ["spring", "cubic_bezier_ease", "clamp", "smoothstep", "kick_pulse", "get_word_transform"]


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def smoothstep(x: float) -> float:
    x = clamp(x)
    return x * x * (3.0 - 2.0 * x)


def spring(t: float, k: float = 180.0, m: float = 1.0, c: float = 14.0) -> float:
    """Analytic step response of  x'' + 2*z*w*x' + w^2*x = w^2  with x(0)=x'(0)=0.

    Returns displacement in [0, ~1.4]; 1.0 is the settled target.
    """
    if t <= 0.0:
        return 0.0
    if m <= 0.0 or k <= 0.0:
        return 1.0
    w = math.sqrt(k / m)
    z = c / (2.0 * math.sqrt(k * m))
    if z < 0.0:
        z = 0.0
    wt = w * t
    if wt > 80.0:                      # fully settled, avoid exp underflow noise
        return 1.0
    if z < 1.0 - 1e-6:                 # underdamped -> bounce
        wd = w * math.sqrt(1.0 - z * z)
        return 1.0 - math.exp(-z * wt) * (math.cos(wd * t) + (z * w / wd) * math.sin(wd * t))
    if abs(z - 1.0) <= 1e-6:           # critically damped
        return 1.0 - math.exp(-wt) * (1.0 + wt)
    s = math.sqrt(z * z - 1.0)         # overdamped
    r1 = -w * (z - s)
    r2 = -w * (z + s)
    return 1.0 - (r2 * math.exp(r1 * t) - r1 * math.exp(r2 * t)) / (r2 - r1)


def _bezier_x(s: float, x1: float, x2: float) -> float:
    u = 1.0 - s
    return 3.0 * u * u * s * x1 + 3.0 * u * s * s * x2 + s * s * s


def _bezier_dx(s: float, x1: float, x2: float) -> float:
    u = 1.0 - s
    return 3.0 * u * u * x1 + 6.0 * u * s * (x2 - x1) + 3.0 * s * s * (1.0 - x2)


def cubic_bezier_ease(t: float, x1: float = 0.25, y1: float = 0.1,
                      x2: float = 0.25, y2: float = 1.0) -> float:
    """CSS-style cubic-bezier(x1,y1,x2,y2): solve x(s)=t by Newton, return y(s)."""
    t = clamp(t)
    if t in (0.0, 1.0):
        return t
    s = t
    for _ in range(6):
        err = _bezier_x(s, x1, x2) - t
        if abs(err) < 1e-6:
            break
        d = _bezier_dx(s, x1, x2)
        if abs(d) < 1e-9:
            break
        s = clamp(s - err / d)
    u = 1.0 - s
    return 3.0 * u * u * s * y1 + 3.0 * u * s * s * y2 + s * s * s


def kick_pulse(t: float, t_hit: float, amp: float = 0.10, tau: float = 0.12) -> float:
    """Exponentially decaying pulse triggered at t_hit."""
    if t_hit < 0.0 or t < t_hit or amp <= 0.0:
        return 0.0
    return amp * math.exp(-(t - t_hit) / max(1e-6, tau))


def get_word_transform(t: float, word: Word, style: Style,
                       nearest_kick: float = -1.0) -> dict[str, Any]:
    """Stateless per-word animation state at absolute time ``t`` (seconds)."""
    dt_in = t - word.start
    sp = spring(dt_in, style.spring.k, style.spring.m, style.spring.c) if style.pop_in_per_word \
        else (1.0 if dt_in >= 0 else 0.0)

    scale = (0.6 + 0.4 * sp) if style.pop_in_per_word else 1.0
    scale += kick_pulse(t, nearest_kick, style.kick_pulse_amp, style.kick_pulse_tau)
    y_offset = (1.0 - sp) * 40.0

    fade_in = clamp(dt_in / 0.08)
    fade_out = 1.0
    tail = t - word.end
    if tail > 0.0:
        fade_out = 1.0 - smoothstep(tail / 0.22)
    opacity = clamp(fade_in * fade_out)

    if dt_in < 0.0:
        scale, opacity, y_offset = 0.6, 0.0, 40.0

    progress = clamp((t - word.start) / word.duration)
    active = word.start <= t <= word.end

    return {
        "scale": scale,
        "y_offset": y_offset,
        "opacity": opacity,
        "karaoke_progress": progress,
        "active": active,
        "spring": sp,
        "rotation": (1.0 - sp) * -4.0 if style.pop_in_per_word else 0.0,
    }

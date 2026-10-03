"""Universal multimodal audio classifier (5 genres) + AudioProfile builder."""
from __future__ import annotations

import math

import numpy as np

from app.audio import extract
from app.audio.rhythm import SR, analyze_rhythm
from app.core.models import AudioProfile, Style
from app.render.presets import AutoStyleEngine

GENRES = ["rap", "deep_house_phonk", "pop_dance", "chanson_acoustic", "speech_podcast"]


def _softmax(scores: dict[str, float], temp: float = 1.0) -> dict[str, float]:
    keys = list(scores)
    v = np.array([scores[k] for k in keys], dtype=np.float64) / max(1e-6, temp)
    v -= v.max()
    e = np.exp(v)
    p = e / e.sum()
    return {k: round(float(x), 4) for k, x in zip(keys, p)}


class UniversalAudioClassifier:
    """Feature-engineered classifier with a pluggable ML head.

    ``model`` may be any object exposing ``predict_proba(X) -> array``
    (e.g. a trained LightGBM/sklearn model); if it is None, the transparent
    rule/score system below is used.
    """

    def __init__(self, model: object | None = None, sr: int = SR) -> None:
        self.model = model
        self.sr = sr

    # ------------------------------------------------------------------ api
    def analyze(self, audio_path: str, words_per_sec: float = 0.0) -> AudioProfile:
        duration = extract.ffprobe_duration(audio_path)
        pcm = extract.decode_pcm(audio_path, sr=self.sr)
        if pcm.size == 0:
            raise ValueError(f"no decodable audio in {audio_path}")

        rh = analyze_rhythm(pcm, self.sr)
        rh.duration = rh.duration or duration

        feats = self.features(rh, words_per_sec)
        probs = self.predict(feats)
        genre = max(probs, key=probs.get)

        return AudioProfile(
            genre=genre,  # type: ignore[arg-type]
            probabilities=probs,
            bpm=round(rh.bpm, 2),
            onsets=rh.onsets,
            kick_events=rh.kick_events,
            snare_events=rh.snare_events,
            rms_energy=[round(v, 5) for v in rh.rms],
            rms_hop_seconds=rh.hop_seconds,
            onset_density=round(rh.onset_density, 3),
            spectral_centroid=round(rh.spectral_centroid, 2),
            zero_crossing_rate=round(rh.zero_crossing_rate, 4),
            low_energy=round(rh.low_energy, 4),
            mid_energy=round(rh.mid_energy, 4),
            high_energy=round(rh.high_energy, 4),
            beat_regularity=round(rh.beat_regularity, 4),
            words_per_sec=round(words_per_sec, 3),
            duration=round(duration, 3),
        )

    # ------------------------------------------------------------- features
    @staticmethod
    def features(rh, words_per_sec: float = 0.0) -> dict[str, float]:
        kick_rate = len(rh.kick_events) / max(1e-6, rh.duration)
        snare_rate = len(rh.snare_events) / max(1e-6, rh.duration)
        rms = np.asarray(rh.rms or [0.0])
        return {
            "bpm": rh.bpm,
            "onset_density": rh.onset_density,
            "kick_rate": kick_rate,
            "snare_rate": snare_rate,
            "beat_regularity": rh.beat_regularity,
            "low_energy": rh.low_energy,
            "mid_energy": rh.mid_energy,
            "high_energy": rh.high_energy,
            "spectral_centroid": rh.spectral_centroid,
            "zcr": rh.zero_crossing_rate,
            "flatness": rh.spectral_flatness,
            "rms_std": float(rms.std()),
            "rms_mean": float(rms.mean()),
            "silence_ratio": float((rms < max(1e-6, rms.mean() * 0.25)).mean()),
            "words_per_sec": words_per_sec,
        }

    # -------------------------------------------------------------- predict
    def predict(self, f: dict[str, float]) -> dict[str, float]:
        if self.model is not None and hasattr(self.model, "predict_proba"):
            X = np.array([[f[k] for k in sorted(f)]], dtype=np.float32)
            p = np.asarray(self.model.predict_proba(X))[0]  # type: ignore[attr-defined]
            return {g: round(float(x), 4) for g, x in zip(GENRES, p)}
        return self.rule_scores(f)

    @staticmethod
    def rule_scores(f: dict[str, float]) -> dict[str, float]:
        """Transparent, tunable scoring. Returns a probability distribution."""
        s = {g: 0.0 for g in GENRES}

        musicality = min(1.0, f["onset_density"] / 4.0) * 0.6 + f["beat_regularity"] * 0.4
        speechiness = f["silence_ratio"] * 1.2 + (1.0 - f["beat_regularity"]) * 0.8

        # 5. Speech / podcast: no rhythm section, many pauses, low bass
        s["speech_podcast"] = (
            2.6 * (1.0 - min(1.0, musicality))
            + 1.4 * f["silence_ratio"]
            + 1.2 * max(0.0, 0.18 - f["low_energy"]) * 5.0
            + (1.0 if f["bpm"] <= 1 else 0.0)
            - 2.0 * f["beat_regularity"]
        )

        # 1. Rap / boom-bap: dense onsets, strong kick+snare, fast syllables
        s["rap"] = (
            1.8 * min(1.0, f["onset_density"] / 5.0)
            + 1.6 * min(1.0, f["kick_rate"] / 2.0)
            + 1.0 * min(1.0, f["snare_rate"] / 2.0)
            + 2.2 * min(1.0, max(0.0, f["words_per_sec"] - 2.6) / 1.6)
            + 1.0 * min(1.0, f["low_energy"] / 0.35)
            - 1.5 * f["silence_ratio"]
        )

        # 2. Deep house / phonk / EDM: very regular 4/4, huge bass, fewer words
        four_four = 1.0 if 110 <= f["bpm"] <= 160 else (0.5 if 85 <= f["bpm"] <= 175 else 0.0)
        s["deep_house_phonk"] = (
            2.4 * f["beat_regularity"]
            + 1.6 * four_four
            + 1.8 * min(1.0, f["low_energy"] / 0.35)
            + 0.8 * min(1.0, f["kick_rate"] / 2.0)
            - 1.6 * min(1.0, max(0.0, f["words_per_sec"] - 2.0))
            - 1.2 * f["silence_ratio"]
        )

        # 3. Pop / dance: prominent vocal, moderate rhythm, balanced spectrum
        s["pop_dance"] = (
            1.6 * musicality
            + 1.4 * (1.0 - abs(f["mid_energy"] - 0.45) / 0.45)
            + 1.2 * min(1.0, max(0.0, 1.0 - abs(f["words_per_sec"] - 2.2) / 1.4))
            + 0.8 * (1.0 if 90 <= f["bpm"] <= 135 else 0.0)
            - 1.0 * abs(f["low_energy"] - 0.22) * 3.0
        )

        # 4. Chanson / acoustic: mid/high dominance, irregular rhythm, vocal focus
        s["chanson_acoustic"] = (
            2.0 * min(1.0, (f["mid_energy"] + f["high_energy"]) / 0.85)
            + 1.8 * (1.0 - f["beat_regularity"])
            + 1.2 * max(0.0, 0.18 - f["low_energy"]) * 5.0
            + 0.8 * (1.0 if 0 < f["bpm"] < 115 else 0.0)
            + 0.8 * min(1.0, max(0.0, 1.0 - abs(f["words_per_sec"] - 2.0) / 1.5))
            - 1.2 * f["silence_ratio"]
        )

        for k in s:
            if math.isnan(s[k]) or math.isinf(s[k]):
                s[k] = 0.0
        return _softmax(s, temp=0.9)

    # ----------------------------------------------------------- convenience
    @staticmethod
    def style_for(profile: AudioProfile, safe_zone: str = "9:16") -> Style:
        return AutoStyleEngine.get_style_for_profile(profile, safe_zone)

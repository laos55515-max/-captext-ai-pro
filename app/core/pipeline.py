"""End-to-end AI pipeline: analyze -> transcribe -> align -> segment -> auto style."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from app.audio.asr_align import SpeechProcessor
from app.audio.classify import UniversalAudioClassifier
from app.audio.extract import ffprobe_duration, probe_video
from app.audio.segment import PhraseSegmenter
from app.core.models import Project, Style
from app.render.presets import AutoStyleEngine

log = logging.getLogger("captext.pipeline")
ProgressCB = Optional[Callable[[float, str], None]]


@dataclass
class PipelineOptions:
    language: str = "auto"
    model_size: str = "large-v3-turbo"
    safe_zone: str = "9:16"
    forced_genre: str | None = None      # None = full auto
    snap_tolerance: float = 0.04
    apply_auto_style: bool = True


class AutoPipeline:
    """One call does everything the '🚀 ИИ-Автоматика' button promises."""

    def __init__(self, options: PipelineOptions | None = None,
                 speech: SpeechProcessor | None = None,
                 classifier: UniversalAudioClassifier | None = None,
                 segmenter: PhraseSegmenter | None = None) -> None:
        self.opt = options or PipelineOptions()
        self.classifier = classifier or UniversalAudioClassifier()
        self.speech = speech or SpeechProcessor(model_size=self.opt.model_size)
        self.segmenter = segmenter or PhraseSegmenter()

    def run(self, media_path: str, progress: ProgressCB = None) -> Project:
        def rep(p: float, m: str) -> None:
            if progress:
                progress(max(0.0, min(1.0, p)), m)

        rep(0.02, "Чтение медиафайла…")
        duration = ffprobe_duration(media_path)
        w, h, fps = probe_video(media_path)

        rep(0.08, "Анализ звука: BPM, онсеты, спектр…")
        profile = self.classifier.analyze(media_path)
        if self.opt.forced_genre:
            profile.genre = self.opt.forced_genre  # type: ignore[assignment]
        rep(0.28, f"Жанр: {profile.genre} ({profile.bpm:.0f} BPM)")

        rep(0.32, "Распознавание и выравнивание речи…")
        words = self.speech.transcribe_and_align(
            media_path, language=self.opt.language,
            onsets=profile.onsets, kicks=profile.kick_events,
            snap_tolerance=self.opt.snap_tolerance,
            progress=lambda p, m: rep(0.32 + 0.5 * p, m),
        )

        if words:
            span = max(1e-6, words[-1].end - words[0].start)
            profile.words_per_sec = round(len(words) / span, 3)
            # refine the genre decision now that syllable rate is known
            if not self.opt.forced_genre:
                probs = self.classifier.rule_scores(
                    {**_features_from_profile(profile), "words_per_sec": profile.words_per_sec})
                profile.probabilities = probs
                profile.genre = max(probs, key=probs.get)  # type: ignore[assignment]

        rep(0.88, "Разбивка на кинетические блоки…")
        phrases = self.segmenter.segment_words(words, profile)

        style: Style = (AutoStyleEngine.get_style_for_profile(profile, self.opt.safe_zone)
                        if self.opt.apply_auto_style
                        else AutoStyleEngine.get_style_for_genre("speech_podcast",
                                                                 self.opt.safe_zone))

        project = Project(
            video_path=media_path, duration=duration, fps=fps, width=w, height=h,
            language=self.speech.last_language or self.opt.language,
            audio_profile=profile, phrases=phrases, global_style=style,
        )
        rep(1.0, f"Готово: {len(phrases)} блоков, {len(words)} слов")
        return project


def _features_from_profile(p) -> dict[str, float]:
    return {
        "bpm": p.bpm, "onset_density": p.onset_density,
        "kick_rate": len(p.kick_events) / max(1e-6, p.duration),
        "snare_rate": len(p.snare_events) / max(1e-6, p.duration),
        "beat_regularity": p.beat_regularity,
        "low_energy": p.low_energy, "mid_energy": p.mid_energy, "high_energy": p.high_energy,
        "spectral_centroid": p.spectral_centroid, "zcr": p.zero_crossing_rate,
        "flatness": 0.2,
        "rms_std": float(_std(p.rms_energy)), "rms_mean": float(_mean(p.rms_energy)),
        "silence_ratio": _silence(p.rms_energy),
        "words_per_sec": p.words_per_sec,
    }


def _mean(v: list[float]) -> float:
    return sum(v) / len(v) if v else 0.0


def _std(v: list[float]) -> float:
    if not v:
        return 0.0
    m = _mean(v)
    return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5


def _silence(v: list[float]) -> float:
    if not v:
        return 0.0
    thr = max(1e-6, _mean(v) * 0.25)
    return sum(1 for x in v if x < thr) / len(v)

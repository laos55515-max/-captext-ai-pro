"""Фасад пайплайна: медиа → вокал → ASR/выравнивание → фразы → стиль.

Вся «тяжёлая» логика распознавания живёт в :mod:`app.core.asr_engine`;
здесь — оркестрация, анализ ритма, разбивка на блоки и сборка Project.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.audio import extract
from app.audio.asr_align import MMSForcedAligner, attach_kicks, mark_keywords, snap_to_onsets
from app.audio.classify import UniversalAudioClassifier
from app.audio.segment import PhraseSegmenter
from app.core.asr_engine import (MAX_SEGMENT_SECONDS, SONG_PROMPT, SPEECH_PROMPT,
                                 SeparationResult, TranscriptionEngine, TranscriptionResult,
                                 VocalSeparator, clean_lyrics, detect_voice_activity,
                                 drop_repeat_hallucinations, enforce_max_segment,
                                 first_vocal_onset, is_hallucination, pick_device,
                                 torch_device)
from app.core.models import AudioProfile, Project
from app.render.presets import AutoStyleEngine

log = logging.getLogger("captext.audio")
ProgressCB = Optional[Callable[[float, str], None]]

# обратная совместимость со старым именем
drop_hallucinations = drop_repeat_hallucinations

__all__ = ["AudioProcessor", "ProcessOptions", "ProcessReport", "VocalSeparator",
           "TranscriptionEngine", "TranscriptionResult", "SeparationResult",
           "clean_lyrics", "drop_hallucinations", "drop_repeat_hallucinations",
           "first_vocal_onset", "detect_voice_activity", "pick_device", "torch_device",
           "SONG_PROMPT", "SPEECH_PROMPT", "is_hallucination"]


@dataclass
class ProcessOptions:
    language: str = "ru"
    model_size: str = "large-v3"
    isolate_vocals: bool = True
    is_song: bool = True
    lyrics: str = ""                      # непустой → forced alignment
    safe_zone: str = "9:16"
    forced_genre: str | None = None
    apply_auto_style: bool = True
    snap_tolerance: float = 0.04
    vad_threshold: float = 0.35
    max_segment_seconds: float = MAX_SEGMENT_SECONDS


@dataclass
class ProcessReport:
    engine: str = ""
    separation: str = ""
    language: str = ""
    vad_method: str = ""
    first_onset: float = 0.0
    n_words: int = 0
    n_phrases: int = 0
    warnings: list[str] = field(default_factory=list)


class AudioProcessor:
    def __init__(self, options: ProcessOptions | None = None) -> None:
        self.opt = options or ProcessOptions()
        self.separator = VocalSeparator()
        self.engine = TranscriptionEngine(self.opt.model_size,
                                          vad_threshold=self.opt.vad_threshold,
                                          max_segment_seconds=self.opt.max_segment_seconds)
        self.classifier = UniversalAudioClassifier()
        self.segmenter = PhraseSegmenter()
        self.report = ProcessReport()

    def capabilities(self) -> dict[str, bool]:
        return {
            "demucs": VocalSeparator.demucs_available(),
            "stable_ts": TranscriptionEngine.stable_ts_available(),
            "faster_whisper": _module_ok("faster_whisper"),
            "torchaudio": MMSForcedAligner().available(),
            "cuda": pick_device()[0] == "cuda",
            "ffmpeg": extract.has_ffmpeg(),
        }

    def process(self, media_path: str, progress: ProgressCB = None) -> Project:
        def rep(p: float, m: str) -> None:
            if progress:
                progress(max(0.0, min(1.0, p)), m)

        self.report = ProcessReport()
        rep(0.02, "Чтение файла…")
        duration = extract.ffprobe_duration(media_path)
        w, h, fps = extract.probe_video(media_path)

        rep(0.06, "Анализ звука: BPM, онсеты, спектр…")
        try:
            profile = self.classifier.analyze(media_path)
        except Exception as exc:
            log.warning("анализ звука не удался: %s", exc)
            profile = AudioProfile(duration=duration)
            self.report.warnings.append(f"Анализ ритма пропущен: {exc}")
        if self.opt.forced_genre:
            profile.genre = self.opt.forced_genre  # type: ignore[assignment]

        work_dir = tempfile.mkdtemp(prefix="captext_")
        try:
            # 1. изоляция вокала
            audio_for_asr = media_path
            if self.opt.isolate_vocals:
                rep(0.12, "Отделение вокала от минусовки…")
                sep = self.separator.separate(media_path, work_dir,
                                              progress=lambda p, m: rep(0.12 + 0.25 * p, m))
                audio_for_asr = sep.vocals_path
                self.report.separation = sep.method
                if sep.method != "demucs" and sep.message:
                    self.report.warnings.append(sep.message)
            else:
                self.report.separation = "off"

            # 2. ASR или forced alignment
            if self.opt.lyrics.strip():
                rep(0.42, "Импорт текста: выравнивание по вокалу…")
                tr = self.engine.align_text(audio_for_asr, self.opt.lyrics, self.opt.language,
                                            progress=lambda p, m: rep(0.42 + 0.35 * p, m))
            else:
                rep(0.42, "Распознавание чистого вокала…")
                tr = self.engine.transcribe(audio_for_asr, self.opt.language,
                                            is_song=self.opt.is_song,
                                            progress=lambda p, m: rep(0.42 + 0.35 * p, m))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        self.report.engine = tr.engine
        self.report.language = tr.language
        self.report.vad_method = tr.vad_method
        self.report.first_onset = tr.first_onset
        self.report.warnings.extend(tr.warnings)

        # 3. постобработка относительно ритма
        rep(0.84, "Привязка к ритму…")
        words = snap_to_onsets(tr.words, profile.onsets, self.opt.snap_tolerance)
        words = attach_kicks(words, profile.kick_events)
        words = mark_keywords(words)
        words = enforce_max_segment(words, self.opt.max_segment_seconds)
        if words:
            span = max(1e-6, words[-1].end - words[0].start)
            profile.words_per_sec = round(len(words) / span, 3)

        # 4. блоки и стиль
        rep(0.92, "Разбивка на кинетические блоки…")
        phrases = self.segmenter.segment_words(words, profile)
        style = AutoStyleEngine.get_style_for_profile(
            profile if self.opt.apply_auto_style else None, self.opt.safe_zone)

        self.report.n_words = len(words)
        self.report.n_phrases = len(phrases)
        rep(1.0, f"Готово: {len(phrases)} блоков, {len(words)} слов "
                 f"[{tr.engine}, вокал: {self.report.separation}, "
                 f"старт вокала {tr.first_onset:.1f} с]")

        return Project(video_path=media_path, duration=duration, fps=fps, width=w, height=h,
                       language=tr.language, audio_profile=profile, phrases=phrases,
                       global_style=style)


def _module_ok(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False

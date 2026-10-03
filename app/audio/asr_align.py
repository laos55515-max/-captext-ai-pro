"""ASR (faster-whisper) + CTC forced alignment (torchaudio MMS_FA) + post-processing."""
from __future__ import annotations

import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from app.audio import extract
from app.core.models import Word

log = logging.getLogger("captext.asr")

ProgressCB = Optional[Callable[[float, str], None]]
SR = 16000


@dataclass
class ASRResult:
    words: list[Word]
    language: str
    text: str


# --------------------------------------------------------------------------- ASR
class ASRBackend:
    """Interface so mlx-whisper / whisper.cpp backends can be swapped in."""

    def transcribe(self, audio_path: str, language: str = "auto") -> ASRResult:
        raise NotImplementedError


class FasterWhisperBackend(ASRBackend):
    def __init__(self, model_size: str = "large-v3-turbo", device: str = "auto",
                 compute_type: str | None = None) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "faster-whisper is not installed. `pip install faster-whisper`"
            ) from exc
        device = self.device
        compute = self.compute_type
        if device == "auto":
            try:
                import torch  # type: ignore
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        if compute is None:
            compute = "float16" if device == "cuda" else "int8"
        log.info("loading faster-whisper %s on %s (%s)", self.model_size, device, compute)
        self._model = WhisperModel(self.model_size, device=device, compute_type=compute)
        return self._model

    def transcribe(self, audio_path: str, language: str = "auto") -> ASRResult:
        model = self._load()
        segments, info = model.transcribe(
            audio_path,
            language=None if language in ("auto", "", None) else language,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=300),
            beam_size=5,
            condition_on_previous_text=False,
        )
        words: list[Word] = []
        chunks: list[str] = []
        for seg in segments:
            chunks.append(seg.text)
            for w in (seg.words or []):
                txt = w.word.strip()
                if not txt:
                    continue
                words.append(Word(text=txt, start=float(w.start), end=float(w.end),
                                  confidence=float(getattr(w, "probability", 0.9) or 0.9)))
        return ASRResult(words=words, language=getattr(info, "language", language) or "unknown",
                         text="".join(chunks).strip())


# ------------------------------------------------------------------- alignment
class MMSForcedAligner:
    """torchaudio MMS_FA CTC forced alignment; gives the precise word timings."""

    def __init__(self) -> None:
        self._bundle = None
        self._model = None
        self._tokenizer = None
        self._aligner = None
        self._device = "cpu"

    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            from torchaudio.pipelines import MMS_FA  # noqa: F401
            return True
        except Exception:
            return False

    def _load(self):
        if self._model is not None:
            return
        import torch  # type: ignore
        from torchaudio.pipelines import MMS_FA  # type: ignore
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._bundle = MMS_FA
        self._model = MMS_FA.get_model().to(self._device).eval()
        self._tokenizer = MMS_FA.get_tokenizer()
        self._aligner = MMS_FA.get_aligner()

    @staticmethod
    def normalize(token: str) -> str:
        t = unicodedata.normalize("NFKC", token).lower()
        t = re.sub(r"[^\w'’ʼ-]", "", t, flags=re.UNICODE)
        return t.replace("’", "'").replace("ʼ", "'")

    def align(self, pcm16k: np.ndarray, words: list[Word]) -> list[Word]:
        """Re-time ``words`` against the waveform. Raises on failure (caller falls back)."""
        import torch  # type: ignore
        self._load()
        assert self._model is not None and self._tokenizer is not None and self._aligner is not None

        transcript = [self.normalize(w.text) for w in words]
        keep = [i for i, t in enumerate(transcript) if t]
        if not keep:
            raise ValueError("empty transcript after normalisation")
        tokens_words = [transcript[i] for i in keep]

        wav = torch.from_numpy(np.ascontiguousarray(pcm16k, dtype=np.float32))[None, :]
        with torch.inference_mode():
            emission, _ = self._model(wav.to(self._device))
            token_spans = self._aligner(emission[0], self._tokenizer(tokens_words))

        ratio = wav.shape[1] / emission.shape[1] / SR
        out = [w.model_copy(deep=True) for w in words]
        for idx, spans in zip(keep, token_spans):
            if not spans:
                continue
            start = float(spans[0].start) * ratio
            end = float(spans[-1].end) * ratio
            score = float(np.mean([s.score for s in spans]))
            out[idx].start = start
            out[idx].end = max(end, start + 0.04)
            out[idx].confidence = max(0.0, min(1.0, score))
        return out


# -------------------------------------------------------------- postprocessing
def enforce_monotonic(words: list[Word], min_dur: float = 0.06) -> list[Word]:
    prev_end = 0.0
    for w in words:
        w.start = max(w.start, prev_end)
        if w.end < w.start + min_dur:
            w.end = w.start + min_dur
        prev_end = w.end
    return words


def snap_to_onsets(words: list[Word], onsets: Iterable[float], tol: float = 0.04) -> list[Word]:
    arr = np.asarray(sorted(onsets), dtype=np.float64)
    if arr.size == 0:
        return words
    for w in words:
        j = int(np.searchsorted(arr, w.start))
        best, best_d = None, tol
        for k in (j - 1, j):
            if 0 <= k < arr.size:
                d = abs(float(arr[k]) - w.start)
                if d < best_d:
                    best, best_d = float(arr[k]), d
        if best is not None:
            shift = best - w.start
            w.start = best
            if w.end <= w.start + 0.03:
                w.end = w.start + max(0.06, 0.03 + shift)
    return enforce_monotonic(words)


def attach_kicks(words: list[Word], kicks: Iterable[float], window: float = 0.35) -> list[Word]:
    arr = np.asarray(sorted(kicks), dtype=np.float64)
    for w in words:
        w.kick = -1.0
        if arr.size:
            j = int(np.searchsorted(arr, w.start))
            if j < arr.size and arr[j] - w.start <= window:
                w.kick = float(arr[j])
            elif j > 0 and w.start - arr[j - 1] <= 0.08:
                w.kick = float(arr[j - 1])
    return words


def mark_keywords(words: list[Word], top_ratio: float = 0.2) -> list[Word]:
    """Cheap keyword heuristic: long, low-frequency tokens & words after long pauses."""
    from collections import Counter
    norm = [re.sub(r"\W", "", w.text.lower()) for w in words]
    freq = Counter(t for t in norm if t)
    scores = []
    for i, w in enumerate(words):
        t = norm[i]
        if not t:
            scores.append(0.0)
            continue
        s = len(t) / 4.0 + 1.0 / freq[t]
        if i > 0 and w.start - words[i - 1].end > 0.5:
            s += 0.8
        if w.text[:1].isupper() and i > 0:
            s += 0.5
        if len(t) <= 3:
            s -= 1.5
        scores.append(s)
    if not scores:
        return words
    k = max(1, int(len(words) * top_ratio))
    thr = sorted(scores, reverse=True)[k - 1]
    for w, s in zip(words, scores):
        w.is_keyword = bool(s >= thr and s > 1.0)
    return words


# ------------------------------------------------------------------- pipeline
class SpeechProcessor:
    def __init__(self, backend: ASRBackend | None = None,
                 aligner: MMSForcedAligner | None = None,
                 model_size: str = "large-v3-turbo") -> None:
        self.backend = backend or FasterWhisperBackend(model_size)
        self.aligner = aligner or MMSForcedAligner()
        self.last_language = "unknown"

    def transcribe_and_align(self, audio_path: str, language: str = "auto",
                             onsets: Iterable[float] = (),
                             kicks: Iterable[float] = (),
                             snap_tolerance: float = 0.04,
                             progress: ProgressCB = None) -> list[Word]:
        def report(p: float, msg: str) -> None:
            if progress:
                progress(p, msg)

        report(0.05, "Декодирование аудио 16 кГц…")
        pcm = extract.decode_pcm(audio_path, sr=SR)
        with tempfile.TemporaryDirectory() as td:
            wav = extract.write_wav(Path(td) / "asr.wav", pcm, SR)

            report(0.15, "Распознавание речи (Whisper)…")
            res = self.backend.transcribe(wav, language=language)
        self.last_language = res.language
        words = res.words
        if not words:
            return []

        report(0.65, "CTC-выравнивание (MMS_FA)…")
        if self.aligner.available():
            try:
                words = self.aligner.align(pcm, words)
            except Exception as exc:  # graceful fallback, marked as low confidence
                log.warning("forced alignment failed (%s) — using whisper timestamps", exc)
                for w in words:
                    w.confidence = min(w.confidence, 0.4)
        else:
            log.warning("torchaudio MMS_FA unavailable — using whisper timestamps")
            for w in words:
                w.confidence = min(w.confidence, 0.5)

        report(0.85, "Постобработка таймингов…")
        words = enforce_monotonic(words)
        words = snap_to_onsets(words, onsets, snap_tolerance)
        words = attach_kicks(words, kicks)
        words = mark_keywords(words)
        report(1.0, "Слова готовы")
        return words

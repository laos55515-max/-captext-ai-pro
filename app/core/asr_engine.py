"""CapText AI Pro — ASR-движок: изоляция вокала, VAD, защита от галлюцинаций в интро.

Главные правила этого модуля:

1. В Whisper уходит ТОЛЬКО изолированный вокал (Demucs/HTDemucs).
2. Перед распознаванием ищем **первую фактическую вспышку вокала** (`first_vocal_onset`).
   Всё, что модель «услышала» раньше этого момента, — галлюцинация музыкального
   вступления («Песня «Музыка»», «Субтитры сделал…») и безжалостно удаляется.
3. Silero VAD (через stable-ts `vad=True` или напрямую) режет бессигнальные участки.
4. Ни один сегмент не длиннее `MAX_SEGMENT_SECONDS` (5–7 с) — растянутая на 30 секунд
   «фраза» физически невозможна.
5. Forced alignment вставленного текста стартует ровно с первой секунды голоса:
   аудио обрезается по `first_vocal_onset`, а тайминги потом сдвигаются обратно.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from app.audio import extract
from app.audio.asr_align import MMSForcedAligner
from app.core.models import Word
from app.core.procutil import run_python_module

log = logging.getLogger("captext.asr_engine")

ProgressCB = Optional[Callable[[float, str], None]]

SR = 16000
MAX_SEGMENT_SECONDS = 6.0      # жёсткий предел длины одной фразы
MAX_WORD_SECONDS = 1.6         # ни одно слово не может звучать дольше
MIN_WORD_SECONDS = 0.06

SONG_PROMPT = (
    "Это текст песни. Куплет, припев, рифмованные строки, повторы и распевы. "
    "Пиши слова полностью, с пунктуацией, без описаний музыки и без пометок вроде "
    "«музыка играет», «вступление» или «аплодисменты»."
)
SPEECH_PROMPT = (
    "Это разговорная речь: подкаст, влог или интервью. Пиши с пунктуацией, "
    "без описаний звуков."
)

# Типовые галлюцинации Whisper на музыке/тишине (рус/укр/англ).
HALLUCINATION_PATTERNS = (
    r"^песня\b", r"^музыка\b", r"^играет музыка", r"^вступление", r"^инструментал",
    r"^субтитры", r"^редактор субтитров", r"^корректор", r"^продолжение следует",
    r"^спасибо за просмотр", r"^подписывайтесь", r"^музика\b", r"^пісня\b",
    r"^\[?music\]?$", r"^\(music\)$", r"^thanks for watching", r"^subtitles by",
    r"^applause$", r"^аплодисменты$", r"^ั$", r"^\.+$", r"^…$",
)
_HALLUCINATION_RE = tuple(re.compile(p, re.IGNORECASE) for p in HALLUCINATION_PATTERNS)


# ============================================================ УСТРОЙСТВА
def pick_device() -> tuple[str, str]:
    """(device, compute_type) для Whisper/CTranslate2 с безопасным откатом на CPU."""
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            return "cuda", "float16"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "cpu", "int8"        # CTranslate2 не умеет MPS
    except Exception as exc:
        log.debug("torch недоступен: %s", exc)
    return "cpu", "int8"


def torch_device() -> str:
    """Устройство для Demucs и torch-моделей (MPS допустим)."""
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            return "mps"
    except Exception:
        pass
    return "cpu"


# ============================================== 1. ИЗОЛЯЦИЯ ВОКАЛА (DEMUCS)
@dataclass
class SeparationResult:
    vocals_path: str
    method: str                     # "demucs" | "hpss" | "raw"
    message: str = ""


class VocalSeparator:
    """Отделение вокала от минусовки — фундамент точности на песнях."""

    def __init__(self, model_name: str = "htdemucs", device: str | None = None) -> None:
        self.model_name = model_name
        self.device = device or torch_device()

    @staticmethod
    def demucs_available() -> bool:
        try:
            import demucs  # type: ignore  # noqa: F401
            return True
        except Exception:
            return False

    def separate(self, audio_path: str, out_dir: str | None = None,
                 progress: ProgressCB = None) -> SeparationResult:
        def rep(p: float, m: str) -> None:
            if progress:
                progress(p, m)

        out_dir = out_dir or tempfile.mkdtemp(prefix="captext_sep_")
        os.makedirs(out_dir, exist_ok=True)

        if self.demucs_available():
            rep(0.05, f"Отделение вокала (Demucs {self.model_name}, {self.device})…")
            try:
                return SeparationResult(self._demucs_api(audio_path, out_dir, rep), "demucs",
                                        f"Demucs {self.model_name} на {self.device}")
            except Exception as exc:
                log.warning("Demucs API не сработал (%s) — пробую CLI", exc)
                try:
                    return SeparationResult(self._demucs_cli(audio_path, out_dir), "demucs",
                                            "Demucs CLI")
                except Exception as exc2:
                    log.warning("Demucs CLI тоже не сработал: %s", exc2)

        rep(0.05, "Demucs недоступен — изоляция вокала фильтрами (HPSS)…")
        return SeparationResult(
            self._hpss_fallback(audio_path, out_dir), "hpss",
            "Demucs не установлен — использован упрощённый HPSS-фильтр. "
            "Для максимальной точности: pip install demucs")

    def _demucs_api(self, audio_path: str, out_dir: str,
                    rep: Callable[[float, str], None]) -> str:
        from demucs.api import Separator, save_audio  # type: ignore
        separator = Separator(model=self.model_name, device=self.device, progress=False)
        rep(0.12, "Demucs: загрузка модели…")
        _origin, stems = separator.separate_audio_file(Path(audio_path))
        if "vocals" not in stems:
            raise RuntimeError(f"нет стема 'vocals' (есть: {list(stems)})")
        out = os.path.join(out_dir, "vocals.wav")
        rep(0.55, "Demucs: сохранение вокальной дорожки…")
        save_audio(stems["vocals"], out, samplerate=separator.samplerate)
        return out

    def _demucs_cli(self, audio_path: str, out_dir: str) -> str:
        """Demucs через CLI.

        ВАЖНО: запускается через `procutil.run_python_module`, а НЕ как
        `[sys.executable, "-m", "demucs.separate", ...]`. В собранном .app
        `sys.executable` — это само приложение, поэтому прямой вызов перезапускал
        GUI (дубликаты окон в Dock), а demucs не выполнялся.
        """
        args = ["-n", self.model_name, "--two-stems", "vocals",
                "-d", self.device, "-o", out_dir, audio_path]
        proc = run_python_module("demucs.separate", args, capture_output=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(detail[-500:] or f"demucs CLI вернул код {proc.returncode}")
        for p in Path(out_dir).rglob("vocals.*"):
            return str(p)
        raise RuntimeError("Demucs CLI не создал vocals.*")

    def _hpss_fallback(self, audio_path: str, out_dir: str) -> str:
        pcm = extract.decode_pcm(audio_path, sr=SR)
        try:
            import librosa  # type: ignore
            harm = librosa.effects.harmonic(pcm, margin=3.0)
            S = librosa.stft(harm, n_fft=1024, hop_length=256)
            freqs = librosa.fft_frequencies(sr=SR, n_fft=1024)
            band = ((freqs >= 120) & (freqs <= 8000)).astype(np.float32)[:, None]
            voice = librosa.istft(S * band, hop_length=256, length=len(pcm))
        except Exception as exc:
            log.debug("librosa HPSS недоступен: %s", exc)
            voice = _bandpass(pcm, SR, 150.0, 7000.0)
        peak = float(np.max(np.abs(voice)) or 1.0)
        return extract.write_wav(os.path.join(out_dir, "vocals.wav"),
                                 (voice / peak * 0.95).astype(np.float32), SR)


def _bandpass(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    """Полосовой фильтр без scipy: разность двух однополюсных ФНЧ (векторизовано)."""
    def onepole(sig: np.ndarray, cutoff: float) -> np.ndarray:
        a = float(np.exp(-2.0 * np.pi * cutoff / sr))
        out = np.empty_like(sig)
        acc = 0.0
        for i in range(sig.size):
            acc = a * acc + (1.0 - a) * float(sig[i])
            out[i] = acc
        return out
    x = x.astype(np.float32)
    return (onepole(x, hi) - onepole(x, lo)).astype(np.float32)


# ====================================== 2. VAD И ПЕРВАЯ ВСПЫШКА ВОКАЛА
@dataclass
class VoiceActivity:
    first_onset: float = 0.0             # первая фактическая секунда голоса
    segments: list[tuple[float, float]] = field(default_factory=list)
    method: str = "energy"               # "silero" | "energy"


def silero_vad_segments(pcm16k: np.ndarray, threshold: float = 0.35) -> list[tuple[float, float]]:
    """Речевые интервалы через Silero VAD (torch.hub). Бросает исключение, если недоступен."""
    import torch  # type: ignore
    model, utils = torch.hub.load("snakers4/silero-vad", "silero_vad",
                                  trust_repo=True, onnx=False, verbose=False)
    get_speech_timestamps = utils[0]
    tensor = torch.from_numpy(np.ascontiguousarray(pcm16k, dtype=np.float32))
    stamps = get_speech_timestamps(tensor, model, sampling_rate=SR, threshold=threshold,
                                   min_speech_duration_ms=180, min_silence_duration_ms=180)
    return [(float(s["start"]) / SR, float(s["end"]) / SR) for s in stamps]


def energy_vad_segments(pcm16k: np.ndarray, threshold: float = 0.35,
                        hop: int = 160, min_speech: float = 0.18,
                        min_silence: float = 0.20) -> list[tuple[float, float]]:
    """Надёжный энергетический VAD (без сети и внешних моделей).

    Адаптивный порог: шумовой пол считается по 20-му перцентилю RMS изолированного
    вокала, поэтому реверберация «минуса» не принимается за голос.
    """
    if pcm16k.size < hop * 4:
        return []
    n = pcm16k.size // hop
    frames = pcm16k[: n * hop].reshape(n, hop)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)) + 1e-12
    db = 20.0 * np.log10(rms)
    floor = float(np.percentile(db, 20))
    peak = float(np.percentile(db, 98))
    if peak - floor < 6.0:                 # сигнала фактически нет
        return []
    # threshold 0..1 → положение порога между шумовым полом и пиком
    level = floor + (peak - floor) * (0.35 + 0.45 * float(threshold))
    voiced = db > level

    segments: list[tuple[float, float]] = []
    frame_s = hop / SR
    start: float | None = None
    silence = 0.0
    for i, v in enumerate(voiced):
        t = i * frame_s
        if v:
            if start is None:
                start = t
            silence = 0.0
        elif start is not None:
            silence += frame_s
            if silence >= min_silence:
                end = t - silence
                if end - start >= min_speech:
                    segments.append((start, end))
                start = None
                silence = 0.0
    if start is not None:
        end = len(pcm16k) / SR
        if end - start >= min_speech:
            segments.append((start, end))
    return segments


def detect_voice_activity(pcm16k: np.ndarray, threshold: float = 0.35,
                          use_silero: bool = True) -> VoiceActivity:
    """Silero VAD, при недоступности — энергетический VAD. Никогда не падает."""
    segments: list[tuple[float, float]] = []
    method = "energy"
    if use_silero:
        try:
            segments = silero_vad_segments(pcm16k, threshold)
            method = "silero"
        except Exception as exc:
            log.info("Silero VAD недоступен (%s) — энергетический VAD", exc)
    if not segments:
        segments = energy_vad_segments(pcm16k, threshold)
        method = "energy" if method != "silero" else "silero→energy"
    first = segments[0][0] if segments else 0.0
    return VoiceActivity(first_onset=float(first), segments=segments, method=method)


def first_vocal_onset(vocals_path: str, threshold: float = 0.35) -> float:
    """Секунда, на которой в `vocals.wav` реально начинается голос (интро игнорируется)."""
    pcm = extract.decode_pcm(vocals_path, sr=SR)
    return detect_voice_activity(pcm, threshold).first_onset


# ======================================== 3. ЧИСТКА ТЕКСТА И ТАЙМИНГОВ
def is_hallucination(text: str) -> bool:
    t = text.strip().strip("«»\"'()[]").lower()
    if not t:
        return True
    return any(rx.search(t) for rx in _HALLUCINATION_RE)


def drop_intro_hallucinations(words: list[Word], first_onset: float,
                              tolerance: float = 0.35) -> list[Word]:
    """Убрать всё, что модель «услышала» до первой вспышки вокала.

    Ровно это чинит кейс «Песня «Музыка»» длиной 30 секунд поверх вступления:
    такие слова заканчиваются раньше реального вокала и удаляются целиком,
    а первое настоящее слово подтягивается к началу голоса.
    """
    if not words:
        return words
    limit = max(0.0, first_onset - tolerance)
    kept: list[Word] = []
    for w in words:
        if w.end <= limit:                       # полностью в интро → выкидываем
            continue
        if w.start < limit and is_hallucination(w.text):
            continue
        if w.start < limit:                      # частично в интро → подрезаем
            w = w.model_copy(update={"start": limit})
        kept.append(w)
    if kept and first_onset > 0:
        # ЖЁСТКАЯ привязка: первое слово начинается ровно там, где реально начался вокал
        shift = kept[0].start - first_onset
        if abs(shift) > 0.06:
            for w in kept:
                w.start = max(0.0, w.start - shift)
                w.end = max(w.start + MIN_WORD_SECONDS, w.end - shift)
    return kept


def drop_repeat_hallucinations(words: list[Word], max_repeat: int = 4) -> list[Word]:
    """Отсечь зацикленные повторы одного слова — классический признак галлюцинации."""
    out: list[Word] = []
    run = 0
    for w in words:
        prev = out[-1].text.lower().strip(".,!?…") if out else None
        cur = w.text.lower().strip(".,!?…")
        run = run + 1 if prev == cur else 0
        if run >= max_repeat:
            continue
        out.append(w)
    return out


def clamp_word_durations(words: list[Word], max_word: float = MAX_WORD_SECONDS) -> list[Word]:
    """Ни одно слово не длиннее `max_word` — иначе субтитр «залипает» на экране."""
    for w in words:
        if w.end - w.start > max_word:
            w.end = w.start + max_word
        if w.end - w.start < MIN_WORD_SECONDS:
            w.end = w.start + MIN_WORD_SECONDS
    return words


def enforce_max_segment(words: list[Word], max_seconds: float = MAX_SEGMENT_SECONDS,
                        gap: float = 0.28) -> list[Word]:
    """Гарантировать, что непрерывная фраза не длиннее `max_seconds`.

    Если слова идут сплошняком дольше лимита, вставляем микропаузу —
    дальше DP-разбивка обязана разрезать блок в этом месте.
    """
    if not words:
        return words
    run_start = words[0].start
    for i, w in enumerate(words):
        if w.end - run_start > max_seconds:
            if i > 0:
                prev = words[i - 1]
                prev.end = max(prev.start + MIN_WORD_SECONDS, prev.end - gap / 2)
                w.start = max(prev.end + gap, w.start)
                if w.end < w.start + MIN_WORD_SECONDS:
                    w.end = w.start + MIN_WORD_SECONDS
            run_start = w.start
    return words


def filter_by_vad(words: list[Word], segments: list[tuple[float, float]],
                  slack: float = 0.25) -> list[Word]:
    """Оставить только слова, пересекающиеся с речевыми интервалами VAD."""
    if not segments:
        return words
    out: list[Word] = []
    for w in words:
        for s, e in segments:
            if w.end >= s - slack and w.start <= e + slack:
                out.append(w)
                break
    return out


def clean_lyrics(text: str) -> str:
    """Очистить вставленный текст песни от разметки: [Куплет 1], (x2), «Припев:»."""
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\((?:x\d+|повтор[^)]*|припев[^)]*)\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(куплет|припев|бридж|verse|chorus|bridge|hook)\s*\d*\s*:?\s*$", " ",
                  text, flags=re.IGNORECASE | re.MULTILINE)
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


# ============================================== 4. ДВИЖОК РАСПОЗНАВАНИЯ
@dataclass
class TranscriptionResult:
    words: list[Word] = field(default_factory=list)
    language: str = "unknown"
    text: str = ""
    engine: str = ""
    first_onset: float = 0.0
    vad_method: str = ""
    warnings: list[str] = field(default_factory=list)


class TranscriptionEngine:
    """stable-ts (приоритет) → faster-whisper (фолбэк). VAD + фиксация языка + анти-интро."""

    def __init__(self, model_size: str = "large-v3", device: str | None = None,
                 compute_type: str | None = None, vad_threshold: float = 0.35,
                 max_segment_seconds: float = MAX_SEGMENT_SECONDS) -> None:
        dev, comp = pick_device()
        self.model_size = model_size
        self.device = device or dev
        self.compute_type = compute_type or comp
        self.vad_threshold = vad_threshold
        self.max_segment_seconds = max_segment_seconds
        self._stable = None
        self._faster = None

    # -- доступность
    @staticmethod
    def stable_ts_available() -> bool:
        try:
            import stable_whisper  # type: ignore  # noqa: F401
            return True
        except Exception:
            return False

    def backend_name(self) -> str:
        if self.stable_ts_available():
            return f"stable-ts ({self.model_size}, {self.device})"
        return f"faster-whisper ({self.model_size}, {self.device}/{self.compute_type})"

    # -- загрузка
    def _load_stable(self):
        if self._stable is None:
            import stable_whisper  # type: ignore
            log.info("stable-ts: загрузка %s на %s", self.model_size, self.device)
            try:
                self._stable = stable_whisper.load_faster_whisper(
                    self.model_size, device=self.device, compute_type=self.compute_type)
            except Exception as exc:
                log.warning("stable-ts faster-backend недоступен (%s) — torch-модель", exc)
                self._stable = stable_whisper.load_model(self.model_size, device=self.device)
        return self._stable

    def _load_faster(self):
        if self._faster is None:
            from faster_whisper import WhisperModel  # type: ignore
            self._faster = WhisperModel(self.model_size, device=self.device,
                                        compute_type=self.compute_type)
        return self._faster

    # -- транскрипция
    def transcribe(self, vocals_path: str, language: str = "ru", is_song: bool = True,
                   initial_prompt: str | None = None,
                   progress: ProgressCB = None) -> TranscriptionResult:
        def rep(p: float, m: str) -> None:
            if progress:
                progress(p, m)

        prompt = initial_prompt or (SONG_PROMPT if is_song else SPEECH_PROMPT)
        lang = None if language in ("auto", "", None) else language
        warns: list[str] = []
        if self.device == "cpu":
            warns.append("GPU не найден — распознавание идёт на CPU, это медленнее.")

        # --- VAD ДО модели: знаем, где реально начинается голос
        rep(0.05, "VAD: поиск первой вспышки вокала…")
        pcm = extract.decode_pcm(vocals_path, sr=SR)
        va = detect_voice_activity(pcm, self.vad_threshold)
        log.info("VAD (%s): первая вспышка вокала на %.2f с, интервалов: %d",
                 va.method, va.first_onset, len(va.segments))

        if self.stable_ts_available():
            try:
                rep(0.15, "Stable-Whisper: распознавание чистого вокала…")
                res = self._transcribe_stable(vocals_path, lang, prompt, warns)
            except Exception as exc:
                log.warning("stable-ts упал (%s) — faster-whisper", exc)
                warns.append(f"stable-ts недоступен ({exc}); использован faster-whisper.")
                rep(0.15, "faster-whisper: распознавание чистого вокала…")
                res = self._transcribe_faster(vocals_path, lang, prompt, warns)
        else:
            warns.append("stable-ts не установлен (pip install stable-ts) — точность ниже.")
            rep(0.15, "faster-whisper: распознавание чистого вокала…")
            res = self._transcribe_faster(vocals_path, lang, prompt, warns)

        rep(0.85, "Чистка интро и нормализация таймингов…")
        res.words = self.postprocess(res.words, va)
        res.first_onset = va.first_onset
        res.vad_method = va.method
        return res

    def postprocess(self, words: list[Word], va: VoiceActivity) -> list[Word]:
        """Полная цепочка защиты от «30-секундной галлюцинации» во вступлении."""
        words = [w for w in words if not is_hallucination(w.text)]
        words = filter_by_vad(words, va.segments)
        words = drop_intro_hallucinations(words, va.first_onset)
        words = drop_repeat_hallucinations(words)
        words = clamp_word_durations(words)
        words = enforce_max_segment(words, self.max_segment_seconds)
        # финальная монотонность
        prev_end = 0.0
        for w in words:
            w.start = max(w.start, prev_end)
            w.end = max(w.end, w.start + MIN_WORD_SECONDS)
            prev_end = w.end
        return words

    def _transcribe_stable(self, path: str, lang: str | None, prompt: str,
                           warns: list[str]) -> TranscriptionResult:
        model = self._load_stable()
        result = model.transcribe(
            path,
            language=lang,                  # принудительная фиксация языка
            vad=True,                       # Silero VAD внутри stable-ts
            vad_threshold=self.vad_threshold,
            word_timestamps=True,
            initial_prompt=prompt,
            regroup=True,
            suppress_silence=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.45,
            compression_ratio_threshold=2.2,
            temperature=0.0,
        )
        return TranscriptionResult(words=words_from_stable(result),
                                   language=getattr(result, "language", lang or "unknown"),
                                   text=getattr(result, "text", "") or "",
                                   engine="stable-ts", warnings=warns)

    def _transcribe_faster(self, path: str, lang: str | None, prompt: str,
                           warns: list[str]) -> TranscriptionResult:
        model = self._load_faster()
        segments, info = model.transcribe(
            path, language=lang, word_timestamps=True, vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=250, threshold=self.vad_threshold),
            initial_prompt=prompt, beam_size=5, temperature=0.0,
            condition_on_previous_text=False, no_speech_threshold=0.45,
            compression_ratio_threshold=2.2,
        )
        words: list[Word] = []
        chunks: list[str] = []
        for seg in segments:
            chunks.append(seg.text)
            for w in (seg.words or []):
                txt = w.word.strip()
                if txt:
                    words.append(Word(text=txt, start=float(w.start), end=float(w.end),
                                      confidence=float(getattr(w, "probability", 0.9) or 0.9)))
        return TranscriptionResult(words=words,
                                   language=getattr(info, "language", lang or "unknown"),
                                   text="".join(chunks).strip(),
                                   engine="faster-whisper", warnings=warns)

    # -- принудительное выравнивание вставленного текста
    def align_text(self, vocals_path: str, lyrics: str, language: str = "ru",
                   progress: ProgressCB = None) -> TranscriptionResult:
        """«Свой текст песни»: первое слово жёстко привязывается к первой секунде голоса.

        Музыкальное интро полностью игнорируется: аудио обрезается по первой вспышке
        вокала, выравнивание идёт по обрезку, после чего тайминги сдвигаются обратно.
        """
        def rep(p: float, m: str) -> None:
            if progress:
                progress(p, m)

        lyrics = clean_lyrics(lyrics)
        if not lyrics:
            raise ValueError("Пустой текст для выравнивания")
        warns: list[str] = []

        rep(0.05, "VAD: поиск начала вокала…")
        pcm = extract.decode_pcm(vocals_path, sr=SR)
        va = detect_voice_activity(pcm, self.vad_threshold)
        onset = max(0.0, va.first_onset - 0.08)      # небольшой запас на атаку звука
        trimmed = pcm[int(onset * SR):]
        if trimmed.size < SR // 2:                   # вокала почти нет — берём всё
            trimmed, onset = pcm, 0.0

        with tempfile.TemporaryDirectory() as td:
            trimmed_path = extract.write_wav(Path(td) / "vocals_trim.wav", trimmed, SR)

            words: list[Word] = []
            engine = ""
            if self.stable_ts_available():
                try:
                    rep(0.25, "Выравнивание текста по вокалу (stable-ts align)…")
                    model = self._load_stable()
                    result = model.align(trimmed_path, lyrics,
                                         language=None if language == "auto" else language,
                                         vad=True, suppress_silence=True, regroup=True,
                                         original_split=True)
                    words = words_from_stable(result)
                    engine = "stable-ts align"
                except Exception as exc:
                    log.warning("stable-ts align упал: %s", exc)
                    warns.append(f"stable-ts align недоступен ({exc}) — CTC MMS_FA.")
            else:
                warns.append("stable-ts не установлен — выравнивание через torchaudio MMS_FA.")

            if not words:
                rep(0.45, "CTC-выравнивание (torchaudio MMS_FA)…")
                aligner = MMSForcedAligner()
                if not aligner.available():
                    raise RuntimeError(
                        "Нет движка выравнивания. Установите: pip install stable-ts "
                        "или pip install torch torchaudio")
                tokens = lyrics.split()
                total = trimmed.size / SR
                step = total / max(1, len(tokens))
                seed = [Word(text=t, start=i * step, end=(i + 1) * step, confidence=0.5)
                        for i, t in enumerate(tokens)]
                words = aligner.align(trimmed, seed)
                engine = "MMS_FA align"

        # сдвигаем тайминги обратно в систему координат исходного файла
        for w in words:
            w.start += onset
            w.end += onset

        # ЖЁСТКАЯ привязка первого слова к первой секунде голоса
        if words:
            shift = words[0].start - va.first_onset
            if abs(shift) > 0.05:
                for w in words:
                    w.start = max(0.0, w.start - shift)
                    w.end = max(w.start + MIN_WORD_SECONDS, w.end - shift)

        words = clamp_word_durations(words)
        words = enforce_max_segment(words, self.max_segment_seconds)
        rep(1.0, "Текст выровнен по вокалу")
        return TranscriptionResult(words=words, language=language, text=lyrics,
                                   engine=engine, first_onset=va.first_onset,
                                   vad_method=va.method, warnings=warns)


def words_from_stable(result) -> list[Word]:
    """Достать слова из WhisperResult (stable-ts) в нашу модель."""
    words: list[Word] = []
    for seg in (getattr(result, "segments", None) or []):
        for w in (getattr(seg, "words", None) or []):
            text = (getattr(w, "word", "") or "").strip()
            if not text:
                continue
            words.append(Word(
                text=text,
                start=float(getattr(w, "start", 0.0)),
                end=float(getattr(w, "end", 0.0)),
                confidence=float(getattr(w, "probability", None)
                                 or getattr(w, "score", None) or 0.9),
            ))
    return words

"""Rhythm analysis: BPM, onsets, kick/snare bands, RMS envelope."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

try:
    import librosa  # type: ignore
    _HAS_LIBROSA = True
except Exception:  # pragma: no cover
    librosa = None  # type: ignore
    _HAS_LIBROSA = False

SR = 22050
HOP = 256
N_FFT = 2048


@dataclass
class RhythmResult:
    bpm: float = 0.0
    beats: list[float] = field(default_factory=list)
    onsets: list[float] = field(default_factory=list)
    kick_events: list[float] = field(default_factory=list)
    snare_events: list[float] = field(default_factory=list)
    rms: list[float] = field(default_factory=list)
    hop_seconds: float = HOP / SR
    onset_density: float = 0.0
    beat_regularity: float = 0.0
    low_energy: float = 0.0
    mid_energy: float = 0.0
    high_energy: float = 0.0
    spectral_centroid: float = 0.0
    zero_crossing_rate: float = 0.0
    spectral_flatness: float = 0.0
    duration: float = 0.0


def _flux_peaks(band: np.ndarray, hop_seconds: float, delta_mult: float = 1.6) -> list[float]:
    """Half-wave-rectified spectral flux of a band + adaptive-median peak picking."""
    if band.size == 0 or band.shape[1] < 3:
        return []
    flux = np.maximum(0.0, np.diff(band, axis=1)).sum(axis=0)
    if flux.max() <= 0:
        return []
    flux = flux / flux.max()
    win = 21
    pad = np.pad(flux, (win // 2, win // 2), mode="edge")
    local = np.array([np.median(pad[i:i + win]) for i in range(len(flux))])
    thr = local * delta_mult + 0.02
    peaks: list[float] = []
    last = -1e9
    for i in range(1, len(flux) - 1):
        if flux[i] > thr[i] and flux[i] >= flux[i - 1] and flux[i] > flux[i + 1]:
            t = (i + 1) * hop_seconds
            if t - last > 0.045:
                peaks.append(round(float(t), 4))
                last = t
    return peaks


def analyze_rhythm(pcm22k: np.ndarray, sr: int = SR) -> RhythmResult:
    """Full rhythm analysis on a mono signal (expects ~22.05 kHz)."""
    res = RhythmResult(hop_seconds=HOP / sr)
    if pcm22k.size < sr // 2:
        return res
    y = pcm22k.astype(np.float32)
    res.duration = len(y) / sr

    if _HAS_LIBROSA:
        S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
        env = librosa.onset.onset_strength(S=librosa.power_to_db(S ** 2),
                                           sr=sr, hop_length=HOP, aggregate=np.median)
        tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=HOP)
        res.bpm = float(np.atleast_1d(tempo)[0])
        res.beats = [round(float(t), 4) for t in
                     librosa.frames_to_time(beats, sr=sr, hop_length=HOP)]
        on_frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP,
                                               backtrack=True)
        res.onsets = [round(float(t), 4) for t in
                      librosa.frames_to_time(on_frames, sr=sr, hop_length=HOP)]
        res.rms = [float(v) for v in librosa.feature.rms(S=S, hop_length=HOP)[0]]
        res.spectral_centroid = float(np.mean(librosa.feature.spectral_centroid(S=S, sr=sr)))
        res.zero_crossing_rate = float(np.mean(librosa.feature.zero_crossing_rate(y, hop_length=HOP)))
        res.spectral_flatness = float(np.mean(librosa.feature.spectral_flatness(S=S)))
    else:  # numpy-only fallback (still fully functional)
        S, freqs = _stft_numpy(y, sr)
        env = np.maximum(0.0, np.diff(np.log1p(S).sum(axis=0), prepend=0.0))
        res.onsets = _flux_peaks(S, res.hop_seconds, 1.4)
        res.bpm, res.beats = _tempo_from_onsets(res.onsets)
        res.rms = [float(v) for v in np.sqrt((S ** 2).mean(axis=0)) * 2.0]
        mag = S.sum(axis=0) + 1e-9
        res.spectral_centroid = float(np.mean((freqs[:, None] * S).sum(axis=0) / mag))
        res.zero_crossing_rate = float(np.mean(np.abs(np.diff(np.sign(y))) > 0))
        res.spectral_flatness = float(np.exp(np.mean(np.log(S + 1e-9))) / (S.mean() + 1e-9))

    # frequency-band energies & transient bands
    if _HAS_LIBROSA:
        pass_S, pass_f = S, freqs
    else:
        pass_S, pass_f = S, freqs
    low = pass_S[(pass_f >= 30) & (pass_f <= 120)]
    mid = pass_S[(pass_f > 120) & (pass_f <= 2000)]
    high = pass_S[(pass_f > 2000)]
    snare_band = pass_S[((pass_f >= 150) & (pass_f <= 250)) | ((pass_f >= 2000) & (pass_f <= 8000))]
    tot = float(pass_S.sum()) + 1e-9
    res.low_energy = float(low.sum()) / tot
    res.mid_energy = float(mid.sum()) / tot
    res.high_energy = float(high.sum()) / tot
    res.kick_events = _flux_peaks(low, res.hop_seconds, 1.8)
    res.snare_events = _flux_peaks(snare_band, res.hop_seconds, 1.8)

    res.onset_density = len(res.onsets) / max(1e-6, res.duration)
    res.beat_regularity = _regularity(res.beats)
    return res


def _stft_numpy(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    win = np.hanning(N_FFT).astype(np.float32)
    n = 1 + max(0, (len(y) - N_FFT) // HOP)
    if n <= 0:
        return np.zeros((N_FFT // 2 + 1, 0), np.float32), np.fft.rfftfreq(N_FFT, 1 / sr)
    frames = np.lib.stride_tricks.sliding_window_view(y, N_FFT)[::HOP][:n] * win
    S = np.abs(np.fft.rfft(frames, axis=1)).T.astype(np.float32)
    return S, np.fft.rfftfreq(N_FFT, 1 / sr)


def _tempo_from_onsets(onsets: list[float]) -> tuple[float, list[float]]:
    if len(onsets) < 4:
        return 0.0, []
    iois = np.diff(np.asarray(onsets))
    iois = iois[(iois > 0.2) & (iois < 1.2)]
    if iois.size == 0:
        return 0.0, []
    period = float(np.median(iois))
    bpm = 60.0 / period
    while bpm < 70:
        bpm *= 2
        period /= 2
    while bpm > 190:
        bpm /= 2
        period *= 2
    beats = list(np.arange(onsets[0], onsets[-1], period))
    return round(bpm, 2), [round(float(b), 4) for b in beats]


def _regularity(beats: list[float]) -> float:
    if len(beats) < 4:
        return 0.0
    d = np.diff(np.asarray(beats))
    if d.mean() <= 0:
        return 0.0
    cv = float(d.std() / d.mean())
    return float(max(0.0, 1.0 - cv * 4.0))

"""Audio/media decoding with a zero-dependency FFmpeg fallback chain.

Resolution order for the ffmpeg binary:
    1. explicit override (CAPTEXT_FFMPEG env var or ``set_ffmpeg_path()``)
    2. system PATH
    3. ``static_ffmpeg`` (downloads/ships static binaries)
    4. ``imageio_ffmpeg`` (bundles a static ffmpeg wheel)
    5. common install locations (Homebrew, /usr/local, Program Files, winget shims)

In a frozen build (PyInstaller) ``<bundle>/bin`` — where ``captext_ai_pro.spec``
puts ffmpeg/ffprobe copied from the build machine — comes *before* the download
backends, so a shipped app never fetches binaries at first launch.

If *no* ffmpeg is available at all, decoding transparently falls back to **PyAV**
(``av``), which links its own libav* — so the app still works. Only the pipe-based
video export truly requires an ffmpeg executable.
"""
from __future__ import annotations

import functools
import logging
import os
import platform
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("captext.extract")

try:  # optional but strongly recommended
    import av  # type: ignore
    _HAS_AV = True
except Exception:  # pragma: no cover
    av = None  # type: ignore
    _HAS_AV = False

_OVERRIDE: dict[str, str | None] = {"ffmpeg": None, "ffprobe": None}


class AudioDecodeError(RuntimeError):
    pass


# --------------------------------------------------------------- ffmpeg lookup
def set_ffmpeg_path(ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
    """Force specific binaries (useful for frozen PyInstaller bundles)."""
    if ffmpeg:
        _OVERRIDE["ffmpeg"] = ffmpeg
    if ffprobe:
        _OVERRIDE["ffprobe"] = ffprobe
    find_ffmpeg.cache_clear()
    find_ffprobe.cache_clear()


def _bundle_bin() -> str | None:
    """``<bundle>/bin`` собранного приложения (PyInstaller: ``sys._MEIPASS/bin``).

    ``captext_ai_pro.spec`` кладёт туда ffmpeg/ffprobe, если они нашлись на
    машине сборки. Для пользователя это значит: скачивать ничего не нужно.
    """
    base = getattr(sys, "_MEIPASS", "")
    if not base:
        return None
    cand = os.path.join(base, "bin")
    return cand if os.path.isdir(cand) else None


def _from_bundle(name: str) -> str | None:
    """ffmpeg/ffprobe, запечённые в сборку. Первым делом, до всяких загрузок."""
    directory = _bundle_bin()
    if not directory:
        return None
    for cand in (os.path.join(directory, name),
                 os.path.join(directory, name + ".exe")):
        if os.path.exists(cand):
            _prepend_to_path(directory)
            log.info("%s взят из бандла: %s", name, cand)
            return cand
    return None


def _candidate_dirs() -> list[str]:
    system = platform.system()
    dirs = [os.environ.get("CAPTEXT_FFMPEG_DIR", "")]
    _bin = _bundle_bin()
    if _bin:
        dirs.append(_bin)
    if system == "Darwin":
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
                 str(Path.home() / "bin")]
    elif system == "Windows":
        dirs += [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                 os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps"),
                 os.path.expandvars(r"%USERPROFILE%\scoop\shims")]
    else:
        dirs += ["/usr/bin", "/usr/local/bin", "/snap/bin"]
    return [d for d in dirs if d]


def _from_static_ffmpeg() -> str | None:
    """static_ffmpeg installs binaries and can prepend them to PATH."""
    try:
        import static_ffmpeg  # type: ignore
        try:
            from static_ffmpeg import run as static_run  # type: ignore
            ff, _fp = static_run.get_or_fetch_platform_executables_else_raise()
            if ff and os.path.exists(ff):
                return ff
        except Exception:
            static_ffmpeg.add_paths()          # older API: mutates PATH
            return shutil.which("ffmpeg")
    except Exception as exc:
        log.debug("static_ffmpeg unavailable: %s", exc)
    return None


def _from_imageio() -> str | None:
    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception as exc:
        log.debug("imageio_ffmpeg unavailable: %s", exc)
    return None


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Return a usable ffmpeg executable path, or None. Never raises."""
    if _OVERRIDE["ffmpeg"] and os.path.exists(_OVERRIDE["ffmpeg"]):
        return _OVERRIDE["ffmpeg"]
    env = os.environ.get("CAPTEXT_FFMPEG")
    if env and os.path.exists(env):
        return env
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    exe = _from_bundle("ffmpeg")            # запечён при сборке — без сети
    if exe:
        return exe
    for getter in (_from_static_ffmpeg, _from_imageio):
        exe = getter()
        if exe:
            _prepend_to_path(os.path.dirname(exe))
            log.info("ffmpeg resolved via %s: %s", getter.__name__, exe)
            return exe
    for d in _candidate_dirs():
        for name in ("ffmpeg", "ffmpeg.exe"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                _prepend_to_path(d)
                return p
    return None


@functools.lru_cache(maxsize=1)
def find_ffprobe() -> str | None:
    """ffprobe next to ffmpeg, from static_ffmpeg, or from PATH. Never raises."""
    if _OVERRIDE["ffprobe"] and os.path.exists(_OVERRIDE["ffprobe"]):
        return _OVERRIDE["ffprobe"]
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    exe = _from_bundle("ffprobe")
    if exe:
        return exe
    try:
        from static_ffmpeg import run as static_run  # type: ignore
        _ff, fp = static_run.get_or_fetch_platform_executables_else_raise()
        if fp and os.path.exists(fp):
            return fp
    except Exception:
        pass
    ff = find_ffmpeg()
    if ff:
        cand = os.path.join(os.path.dirname(ff),
                            "ffprobe.exe" if ff.endswith(".exe") else "ffprobe")
        if os.path.exists(cand):
            return cand
    return None


def _prepend_to_path(directory: str) -> None:
    if directory and directory not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


def ensure_ffmpeg(verbose: bool = True) -> str | None:
    """Call once at startup. Resolves + caches ffmpeg; returns path or None."""
    exe = find_ffmpeg()
    if verbose:
        if exe:
            log.info("FFmpeg: %s", exe)
        elif _HAS_AV:
            log.warning("FFmpeg не найден — используется встроенный декодер PyAV. "
                        "Экспорт видео будет недоступен: pip install static-ffmpeg")
        else:
            log.error("Нет ни ffmpeg, ни PyAV. Установите: pip install static-ffmpeg av")
    return exe


def ffmpeg_binary() -> str:
    """Strict accessor for features that genuinely need the executable (export)."""
    exe = find_ffmpeg()
    if not exe:
        raise AudioDecodeError(
            "FFmpeg не найден. Установите его одним из способов:\n"
            "  pip install static-ffmpeg     (рекомендуется, качается автоматически)\n"
            "  pip install imageio-ffmpeg\n"
            "  brew install ffmpeg  /  winget install Gyan.FFmpeg\n"
            "или укажите путь в переменной окружения CAPTEXT_FFMPEG."
        )
    return exe


def has_ffmpeg() -> bool:
    return find_ffmpeg() is not None


def backend_report() -> str:
    return (f"ffmpeg={'да' if has_ffmpeg() else 'нет'}, "
            f"ffprobe={'да' if find_ffprobe() else 'нет'}, "
            f"PyAV={'да' if _HAS_AV else 'нет'}")


# ------------------------------------------------------------------- probing
def ffprobe_duration(path: str) -> float:
    """Duration in seconds. ffprobe → PyAV → ffmpeg-decode fallback."""
    _require_file(path)
    exe = find_ffprobe()
    if exe:
        try:
            out = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, check=False, timeout=60).stdout.strip()
            val = float(out)
            if val > 0:
                return val
        except Exception as exc:
            log.debug("ffprobe duration failed: %s", exc)
    if _HAS_AV:
        try:
            with av.open(path) as c:  # type: ignore[union-attr]
                if c.duration:
                    return float(c.duration) / 1_000_000.0
                for s in list(c.streams.audio) + list(c.streams.video):
                    if s.duration and s.time_base:
                        return float(s.duration * s.time_base)
        except Exception as exc:
            log.debug("PyAV duration failed: %s", exc)
    try:                                   # last resort: decode and measure
        return len(decode_pcm(path, sr=8000)) / 8000.0
    except Exception as exc:
        raise AudioDecodeError(f"Не удалось определить длительность {path}: {exc}") from exc


def probe_video(path: str) -> tuple[int, int, float]:
    """Return (width, height, fps); (1080, 1920, 30.0) for audio-only inputs."""
    if _HAS_AV:
        try:
            with av.open(path) as c:  # type: ignore[union-attr]
                for s in c.streams.video:
                    fps = float(s.average_rate) if s.average_rate else 30.0
                    w = int(s.codec_context.width or 0)
                    h = int(s.codec_context.height or 0)
                    if w and h:
                        return w, h, (fps if 0 < fps < 1000 else 30.0)
        except Exception as exc:
            log.debug("PyAV probe failed: %s", exc)
    exe = find_ffprobe()
    if exe:
        try:
            out = subprocess.run(
                [exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height,avg_frame_rate", "-of", "csv=p=0:s=,", path],
                capture_output=True, text=True, check=False, timeout=60).stdout.strip()
            parts = out.split(",")
            if len(parts) >= 3 and parts[0].isdigit():
                num, _, den = parts[2].partition("/")
                try:
                    fps = float(num) / float(den or 1)
                except (ValueError, ZeroDivisionError):
                    fps = 30.0
                return int(parts[0]), int(parts[1]), (fps if 0 < fps < 1000 else 30.0)
        except Exception as exc:
            log.debug("ffprobe probe failed: %s", exc)
    return 1080, 1920, 30.0


# ------------------------------------------------------------------ decoding
def decode_pcm(path: str, sr: int = 16000, start: float | None = None,
               duration: float | None = None) -> np.ndarray:
    """Decode media to mono float32 PCM in [-1, 1] at ``sr`` Hz.

    Uses ffmpeg when available, otherwise PyAV. Raises :class:`AudioDecodeError`
    only when *both* backends fail.
    """
    _require_file(path)
    errors: list[str] = []
    if has_ffmpeg():
        try:
            return _decode_ffmpeg(path, sr, start, duration)
        except Exception as exc:
            errors.append(f"ffmpeg: {exc}")
            log.warning("ffmpeg decode failed, falling back to PyAV: %s", exc)
    if _HAS_AV:
        try:
            return _decode_pyav(path, sr, start, duration)
        except Exception as exc:
            errors.append(f"PyAV: {exc}")
    raise AudioDecodeError(
        "Не удалось декодировать аудио.\n" + "\n".join(errors) +
        "\n\nУстановите бэкенд: pip install static-ffmpeg av")


def _decode_ffmpeg(path: str, sr: int, start: float | None,
                   duration: float | None) -> np.ndarray:
    cmd = [ffmpeg_binary(), "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", path]
    if duration is not None:
        cmd += ["-t", f"{max(0.01, duration):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise AudioDecodeError(proc.stderr.decode("utf-8", "ignore")[-600:] or "empty output")
    return np.ascontiguousarray(np.frombuffer(proc.stdout, dtype=np.float32))


def _decode_pyav(path: str, sr: int, start: float | None,
                 duration: float | None) -> np.ndarray:
    """Pure-PyAV decode + resample to mono float32 @ sr."""
    if not _HAS_AV:
        raise AudioDecodeError("PyAV (av) не установлен")
    import av.audio.resampler  # type: ignore  # noqa: F401

    chunks: list[np.ndarray] = []
    with av.open(path) as container:  # type: ignore[union-attr]
        if not container.streams.audio:
            raise AudioDecodeError("в файле нет аудиодорожки")
        stream = container.streams.audio[0]
        stream.thread_type = "AUTO"
        if start:
            container.seek(int(max(0.0, start) / float(stream.time_base)),
                           stream=stream, any_frame=False, backward=True)
        resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono", rate=sr)
        t_end = None if duration is None else (start or 0.0) + duration
        for frame in container.decode(stream):
            ts = float(frame.pts * stream.time_base) if frame.pts is not None else None
            if start and ts is not None and ts + 0.05 < start:
                continue
            if t_end is not None and ts is not None and ts > t_end:
                break
            for out in resampler.resample(frame):
                arr = out.to_ndarray()
                chunks.append(arr.reshape(-1).astype(np.float32, copy=False))
        for out in resampler.resample(None):        # flush
            chunks.append(out.to_ndarray().reshape(-1).astype(np.float32, copy=False))

    if not chunks:
        raise AudioDecodeError("PyAV не вернул аудиосэмплов")
    pcm = np.concatenate(chunks)
    if pcm.dtype != np.float32:
        pcm = pcm.astype(np.float32)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak > 1.0:                                   # e.g. int-backed formats
        pcm = pcm / peak
    if duration is not None:
        pcm = pcm[: int(duration * sr)]
    return np.ascontiguousarray(pcm)


def _require_file(path: str) -> None:
    if not path or not os.path.exists(path):
        raise AudioDecodeError(f"Файл не найден: {path!r}")


# ------------------------------------------------------------------- helpers
def write_wav(path: str | Path, pcm: np.ndarray, sr: int = 16000) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ints = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(ints.tobytes())
    return str(p)


def windows(pcm: np.ndarray, sr: int, win_s: float = 5.0,
            positions: tuple[float, ...] = (0.10, 0.50, 0.65)) -> list[np.ndarray]:
    n = len(pcm)
    w = int(win_s * sr)
    if n <= w:
        return [pcm]
    out: list[np.ndarray] = []
    for p in positions:
        s = int(min(max(0, n * p), n - w))
        out.append(pcm[s:s + w])
    return out

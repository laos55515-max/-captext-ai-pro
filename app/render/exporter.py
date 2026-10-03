"""High-speed export: RGBA frames from SubtitleScene piped into FFmpeg."""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

from app.audio.extract import ffmpeg_binary, probe_video
from app.core.models import Project
from app.render.scene import SubtitleScene

log = logging.getLogger("captext.export")

ProgressCB = Optional[Callable[[float, str], None]]

FORMATS = ("mp4_h264", "mp4_hevc", "mov_prores4444_alpha", "webm_alpha")


@dataclass
class EncoderChoice:
    codec: str
    extra: list[str]


def pick_encoder(format_type: str, bitrate: str = "12M") -> EncoderChoice:
    system = platform.system()
    available = _encoders()
    if format_type == "mov_prores4444_alpha":
        return EncoderChoice("prores_ks", ["-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                                           "-alpha_bits", "16"])
    if format_type == "webm_alpha":
        return EncoderChoice("libvpx-vp9", ["-pix_fmt", "yuva420p", "-b:v", bitrate,
                                            "-auto-alt-ref", "0"])
    if format_type == "mp4_hevc":
        if system == "Darwin" and "hevc_videotoolbox" in available:
            return EncoderChoice("hevc_videotoolbox", ["-b:v", bitrate, "-tag:v", "hvc1"])
        if "hevc_nvenc" in available:
            return EncoderChoice("hevc_nvenc", ["-preset", "p5", "-b:v", bitrate, "-tag:v", "hvc1"])
        return EncoderChoice("libx265", ["-crf", "20", "-preset", "medium", "-tag:v", "hvc1"])
    # default h264
    if system == "Darwin" and "h264_videotoolbox" in available:
        return EncoderChoice("h264_videotoolbox", ["-b:v", bitrate])
    if "h264_nvenc" in available:
        return EncoderChoice("h264_nvenc", ["-preset", "p5", "-b:v", bitrate])
    if "h264_qsv" in available:
        return EncoderChoice("h264_qsv", ["-b:v", bitrate])
    return EncoderChoice("libx264", ["-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p"])


def _encoders() -> set[str]:
    exe = shutil.which("ffmpeg")
    if not exe:
        return set()
    out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True,
                         check=False).stdout
    return {line.split()[1] for line in out.splitlines()
            if line.startswith(" ") and len(line.split()) > 1}


class ExportCancelled(RuntimeError):
    pass


class VideoExporter:
    """Renders subtitles with the *same* scene used in preview, then muxes with FFmpeg."""

    def __init__(self, scene: SubtitleScene | None = None) -> None:
        self.scene = scene
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def export(self, project: Project, output_path: str, format_type: str = "mp4_h264",
               fps: float | None = None, bitrate: str = "12M",
               progress: ProgressCB = None) -> str:
        if format_type not in FORMATS:
            raise ValueError(f"format_type must be one of {FORMATS}")
        self._cancel = False
        ffmpeg = ffmpeg_binary()

        src = project.video_path
        w, h, src_fps = probe_video(src) if src else (project.width, project.height, project.fps)
        w = project.width or w
        h = project.height or h
        fps = float(fps or project.fps or src_fps or 30.0)
        duration = float(project.duration or (project.phrases[-1].end + 1.0
                                              if project.phrases else 1.0))
        n_frames = max(1, int(round(duration * fps)))

        scene = self.scene or SubtitleScene(project, w, h)
        scene.resize(w, h)

        alpha_only = format_type in ("mov_prores4444_alpha", "webm_alpha")
        enc = pick_encoder(format_type, bitrate)

        cmd: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if not alpha_only:
            cmd += ["-i", src]
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-"]
        if alpha_only:
            cmd += ["-map", "0:v", "-c:v", enc.codec, *enc.extra]
        else:
            cmd += ["-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[v]",
                    "-map", "[v]", "-map", "0:a?", "-c:a", "aac", "-b:a", "192k",
                    "-c:v", enc.codec, *enc.extra, "-shortest"]
        cmd += ["-r", f"{fps}", output_path]

        log.info("ffmpeg: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdin is not None
        try:
            for i in range(n_frames):
                if self._cancel:
                    raise ExportCancelled("export cancelled by user")
                proc.stdin.write(scene.render_rgba(i / fps))
                if progress and (i % 5 == 0 or i == n_frames - 1):
                    progress((i + 1) / n_frames, f"Кадр {i + 1}/{n_frames}")
            proc.stdin.close()
        except (BrokenPipeError, ExportCancelled):
            proc.kill()
            err = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
            raise RuntimeError(f"Экспорт прерван. FFmpeg: {err[-600:]}") from None
        finally:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()

        code = proc.wait()
        err = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
        if code != 0:
            raise RuntimeError(f"FFmpeg завершился с кодом {code}: {err[-800:]}")
        if progress:
            progress(1.0, "Готово")
        return output_path

    # -------------------------------------------------------------- subtitles
    @staticmethod
    def export_srt(project: Project, output_path: str) -> str:
        def ts(x: float) -> str:
            ms = int(round(x * 1000))
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        lines = []
        for i, ph in enumerate(project.phrases, 1):
            lines.append(f"{i}\n{ts(ph.start)} --> {ts(ph.end)}\n{ph.text}\n")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return output_path

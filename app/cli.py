"""Headless CLI:  python -m app.cli input.mp4 --lang uk --out out.mp4"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="captext", description="CapText AI Pro — CLI")
    ap.add_argument("media")
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--genre", default=None, help="force genre instead of auto detection")
    ap.add_argument("--zone", default="9:16", choices=["9:16", "16:9", "1:1", "4:3"])
    ap.add_argument("--project", default=None, help="write .ctp project json")
    ap.add_argument("--json", action="store_true", help="dump words+profile to stdout")
    ap.add_argument("--srt", default=None)
    ap.add_argument("--out", default=None, help="render a video")
    ap.add_argument("--format", default="mp4_h264",
                    choices=["mp4_h264", "mp4_hevc", "mov_prores4444_alpha", "webm_alpha"])
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    from app.core.pipeline import AutoPipeline, PipelineOptions
    from app.render.exporter import VideoExporter

    def progress(p: float, m: str) -> None:
        sys.stderr.write(f"\r[{p * 100:5.1f}%] {m:<60}")
        sys.stderr.flush()

    pipe = AutoPipeline(PipelineOptions(language=args.lang, model_size=args.model,
                                        safe_zone=args.zone, forced_genre=args.genre))
    project = pipe.run(args.media, progress=progress)
    sys.stderr.write("\n")

    if args.project:
        project.save(args.project)
        print("project:", args.project)
    if args.json:
        print(json.dumps({
            "profile": project.audio_profile.model_dump() if project.audio_profile else None,
            "phrases": [{"start": p.start, "end": p.end, "text": p.text} for p in project.phrases],
        }, ensure_ascii=False, indent=2))
    if args.srt:
        VideoExporter.export_srt(project, args.srt)
        print("srt:", args.srt)
    if args.out:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance() or QGuiApplication([])
        VideoExporter().export(project, args.out, args.format, progress=progress)
        sys.stderr.write("\n")
        print("video:", args.out)
        del app
    return 0


if __name__ == "__main__":
    # Первым оператором: в собранном приложении сюда может прийти spawn-воркер,
    # и он обязан уйти в multiprocessing ДО любого импорта Qt./настройки GUI.
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())

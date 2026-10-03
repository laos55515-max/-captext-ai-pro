"""Render a strip of demo frames per genre preset — no media file required.

Usage:  QT_QPA_PLATFORM=offscreen python tools/demo_frames.py docs/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter  # noqa: E402

from app.core.models import Phrase, Project, Word  # noqa: E402
from app.render.presets import AutoStyleEngine  # noqa: E402
from app.render.scene import SubtitleScene  # noqa: E402

TEXTS = {
    "rap": "ЧИТАЮ ПОВЕРХ БИТА",
    "deep_house_phonk": "ГЛУБЖЕ В НОЧЬ",
    "pop_dance": "танцуй со мной до утра",
    "chanson_acoustic": "гитара и тихий вечер",
    "speech_podcast": "сегодня разбираем главное",
}


def build(genre: str, w: int = 720, h: int = 1280) -> SubtitleScene:
    words = []
    t = 0.0
    for token in TEXTS[genre].split():
        dur = 0.28 + 0.04 * len(token)
        words.append(Word(text=token, start=t, end=t + dur, kick=t + 0.02,
                          is_keyword=len(token) > 6))
        t += dur + 0.05
    style = AutoStyleEngine.get_style_for_genre(genre)
    proj = Project(duration=t, width=w, height=h, phrases=[Phrase(words=words)],
                   global_style=style)
    return SubtitleScene(proj, w, h)


def main(outdir: str = "docs") -> None:
    app = QGuiApplication.instance() or QGuiApplication([])
    os.makedirs(outdir, exist_ok=True)
    for genre in TEXTS:
        scene = build(genre)
        canvas = QImage(scene.width, scene.height, QImage.Format_RGBA8888)
        grad = QLinearGradient(0, 0, scene.width, scene.height)
        grad.setColorAt(0.0, QColor("#101826"))
        grad.setColorAt(1.0, QColor("#2A1038"))
        p = QPainter(canvas)
        p.fillRect(canvas.rect(), grad)
        scene.paint(p, scene.project.duration * 0.6)
        p.end()
        path = os.path.join(outdir, f"demo_{genre}.png")
        canvas.save(path)
        print("wrote", path)
    del app


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()      # первым оператором: spawn-воркер не должен
    main(sys.argv[1] if len(sys.argv) > 1 else "docs")

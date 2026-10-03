"""CapText AI Pro — entry point: FFmpeg bootstrap, self-checks, crash-proof startup.

ПРАВИЛА ЭТОГО ФАЙЛА (иначе в собранном .app открываются дубликаты окон):

1. `multiprocessing.freeze_support()` — ПЕРВЫЙ исполняемый оператор внутри
   `if __name__ == "__main__":`. В упакованном приложении spawn-воркер
   приходит сюда как повторный запуск самого бинарника (`--multiprocessing-fork`);
   этот вызов обязан перехватить его до любого импорта Qt.

2. Сразу за ним — отсечка детских ролей (`_child_role()`), которая уводит процесс
   в `app.workers.entry` ДО импорта PySide6. Дочерние процессы приложения
   (например `python -m demucs.separate`) в сборке превращаются в повторный запуск
   бандла: если такой процесс дойдёт до `QApplication`, macOS откроет второе окно,
   а задача не выполнится.

3. `QApplication` создаётся ТОЛЬКО внутри `main()`. На уровне модуля Qt не трогаем:
   при spawn `__main__` импортируется целиком, и любой модульный GUI-код выполнится
   в воркере раньше защитного блока.
"""
from __future__ import annotations

import logging
import math
import multiprocessing
import os
import sys
import traceback

# Allow `python main.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
# Qt's own abort on fatal warnings is off by default; make sure nothing turns it on.
os.environ.pop("QT_FATAL_WARNINGS", None)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("captext")

CHILD_ENV = "CAPTEXT_CHILD_PROCESS"
WORKER_ONLY_ENV = "CAPTEXT_WORKER_ONLY"


# ---------------------------------------------------------- frozen / bundling
def is_frozen() -> bool:
    """True внутри собранного PyInstaller/Nuitka-приложения."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """Каталог ресурсов бандла (_MEIPASS), иначе папка скрипта."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def bootstrap_bundled_ffmpeg() -> None:
    """В сборке подхватить ffmpeg/ffprobe, положенные рядом с приложением.

    Spec кладёт их в `bin/` внутри бандла (и в `Contents/Resources/bin` на macOS),
    поэтому здесь достаточно выставить переменные, которые уже умеет искать
    `app.audio.extract.find_ffmpeg()`. Ничего не скачиваем и не падаем.
    """
    if not is_frozen():
        return
    candidates = [
        os.path.join(bundle_dir(), "bin"),
        os.path.join(os.path.dirname(bundle_dir()), "Resources", "bin"),
    ]
    for d in candidates:
        exe = os.path.join(d, "ffmpeg")
        if os.path.exists(exe):
            os.environ.setdefault("CAPTEXT_FFMPEG_DIR", d)
            os.environ.setdefault("CAPTEXT_FFMPEG", exe)
            log.info("ffmpeg из бандла: %s", exe)
            return


# --------------------------------------------------------------- child roles
def _child_role() -> str | None:
    """Роль дочернего процесса. `None` = обычный запуск GUI.

    Порядок проверок — от самых явных признаков к совместимости со старым кодом:

    * `CAPTEXT_WORKER_ONLY=1`          → процессу GUI запрещён категорически;
    * `--captext-worker <job.json>`    → headless-задача (пайплайн/экспорт);
    * `--captext-worker-module <mod>`  → аналог `python -m <mod>` внутри бандла;
    * `-m <mod>` в argv                → тот же смысл: так выглядит старый вызов
      `[sys.executable, "-m", "demucs.separate", ...]`, который в сборке
      перезапускал приложение;
    * `CAPTEXT_CHILD_PROCESS=1`        → воркер без аргументов (GUI запрещён).
    """
    argv = sys.argv[1:]
    if os.environ.get(WORKER_ONLY_ENV) == "1":
        return "worker-only"
    if "--captext-worker" in argv:
        return "job"
    if "--captext-worker-module" in argv:
        return "module"
    if argv[:1] == ["-m"]:
        return "module"
    if os.environ.get(CHILD_ENV) == "1":
        return "worker-only"
    return None


def run_child_process(role: str) -> int:
    """Тело дочернего процесса. Qt сюда не попадает ни при каких условиях."""
    from app.workers.entry import run_worker_role

    return run_worker_role(role)


# --------------------------------------------------------------- ffmpeg boot
def bootstrap_ffmpeg() -> str | None:
    """Resolve an FFmpeg binary before anything touches media.

    Order: PATH → static_ffmpeg → imageio_ffmpeg → known install dirs.
    Returns the path, or None if we must rely on the PyAV fallback.
    Never raises: a missing FFmpeg degrades features, it must not kill the app.
    """
    try:
        from app.audio.extract import backend_report, ensure_ffmpeg
    except Exception as exc:                      # pragma: no cover
        log.error("Не удалось импортировать app.audio.extract: %s", exc)
        return None
    try:
        exe = ensure_ffmpeg(verbose=True)
    except Exception as exc:                      # pragma: no cover
        log.warning("FFmpeg bootstrap error: %s", exc)
        exe = None
    log.info("Аудио-бэкенды: %s", backend_report())
    if exe is None:
        log.warning("FFmpeg не найден. Декодирование пойдёт через PyAV; "
                    "для экспорта видео выполните: pip install static-ffmpeg")
    return exe


# ------------------------------------------------------------- crash guard
def install_exception_hook(show_dialog: bool = True) -> None:
    """Route *any* uncaught exception (main or worker thread) to the log/UI.

    Without this, an exception raised inside a QThread callback can tear down
    the interpreter (`zsh: abort`). With it, the app stays alive and reports.
    """
    def handle(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("UNCAUGHT EXCEPTION:\n%s", text)
        if not show_dialog:
            return
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                box = QMessageBox(QMessageBox.Critical, "Непредвиденная ошибка",
                                  f"{exc_type.__name__}: {exc}")
                box.setDetailedText(text)
                box.exec()
        except Exception:
            pass

    sys.excepthook = handle

    try:                                          # Python 3.8+: thread exceptions
        import threading

        def thread_hook(args) -> None:
            handle(args.exc_type, args.exc_value, args.exc_traceback)
        threading.excepthook = thread_hook
    except Exception:
        pass


def set_window_icon(app) -> None:
    """Иконка окна/таскбара. На macOS её задаёт система (CFBundleIconFile в .app),
    поэтому там вызов безвреден; на Windows/Linux это единственный способ показать
    иконку в панели задач. Никогда не роняет запуск."""
    try:
        from PySide6.QtGui import QIcon

        for cand in (os.path.join(bundle_dir(), "AppIcon.png"),
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "AppIcon.png")):
            if os.path.exists(cand):
                app.setWindowIcon(QIcon(cand))
                log.info("иконка окна: %s", cand)
                return
        log.debug("AppIcon.png не найден — окно будет с иконкой по умолчанию")
    except Exception as exc:                      # pragma: no cover
        log.debug("не удалось поставить иконку окна: %s", exc)


def install_qt_message_handler() -> None:
    """Send Qt's own warnings to the logger instead of stderr noise/aborts."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
                  QtMsgType.QtWarningMsg: logging.WARNING,
                  QtMsgType.QtCriticalMsg: logging.ERROR,
                  QtMsgType.QtFatalMsg: logging.CRITICAL}

        def handler(mode, context, message) -> None:
            logging.getLogger("qt").log(levels.get(mode, logging.INFO), "%s", message)

        qInstallMessageHandler(handler)
    except Exception:
        pass


# ------------------------------------------------------------- self checks
def self_check() -> None:
    """Unit-asserts for the spring math and the pydantic models. Fails fast and loudly."""
    from app.core.models import AudioProfile, Phrase, Project, SpringConfig, Style, Word
    from app.render.physics import (clamp, cubic_bezier_ease, get_word_transform, kick_pulse,
                                    spring)

    # --- spring: boundary conditions
    assert spring(0.0) == 0.0
    assert spring(-1.0) == 0.0
    assert abs(spring(10.0, 180, 1, 14) - 1.0) < 1e-3, "spring must settle at 1.0"

    # underdamped must overshoot
    over = max(spring(t / 1000.0, 320, 1.0, 16) for t in range(0, 2000))
    assert over > 1.0, f"underdamped spring must overshoot, got {over}"
    z = SpringConfig(k=320, m=1.0, c=16).zeta
    assert z < 1.0, f"expected underdamped, zeta={z}"

    # critically damped: no overshoot
    crit_c = 2 * math.sqrt(180 * 1.0)
    crit = max(spring(t / 1000.0, 180, 1.0, crit_c) for t in range(0, 3000))
    assert crit <= 1.0 + 1e-6, f"critically damped must not overshoot, got {crit}"

    # overdamped: monotone rise, no overshoot
    prev = -1.0
    for i in range(0, 2000, 5):
        v = spring(i / 1000.0, 180, 1.0, 120)
        assert v >= prev - 1e-9, "overdamped response must be monotone"
        assert v <= 1.0 + 1e-6
        prev = v

    # --- easing
    assert cubic_bezier_ease(0.0) == 0.0 and cubic_bezier_ease(1.0) == 1.0
    assert 0.0 < cubic_bezier_ease(0.5) < 1.0
    assert clamp(5.0) == 1.0 and clamp(-5.0) == 0.0
    assert kick_pulse(1.0, 1.0, 0.1, 0.12) == 0.1
    assert kick_pulse(0.5, 1.0) == 0.0

    # --- models
    w = Word(text="тест", start=1.0, end=0.5)          # invalid order gets repaired
    assert w.end > w.start
    style = Style()
    assert style.spring.zeta > 0
    ph = Phrase(words=[Word(text="a", start=0.0, end=0.4),
                       Word(text="b", start=0.4, end=0.9)])
    assert ph.text == "a b" and ph.start == 0.0 and abs(ph.end - 0.9) < 1e-9
    prof = AudioProfile(genre="rap", probabilities={"rap": 1.0}, bpm=92.0)
    proj = Project(video_path="x.mp4", duration=10.0, audio_profile=prof, phrases=[ph],
                   global_style=style)
    restored = Project.model_validate(proj.model_dump(mode="json"))
    assert restored.phrases[0].text == "a b"
    assert restored.audio_profile and restored.audio_profile.genre == "rap"

    # --- transform sanity
    tr = get_word_transform(0.0001, ph.words[0], style, -1.0)
    assert tr["opacity"] <= 0.05 and tr["scale"] < 0.75
    tr2 = get_word_transform(5.0, ph.words[0], style, -1.0)
    assert tr2["opacity"] == 0.0 or tr2["opacity"] < 0.01   # long gone

    # --- decoding backend is at least resolvable (no exception thrown)
    from app.audio.extract import backend_report, find_ffmpeg
    find_ffmpeg()
    assert isinstance(backend_report(), str)

    log.info("self-check passed: spring math, easing, data models and decoder lookup")


# -------------------------------------------------------------------- main
def configure_platform() -> None:
    """Платформенные настройки, которые обязаны быть выставлены ДО QApplication.

    На macOS (в т.ч. Apple Silicon M1/M2/M3) выбираем нативный AVFoundation-бэкенд:
    именно из-за ffmpeg-бэкенда предпросмотр оставался чёрным/пустым.
    """
    try:
        from app.ui.player import configure_media_backend
        configure_media_backend()
    except Exception as exc:                      # pragma: no cover
        log.debug("media backend config: %s", exc)


def _media_argument(argv: list[str]) -> str | None:
    """Первый существующий путь из argv (открытие файла двойным кликом / drag-n-drop)."""
    for a in argv:
        if a and not a.startswith("-") and os.path.exists(a):
            return a
    return None


def main(argv: list[str] | None = None) -> int:
    """GUI-ветка. Единственное место, где создаётся QApplication."""
    argv = list(sys.argv[1:] if argv is None else argv)

    install_exception_hook(show_dialog=False)     # dialogs only after QApplication exists
    bootstrap_bundled_ffmpeg()
    configure_platform()
    bootstrap_ffmpeg()

    try:
        self_check()
    except AssertionError as exc:
        log.error("SELF-CHECK FAILED: %s", exc)
        return 3

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        log.error("PySide6 не установлен. Выполните: pip install -r requirements.txt")
        return 2

    install_qt_message_handler()

    # Новый интерфейс (CapCut-подобный). Старое окно остаётся как app.ui.main_window.
    try:
        from app.ui.ui_main import MainWindow
    except Exception:
        log.exception("Не удалось загрузить новый UI — откатываюсь на классическое окно")
        from app.ui.main_window import MainWindow

    app = QApplication.instance()
    if app is None:
        app = QApplication([sys.argv[0], *argv])
    else:                                         # повторный запуск в том же процессе
        log.warning("QApplication уже существует — переиспользую существующий экземпляр")
    app.setApplicationName("CapText AI Pro")
    app.setOrganizationName("CapText")
    set_window_icon(app)
    install_exception_hook(show_dialog=True)

    try:
        win = MainWindow()
    except Exception:
        log.exception("Не удалось создать главное окно")
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "Ошибка запуска", traceback.format_exc()[-1500:])
        return 4

    media = _media_argument(argv)
    if media:
        win.load_media(media)

    win.show()
    log.info("GUI-режим: окно создано (pid=%s, frozen=%s)", os.getpid(), is_frozen())
    return app.exec()


if __name__ == "__main__":
    # 1) ПЕРВАЯ строка блока. Spawn-воркер (--multiprocessing-fork) уходит в
    #    spawn_main() здесь и никогда не доходит до импорта Qt.
    multiprocessing.freeze_support()

    # 2) Отсечка дочерних ролей: воркер никогда не поднимает GUI.
    _role = _child_role()
    if _role is not None:
        raise SystemExit(run_child_process(_role))

    # 3) Обычный запуск: GUI.
    raise SystemExit(main())

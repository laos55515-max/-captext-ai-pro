"""Роли дочерних процессов CapText AI Pro.

Сюда процесс попадает ТОЛЬКО из точки входа (`main.py`) — до импорта PySide6,
поэтому в этом модуле гарантированно нет ни `QApplication`, ни окна, ни виджетов.
Импортировать отсюда GUI-модули запрещено: это вернёт баг с дубликатами окон.

Поддерживаемые роли:

* ``module`` — аналог ``python -m <module>`` внутри собранного приложения
  (используется, например, для ``demucs.separate``);
* ``job``    — headless-задача из JSON-файла (пайплайн / экспорт / диагностика);
* ``worker-only`` — процессу запрещён GUI, но задача не указана (ошибка-подсказка).

Прогресс headless-задач пишется в stderr строками JSON, чтобы родитель (GUI) мог его
показывать, не импортируя ничего из Qt:

    {"event": "progress", "p": 0.42, "message": "Распознавание…"}
    {"event": "result",   "project": "/tmp/out.ctp"}
    {"event": "error",    "message": "..."}
"""
from __future__ import annotations

import json
import logging
import os
import runpy
import sys
import traceback
from pathlib import Path

log = logging.getLogger("captext.workers")

PROGRESS_PREFIX = "@@captext-progress@@ "


# ------------------------------------------------------------------ helpers
def _emit(event: str, **fields) -> None:
    """Одна JSON-строка прогресса в stderr (родитель парсит её, GUI не нужен)."""
    try:
        sys.stderr.write(PROGRESS_PREFIX + json.dumps(
            {"event": event, **fields}, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except Exception:                                 # прагма: диагностика не должна падать
        pass


def assert_no_gui(where: str) -> None:
    """Гарантия, что в этом процессе нет GUI.

    Нюанс собранного приложения: PyInstaller-овский runtime-hook Qt предзагружает
    ``PySide6``/``PySide6.QtCore`` в КАЖДЫЙ процесс бандла — это нормально и окна
    не создаёт. Опасен только ``PySide6.QtWidgets`` вместе с живым ``QApplication``:
    именно он открывает второе окно и вторую иконку в Dock.
    """
    qt_widgets = sys.modules.get("PySide6.QtWidgets")
    if qt_widgets is None:
        return
    try:
        instance = qt_widgets.QApplication.instance()
    except Exception:                                 # прагма: диагностика не должна падать
        return
    if instance is not None:
        log.error("РЕГРЕСС: в воркере (%s) уже есть QApplication — GUI-код не должен "
                  "исполняться в дочернем процессе", where)


def _friendly_module_error(module: str, exc: BaseException) -> str:
    if isinstance(exc, ModuleNotFoundError):
        pkg = (module.split(".")[0] or module)
        return (f"Модуль «{module}» не найден в этом окружении "
                f"(нет пакета «{pkg}»). Установите его: pip install {pkg}")
    return f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------- roles
def run_module(module: str, argv: list[str]) -> int:
    """Эквивалент `python -m module argv` внутри упакованного приложения."""
    os.environ.setdefault("CAPTEXT_CHILD_PROCESS", "1")
    # Путь к бандлу уже в sys.path; добавляем cwd, как это делает python -m.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    assert_no_gui(f"модуль {module}")                 # защита от регресса
    sys.argv = [module, *argv]
    log.info("worker: python -m %s %s", module, " ".join(argv))
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:                         # модуль завершился сам
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except BaseException as exc:                      # noqa: BLE001
        log.error("worker: модуль %s упал:\n%s", module,
                  "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        _emit("error", message=_friendly_module_error(module, exc))
        return 1
    return 0


def run_job(payload: dict) -> int:
    """Headless-задача без GUI-потока. `action`: pipeline | capabilities | export."""
    # Воркеру GUI не нужен; если задаче понадобится Qt (экспорт через QImage),
    # пусть он будет строго offscreen — окно не появится ни при каких условиях.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    assert_no_gui("headless job")
    action = str(payload.get("action", "pipeline")).lower()
    log.info("worker: задача action=%s", action)

    def progress(p: float, message: str) -> None:
        _emit("progress", p=round(float(p), 4), message=str(message))

    try:
        if action == "capabilities":
            from app.audio.audio_processor import AudioProcessor
            caps = AudioProcessor().capabilities()
            print(json.dumps(caps, ensure_ascii=False))
            _emit("result", capabilities=caps)
            return 0

        if action in ("pipeline", "analyze", "auto"):
            from app.core.pipeline import AutoPipeline, PipelineOptions

            media = payload["media"]
            opts = PipelineOptions(**{
                k: v for k, v in dict(payload.get("options") or {}).items()
                if k in PipelineOptions.__dataclass_fields__
            })
            project = AutoPipeline(opts).run(media, progress=progress)

            out = payload.get("out_project")
            if out:
                project.save(out)
            _emit("result", project=out, phrases=len(project.phrases),
                  duration=project.duration)
            return 0

        if action == "export":
            from app.render.exporter import VideoExporter

            project = _load_project(payload["project"])
            out = payload["out"]
            exporter = VideoExporter()
            exporter.export(project, out, payload.get("format", "mp4_h264"),
                            progress=progress)
            _emit("result", video=out)
            return 0

        _emit("error", message=f"неизвестное действие: {action}")
        return 2

    except BaseException as exc:                      # noqa: BLE001
        log.error("worker: задача упала:\n%s",
                  "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        _emit("error", message=f"{type(exc).__name__}: {exc}")
        return 1


def _load_project(path: str):
    from app.core.models import Project
    return Project.load(path)


def run_job_file(path: str) -> int:
    """Задача из JSON-файла (`--captext-worker job.json`)."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("worker: не читается job-файл %s: %s", path, exc)
        _emit("error", message=f"job-файл не читается: {exc}")
        return 2
    return run_job(payload)


def run_worker_role(role: str, argv: list[str] | None = None) -> int:
    """Единая точка входа дочернего процесса (вызывается из main.py)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    log.info("worker: роль=%s argv=%s frozen=%s", role, argv, bool(getattr(sys, "frozen", False)))
    assert_no_gui(f"роль {role}")

    if role == "module":
        if "--captext-worker-module" in argv:
            i = argv.index("--captext-worker-module")
            module, rest = argv[i + 1], argv[i + 2:]
        elif "-m" in argv:                            # совместимость со старым вызовом
            i = argv.index("-m")
            module, rest = argv[i + 1], argv[i + 2:]
        else:
            _emit("error", message="не указан модуль для запуска")
            return 2
        return run_module(module, rest)

    if role == "job":
        if "--captext-worker" in argv:
            i = argv.index("--captext-worker")
            if i + 1 < len(argv):
                return run_job_file(argv[i + 1])
        job = os.environ.get("CAPTEXT_WORKER_JOB")
        if job:
            return run_job_file(job)
        _emit("error", message="не указан job-файл (--captext-worker <job.json>)")
        return 2

    if role == "worker-only":
        job = os.environ.get("CAPTEXT_WORKER_JOB")
        if job:
            return run_job_file(job)
        _emit("error", message="CAPTEXT_WORKER_ONLY=1: GUI запрещён, но задача не передана")
        log.error("Процесс помечен как воркер, но job не задан. "
                  "Передайте --captext-worker <job.json> или CAPTEXT_WORKER_JOB.")
        return 2

    _emit("error", message=f"неизвестная роль воркера: {role}")
    return 2


__all__ = ["run_worker_role", "run_module", "run_job", "run_job_file",
           "assert_no_gui", "PROGRESS_PREFIX"]

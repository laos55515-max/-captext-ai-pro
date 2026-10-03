"""Безопасный запуск тяжёлых задач в QThread.

Правила, которые исключают падение процесса и `QThread::wait: Thread tried to wait on itself`:
  * Worker.run() ловит BaseException и превращает её в сигнал `failed`;
  * все сигналы доставляются в GUI-поток (Qt.QueuedConnection);
  * поток останавливается только через quit(), wait() вызывается лишь из GUI-потока;
  * очистка — в QThread.finished, объекты освобождаются deleteLater().
"""
from __future__ import annotations

import logging
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

log = logging.getLogger("captext.ui.workers")


def friendly_error(exc: BaseException) -> str:
    """Человеческое сообщение + подсказка, что установить/сделать."""
    head = f"{type(exc).__name__}: {exc}"
    text = str(exc).lower()
    hint = ""
    if "ffmpeg" in text:
        hint = "\n\nПодсказка: pip install static-ffmpeg av"
    elif "demucs" in text:
        hint = "\n\nПодсказка: pip install demucs  (или снимите галочку «Изолировать вокал»)"
    elif "stable_whisper" in text or "stable-ts" in text:
        hint = "\n\nПодсказка: pip install stable-ts"
    elif "faster_whisper" in text or "faster-whisper" in text:
        hint = "\n\nПодсказка: pip install faster-whisper"
    elif "out of memory" in text or isinstance(exc, MemoryError):
        hint = ("\n\nПодсказка: не хватает памяти GPU/ОЗУ. Выберите модель поменьше "
                "(medium или small) во вкладке «Текст и распознавание».")
    elif "cuda" in text:
        hint = "\n\nПодсказка: GPU недоступен — приложение продолжит работу на CPU."
    elif isinstance(exc, (ImportError, ModuleNotFoundError)):
        hint = "\n\nПодсказка: pip install -r requirements.txt"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
    return f"{head}{hint}\n\n---\n{tb}"


class Worker(QObject):
    progress = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    done = Signal()

    def __init__(self, fn: Callable, *args, **kwargs) -> None:
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, progress=self._emit_progress, **self._kwargs)
        except BaseException as exc:                # noqa: BLE001 — не должно улететь наружу
            log.exception("фоновая задача упала")
            self.failed.emit(friendly_error(exc))
        else:
            self.succeeded.emit(result)
        finally:
            self.done.emit()

    def _emit_progress(self, p: float, msg: str) -> None:
        try:
            self.progress.emit(float(p), str(msg))
        except Exception:
            pass


class JobRunner(QObject):
    """Один одновременный фоновый job с сигналами для UI."""

    progress = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    state_changed = Signal(bool)          # True = занят

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: Worker | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, fn: Callable, *args, **kwargs) -> bool:
        if self.busy:
            return False
        thread = QThread()
        worker = Worker(fn, *args, **kwargs)
        worker.moveToThread(thread)
        self._thread, self._worker = thread, worker

        thread.started.connect(worker.run)
        worker.progress.connect(self.progress, Qt.QueuedConnection)
        worker.succeeded.connect(self.succeeded, Qt.QueuedConnection)
        worker.failed.connect(self.failed, Qt.QueuedConnection)
        worker.done.connect(thread.quit, Qt.QueuedConnection)
        thread.finished.connect(self._cleanup, Qt.QueuedConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self.state_changed.emit(True)
        thread.start()
        return True

    @Slot()
    def _cleanup(self) -> None:
        self._thread = None
        self._worker = None
        self.state_changed.emit(False)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Вызывать ТОЛЬКО из GUI-потока (например, в closeEvent)."""
        thread = self._thread
        if thread is None:
            return
        thread.requestInterruption()
        thread.quit()
        if not thread.wait(timeout_ms):
            thread.terminate()
            thread.wait(2000)

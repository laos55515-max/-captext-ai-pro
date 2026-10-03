"""Безопасный запуск python-модулей (CLI-утилиты вроде demucs) из GUI-приложения.

Зачем этот модуль
-----------------
В собранном приложении (PyInstaller/Nuitka, macOS `.app`) `sys.executable` указывает
на сам бандл. Поэтому привычный вызов

    subprocess.run([sys.executable, "-m", "demucs.separate", ...])

перезапускает **всё приложение целиком**: в Dock появляется второе окно, а demucs
не выполняется (задача «висит»/падает). Именно это выглядит как «бесконечные
дубликаты окон».

Решение
-------
Единственная точка запуска python-модулей — `run_python_module()`:

* в исходниках: `python -m <module> args...` (как раньше);
* в сборке: `CapText.app/Contents/MacOS/CapText --captext-worker-module <module> args...`.
  Точка входа (`main.py`) распознаёт флаг **до импорта Qt** и выполняет
  `runpy.run_module(module, run_name="__main__")` — то есть процесс ведёт себя как
  `python -m`, но никогда не создаёт `QApplication`.

Дополнительно каждый дочерний процесс получает `CAPTEXT_CHILD_PROCESS=1` — второй
предохранитель на случай, если флаги перехватит macOS (argv emulation) или Python.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Iterable, Mapping, Sequence

log = logging.getLogger("captext.procutil")

CHILD_ENV = "CAPTEXT_CHILD_PROCESS"
WORKER_MODULE_FLAG = "--captext-worker-module"
WORKER_JOB_FLAG = "--captext-worker"


def is_frozen() -> bool:
    """True внутри собранного приложения."""
    return bool(getattr(sys, "frozen", False))


def worker_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Окружение дочернего процесса: GUI запрещён, Qt-плагины не наследуются."""
    env = os.environ.copy()
    env[CHILD_ENV] = "1"
    env.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    env.pop("QT_FATAL_WARNINGS", None)
    env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def python_module_command(module: str,
                          args: Sequence[str] = ()) -> list[str]:
    """Команда запуска python-модуля — корректная и в сборке, и в исходниках."""
    args = [str(a) for a in args]
    if is_frozen():
        return [sys.executable, WORKER_MODULE_FLAG, module, *args]
    return [sys.executable or "python3", "-m", module, *args]


def run_python_module(module: str,
                      args: Sequence[str] = (),
                      *,
                      timeout: float | None = None,
                      capture_output: bool = True,
                      check: bool = False,
                      extra_env: Mapping[str, str] | None = None,
                      cwd: str | None = None) -> subprocess.CompletedProcess:
    """Запустить `python -m module` и вернуть результат (без GUI в дочернем процессе)."""
    cmd = python_module_command(module, args)
    log.info("Запуск python-модуля: %s", " ".join(cmd[:4] + (["…"] if len(cmd) > 4 else [])))
    log.debug("Полная команда: %s", cmd)
    return subprocess.run(
        cmd,
        capture_output=capture_output,
        text=capture_output,
        check=check,
        timeout=timeout,
        env=worker_env(extra_env),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
    )


def popen_python_module(module: str,
                        args: Sequence[str] = (),
                        *,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        extra_env: Mapping[str, str] | None = None,
                        cwd: str | None = None) -> subprocess.Popen:
    """Как `run_python_module`, но с потоковым выводом (для длинных задач/рендера)."""
    cmd = python_module_command(module, args)
    return subprocess.Popen(cmd, stdout=stdout, stderr=stderr, stdin=stdin,
                            text=True, env=worker_env(extra_env), cwd=cwd)


def main_is_frozen_entry() -> bool:
    """True, если текущий процесс — дочерний воркер приложения (не GUI)."""
    return os.environ.get(CHILD_ENV) == "1" or bool(getattr(sys, "frozen", False)) and (
        WORKER_MODULE_FLAG in sys.argv or WORKER_JOB_FLAG in sys.argv
    )


__all__ = [
    "CHILD_ENV", "WORKER_MODULE_FLAG", "WORKER_JOB_FLAG",
    "is_frozen", "worker_env", "python_module_command",
    "run_python_module", "popen_python_module",
]

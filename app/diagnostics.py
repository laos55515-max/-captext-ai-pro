"""Диагностика окружения CapText AI Pro: ассеты, движки, ffmpeg.

Зачем модуль
------------
В собранном приложении половина ошибок выглядит одинаково: «нет файла/модели».
Этот модуль отвечает на вопрос «что реально доступно в ЭТОМ окружении» —
и в исходниках, и внутри `.app`, где пути и наличие файлов отличаются.

Запуск:

    python -m app.diagnostics                       # отчёт в stdout
    python -m app.diagnostics --json out.json       # отчёт в файл
    python -m app.diagnostics --strict              # код возврата 1, если критичное отсутствует

    # внутри собранного приложения (без GUI):
    CapText --captext-worker-module app.diagnostics

Что проверяется:

* ``faster_whisper``: наличие и **загрузка** ``assets/silero_vad_v6.onnx``
  (именно здесь была ошибка ``NO_SUCHFILE`` в бандле);
* ``stable_whisper`` / ``demucs`` / ``torch`` / ``torchaudio`` — доступность движков;
* ffmpeg/ffprobe — путь, версия, откуда взяты;
* аудио-бэкенды приложения (``app.audio.extract.backend_report``);
* быстрый self-check моделей и математики (``main.self_check`` не тянем — он требует Qt-независимых
  модулей, поэтому здесь только то, что безопасно в воркере).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


def _status(ok: bool, warn: bool = False) -> str:
    return OK if ok else (WARN if warn else FAIL)


# --------------------------------------------------------------- subchecks
def check_runtime() -> dict:
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "bundle_dir": getattr(sys, "_MEIPASS", None),
        "cwd": os.getcwd(),
        "child_role": os.environ.get("CAPTEXT_CHILD_PROCESS"),
    }


def check_faster_whisper() -> dict:
    """Самое важное: ассеты faster-whisper (VAD-модель) внутри бандла."""
    out: dict = {"package": "faster_whisper", "asset": None, "asset_exists": False,
                 "asset_bytes": 0, "model_load": None, "status": FAIL}
    try:
        import faster_whisper
        from faster_whisper.utils import get_assets_path
    except Exception as exc:                                # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["version"] = getattr(faster_whisper, "__version__", "?")
    assets = Path(get_assets_path())
    model = assets / "silero_vad_v6.onnx"
    out["asset"] = str(model)
    out["assets_dir_exists"] = assets.is_dir()
    out["asset_exists"] = model.exists()
    if model.exists():
        out["asset_bytes"] = model.stat().st_size

    # реальная загрузка модели (onnxruntime) — то, что делал VAD при NO_SUCHFILE
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        out["model_load"] = f"onnxruntime {ort.__version__}: сессия создана, входов {len(sess.get_inputs())}"
        out["status"] = OK
    except Exception as exc:                                # noqa: BLE001
        out["model_load"] = f"{type(exc).__name__}: {exc}"
        out["status"] = OK if out["asset_exists"] else FAIL
    return out


def check_engine(name: str, attr: str | None = None) -> dict:
    res: dict = {"package": name, "status": FAIL}
    try:
        mod = __import__(name)
        res["version"] = getattr(mod, "__version__", "?")
        if attr:
            getattr(mod, attr)
        res["status"] = OK
    except Exception as exc:                                # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
    return res


def check_ffmpeg() -> dict:
    res: dict = {"ffmpeg": None, "ffprobe": None, "version": None, "status": WARN}
    try:
        from app.audio.extract import find_ffmpeg, find_ffprobe
        ff, fp = find_ffmpeg(), find_ffprobe()
    except Exception as exc:                                # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
        ff = shutil.which("ffmpeg")
        fp = shutil.which("ffprobe")
    res["ffmpeg"], res["ffprobe"] = ff, fp
    if ff:
        try:
            out = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=15)
            res["version"] = (out.stdout or "").splitlines()[0][:80]
            res["status"] = OK if fp else WARN
        except Exception as exc:                            # noqa: BLE001
            res["error"] = f"запуск ffmpeg не удался: {exc}"
    res["env_CAPTEXT_FFMPEG"] = os.environ.get("CAPTEXT_FFMPEG")
    res["env_CAPTEXT_FFMPEG_DIR"] = os.environ.get("CAPTEXT_FFMPEG_DIR")
    return res


def check_app_backends() -> dict:
    try:
        from app.audio.extract import backend_report
        return {"report": backend_report(), "status": OK}
    except Exception as exc:                                # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "status": WARN}


def check_optional_plugins() -> dict:
    """Qt-плагины и медиа-бэкенд: в бандле они лежат рядом с бинарником."""
    res: dict = {"status": WARN}
    try:
        from app.ui.player import multimedia_status
        res["multimedia_status"] = multimedia_status()
        res["status"] = OK
    except Exception as exc:                                # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
    return res


# ------------------------------------------------------------------ отчёт
def collect_report() -> dict:
    report = {
        "runtime": check_runtime(),
        "faster_whisper": check_faster_whisper(),
        "stable_whisper": check_engine("stable_whisper"),
        "demucs": check_engine("demucs"),
        "torch": check_engine("torch"),
        "torchaudio": check_engine("torchaudio"),
        "ctranslate2": check_engine("ctranslate2"),
        "onnxruntime": check_engine("onnxruntime"),
        "av": check_engine("av"),
        "librosa": check_engine("librosa"),
        "ffmpeg": check_ffmpeg(),
        "app_backends": check_app_backends(),
        "qt_media": check_optional_plugins(),
    }
    critical = [report["faster_whisper"], report["ffmpeg"]]
    missing = [c.get("package", "faster_whisper") for c in critical
               if c.get("status") == FAIL and c.get("package", "faster_whisper") == "faster_whisper"]
    if report["faster_whisper"].get("status") != OK:
        missing.append("faster_whisper.assets")
    report["critical_missing"] = missing
    return report


def print_report(report: dict) -> None:
    r = report["runtime"]
    print("=" * 78)
    print("CapText AI Pro — диагностика окружения")
    print("=" * 78)
    print(f"Python           : {r['python']}  ({r['executable']})")
    print(f"frozen (бандл)   : {r['frozen']}" + (f"  _MEIPASS={r['bundle_dir']}" if r["bundle_dir"] else ""))
    print(f"роль процесса    : {r['child_role'] or 'обычный запуск (GUI разрешён)'}")
    print()
    for key, title in (("faster_whisper", "faster-whisper (ассеты + VAD-модель)"),
                       ("stable_whisper", "stable-ts"), ("demucs", "Demucs"),
                       ("torch", "PyTorch"), ("torchaudio", "torchaudio"),
                       ("ctranslate2", "CTranslate2"), ("onnxruntime", "onnxruntime"),
                       ("av", "PyAV"), ("librosa", "librosa")):
        item = report[key]
        mark = {"OK": "✓", "WARN": "!", "FAIL": "✗"}[item["status"]]
        extra = f" {item.get('version', '')}".rstrip()
        print(f"  [{mark}] {title:<42}{extra}")
        if key == "faster_whisper":
            print(f"        модель: {item.get('asset')}")
            print(f"        файл {'есть' if item.get('asset_exists') else 'ОТСУТСТВУЕТ'}"
                  f" ({item.get('asset_bytes', 0)} байт) | загрузка: {item.get('model_load')}")
        if item.get("error"):
            print(f"        {item['error']}")
    ff = report["ffmpeg"]
    mark = {"OK": "✓", "WARN": "!", "FAIL": "✗"}[ff["status"]]
    print(f"  [{mark}] FFmpeg / FFprobe")
    print(f"        ffmpeg : {ff['ffmpeg']}")
    print(f"        ffprobe: {ff['ffprobe']}")
    if ff.get("version"):
        print(f"        {ff['version']}")
    print(f"  [i] аудио-бэкенды: {report['app_backends'].get('report') or report['app_backends'].get('error')}")
    print(f"  [i] Qt-медиа: {report['qt_media'].get('multimedia_status') or report['qt_media'].get('error')}")
    print()
    if report["critical_missing"]:
        print("КРИТИЧНО ОТСУТСТВУЕТ:", ", ".join(report["critical_missing"]))
    else:
        print("Критичные ассеты и ffmpeg на месте.")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="captext-diagnostics",
                                 description="Диагностика ассетов и движков CapText AI Pro")
    ap.add_argument("--json", default=None, help="сохранить отчёт в файл")
    ap.add_argument("--strict", action="store_true",
                    help="код возврата 1, если отсутствует критичный ассет (faster-whisper VAD)")
    args = ap.parse_args(argv)

    report = collect_report()
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"отчёт сохранён: {args.json}")
    if args.strict and report["critical_missing"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

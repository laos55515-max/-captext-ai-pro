# -*- mode: python ; coding: utf-8 -*-
"""captext_ai_pro.spec — сборка CapText AI Pro (macOS .app + onedir).

Почему именно так (правки, которые лечат дубликаты окон и падения):

1. ``argv_emulation=False`` в BUNDLE — КРИТИЧНО. Иначе macOS перехватывает и
   переписывает argv дочерних процессов: флаги воркера (`--multiprocessing-fork`,
   `--captext-worker-module`) «съедаются», и дочерний процесс идёт по GUI-ветке —
   открывается второе окно (и иконка в Dock), а задача не выполняется.

2. ``COLLECT`` (onedir), а не onefile. onefile + spawn/multiprocessing заставляет
   каждый дочерний процесс распаковывать бандл в новый ``_MEIPASS`` — медленно и
   ломает пути/authkey. Onedir — один распакованный бандл, дочерние процессы дешёвые.

3. ``LSMultipleInstancesProhibited`` — LaunchServices не поднимает вторую копию
   приложения и не растаскивает её по Dock.

4. hiddenimports для multiprocessing.spawn/popen_spawn_* — PyInstaller не всегда
   тянет их из статического анализа, а они нужны для spawn-воркеров.

5. Ассеты AI-стека собираются явно (`collect_data_files`). Без этого в рантайме
   падает ``NO_SUCHFILE`` на ``faster_whisper/assets/silero_vad_v6.onnx`` —
   собственного хука для faster-whisper в PyInstaller нет.

6. ffmpeg/ffprobe (если найдены на машине сборки) кладутся в ``bin/`` бандла:
   ``main.py`` выставляет ``CAPTEXT_FFMPEG_DIR``, и приложение не пытается
   скачивать бинарники в рантайме (в песочнице/карантине это часто запрещено).

Совместимость: spec-файл ИСПОЛНЯЕТСЯ тем интерпретатором, под которым запущен
PyInstaller, — а он может быть старее, чем ваше приложение (например, системный
python3). Поэтому здесь:

  * стоит ``from __future__ import annotations`` (аннотации не вычисляются);
  * НЕТ аннотаций вида ``str | None`` и ``list[tuple[str, str]]`` — такие
    конструкции падают с ``TypeError: unsupported operand type(s) for |``
    на Python < 3.10 (и ``list[...]`` на < 3.9).

Проверить, каким интерпретатором собираете вы:

    python3 -c "import sys, PyInstaller; print(sys.version, PyInstaller.__version__)"

Сборка (важно запускать именно через ``python3 -m``, чтобы не поймать
PyInstaller из другого окружения):

    python3 -m PyInstaller captext_ai_pro.spec --noconfirm
"""
from __future__ import annotations        # аннотации не вычисляются при выполнении spec

import importlib.util
import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "CapText AI Pro"
ENTRY = "main.py"
IS_MAC = sys.platform == "darwin"

# --------------------------------------------------------------------- иконка
# Иконка обязана лежать в КОРНЕ проекта рядом с main.py и этим spec:
#     AppIcon.icns  — macOS (рисуется: python3 tools/make_app_icon.py)
#     AppIcon.png   — исходный арт 1024x1024 (кладётся в бандл для оконной иконки)
#
# Как это работает в PyInstaller (проверено по исходникам 6.22):
#   * EXE(icon=...) — реально встраивается ТОЛЬКО на Windows; на macOS EXE игнорирует
#     иконку (иконка окна/приложения берётся из бандла), на Linux печатает warning.
#     Ставим и туда, и туда — как вы просили; на Windows это даёт иконку .exe.
#   * BUNDLE(icon=...) — на macOS копирует файл в Contents/Resources/ и прописывает
#     CFBundleIconFile в Info.plist. Именно это и «запекает» иконку в .app.
#     ВАЖНО: без файла BUNDLE падает с FileNotFoundError — поэтому путь проверяем.
ICON = "AppIcon.icns"                 # ← ровно то, что просили: icon='AppIcon.icns'
ICON_FALLBACKS = (os.path.join("assets", "icon.icns"), os.path.join("assets", "AppIcon.icns"))

_SPEC_DIR = None
try:                                                    # PyInstaller кладёт это в namespace spec
    _SPEC_DIR = SPECPATH                                # noqa: F821
except NameError:
    _SPEC_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()


def resolve_icon():
    """Путь к .icns рядом со spec (или None, если иконки нет и платформа не macOS)."""
    for cand in (ICON, *ICON_FALLBACKS):
        if os.path.exists(os.path.join(_SPEC_DIR, cand)):
            return cand
    if IS_MAC:
        raise SystemExit(
            "[spec] НЕ найден %s в корне проекта: %s\n"
            "        Положите туда AppIcon.icns или сгенерируйте его:\n"
            "            python3 tools/make_app_icon.py --out-icns AppIcon.icns\n"
            "        (BUNDLE на macOS без иконки падает — поэтому сборка остановлена.)"
            % (ICON, _SPEC_DIR)
        )
    print("[spec] %s не найден — на этой платформе иконка не требуется, продолжаю" % ICON)
    return None


# Ранняя проверка: если файла нет — на macOS сборка останавливается с понятной
# подсказкой (а не падает невнятным FileNotFoundError внутри BUNDLE).
resolve_icon()

# --------------------------------------------------------------------- datas
datas = [("app/ui/assets", "app/ui/assets")]        # theme.py ищет SVG рядом с собой
# AppIcon.png кладём в бандл: main.py ставит его иконкой окна (актуально для
# Windows/Linux; на macOS иконку окна задаёт система по Info.plist).
# Мастер-файл живёт в assets/ (его пишет tools/make_app_icon.py), но если
# положить копию в корень — тоже подхватится.
for _icon_png in (os.path.join(_SPEC_DIR, "assets", "AppIcon.png"),
                  os.path.join(_SPEC_DIR, "AppIcon.png")):
    if os.path.exists(_icon_png):
        datas.append((_icon_png, "."))
        print("[spec] иконка окна в бандле: %s" % _icon_png)
        break

# ------------------------------------------------- ассеты AI-стека (data files)
# КРИТИЧНО для faster-whisper: у PyInstaller НЕТ хука для этого пакета, поэтому
# модель VAD `faster_whisper/assets/silero_vad_v6.onnx` (1.2 МБ) не попадает в
# бандл. В рантайме `faster_whisper.vad` читает её через
# `get_assets_path()/silero_vad_v6.onnx` и падает с
#     NO_SUCHFILE: Load model from .../silero_vad_v6.onnx failed
# Явный сбор data-файлов это лечит.
datas += collect_data_files("faster_whisper")

# Остальные пакеты стека с ассетами: собираем только те, что реально установлены.
#   * whisper  — mel_filters.npz и *.tiktoken (нужны openai-whisper, на нём стоит stable-ts);
#   * demucs   — yaml-описания моделей (веса качаются в рантайме);
#   * librosa  — registry.txt для lazy_loader;
#   * torchaudio/librosa — прочие данные, если есть.
# Если пакета нет на машине сборки — просто пропускаем.
AI_DATA_PACKAGES = ("stable_whisper", "whisper", "tiktoken", "tiktoken_ext",
                    "demucs", "torchaudio", "librosa")
for _pkg in AI_DATA_PACKAGES:
    if importlib.util.find_spec(_pkg) is None:
        print("[spec] %s не установлен — ассеты не собираю" % _pkg)
        continue
    _files = collect_data_files(_pkg)
    datas += _files
    print("[spec] ассеты %s: %d файлов" % (_pkg, len(_files)))

# отчёт: что из faster_whisper реально попало в бандл
_fw = [d for d in datas if "faster_whisper" in str(d[0])]
print("[spec] faster_whisper: %d файлов данных, в т.ч. VAD-модель: %s"
      % (len(_fw), any("silero_vad" in str(d[0]) for d in _fw)))


# ------------------------------------------------------------ ffmpeg staging
def stage_ffmpeg():
    """Скопировать ffmpeg/ffprobe в build/ffmpeg_bin с правильными именами.

    Возвращает список (src, dest_dir) для `binaries=`, чтобы в бандле оказались
    ровно `bin/ffmpeg` и `bin/ffprobe` с битом исполнения.
    """
    found = {}

    def remember(path, name):
        if path and os.path.exists(path) and name not in found:
            found[name] = os.path.abspath(path)

    remember(shutil.which("ffmpeg"), "ffmpeg")
    remember(shutil.which("ffprobe"), "ffprobe")
    try:                                            # static_ffmpeg: качает/кэширует
        from static_ffmpeg import run as static_run  # type: ignore
        ff, fp = static_run.get_or_fetch_platform_executables_else_raise()
        remember(ff, "ffmpeg")
        remember(fp, "ffprobe")
    except Exception:
        pass
    try:                                            # imageio_ffmpeg: только ffmpeg
        import imageio_ffmpeg  # type: ignore
        remember(imageio_ffmpeg.get_ffmpeg_exe(), "ffmpeg")
    except Exception:
        pass

    if not found:
        print("[spec] ffmpeg/ffprobe не найдены — приложение будет искать их в системе")
        return []

    stage = os.path.abspath(os.path.join("build", "ffmpeg_bin"))
    os.makedirs(stage, exist_ok=True)
    out = []
    for name, src in found.items():
        dst = os.path.join(stage, name)
        if os.path.abspath(src) != dst:
            shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        out.append((dst, "bin"))
        print("[spec] в бандл попадёт %s: %s" % (name, src))
    return out


binaries = stage_ffmpeg()

# ------------------------------------------------------------ hidden imports
hiddenimports = [
    # spawn-воркеры (multiprocessing): нужны явно, иначе PyInstaller их теряет
    "multiprocessing",
    "multiprocessing.spawn",
    "multiprocessing.popen_spawn_posix",
    "multiprocessing.popen_spawn_win32",
    "multiprocessing.forkserver",
    "multiprocessing.resource_tracker",
    "queue",
] + collect_submodules("app")

# ------------------------------------------------------------------- excludes
excludes = [
    "tkinter", "_tkinter",
    # CUDA-стек: приложению не нужен (на macOS его и нет, а на Linux-сборке
    # torch-CUDA тащит ~3 ГБ библиотек nvidia). Работаем на CPU/MPS.
    # ВАЖНО: "torch.cuda" здесь исключать нельзя — torch импортирует его на
    # старте, и весь стек (torch/torchaudio/stable-ts) падает с
    # ModuleNotFoundError. Исключаем только сами колёса.
    "nvidia", "triton",
    "matplotlib", "IPython", "jupyter", "notebook",
    "pytest", "_pytest",
    "PyQt5", "PyQt6", "PySide2",           # проект на PySide6 — чужие биндинги не тащим
    "setuptools._distutils",
]

a = Analysis(
    [ENTRY],
    pathex=[os.path.abspath(".")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir: файлы кладёт COLLECT
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=not IS_MAC,             # macOS — оконное приложение; на Linux удобнее видеть лог
    icon='AppIcon.icns',            # ← ровно это: на Windows встраивается в EXE
    disable_windowed_traceback=False,
    argv_emulation=False,           # no-op для EXE; фиксация намерения (см. BUNDLE)
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# ----------------------------------------------------------------- macOS .app
if IS_MAC:
    app = BUNDLE(
        coll,
        name="%s.app" % APP_NAME,
        # ← иконка .app: копируется в Contents/Resources/AppIcon.icns,
        #   в Info.plist прописывается CFBundleIconFile=AppIcon.icns
        icon='AppIcon.icns',
        bundle_identifier="com.captext.aipro",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            # запретить LaunchServices поднимать вторую копию приложения
            "LSMultipleInstancesProhibited": True,
            "NSMicrophoneUsageDescription": "CapText AI Pro обрабатывает только выбранные вами файлы",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
        },
        # ⚠️ КРИТИЧНО: False. Включать нельзя — macOS перехватывает argv дочерних
        # процессов (spawn/воркеров), и они стартуют как второе GUI-окно.
        argv_emulation=False,
    )

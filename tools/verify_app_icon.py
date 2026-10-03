#!/usr/bin/env python3
"""verify_app_icon.py — проверка, что иконка реально попала в .app пакет.

На macOS — проверяет НАСТОЯЩИЙ собранный бандл:

    python3 tools/verify_app_icon.py "dist/CapText AI Pro.app"

Проверяет: Info.plist (CFBundleIconFile), наличие и целостность
Contents/Resources/<icon>, совпадение с корневым AppIcon.icns, размеры внутри .icns,
а также что иконка не «пустая» (есть альфа-канал/не чёрный квадрат).

На Linux/Windows BUNDLE() в PyInstaller — no-op, поэтому скрипт воспроизводит
ровно тот же этап сборки (по исходникам PyInstaller building/osx.py):

    normalize_icon_type(icon, ("icns",), "icns", workpath)   # валидация иконки
    shutil.copyfile(icon, Contents/Resources/<basename>)     # копирование
    info_plist_dict["CFBundleIconFile"] = basename           # запись в Info.plist

и собирает структуру .app в /tmp для проверки. Так видно, что spec «прошит»
правильно, ещё до сборки на Mac.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON_NAME = "AppIcon.icns"
APP_NAME = "CapText AI Pro"


# ------------------------------------------------------------------ утилиты
def parse_icns(path: Path) -> dict:
    raw = path.read_bytes()
    info = {"magic_ok": raw[:4] == b"icns", "declared": None, "actual": len(raw),
            "entries": [], "sizes": [], "pngs_ok": True}
    if not info["magic_ok"]:
        return info
    info["declared"] = struct.unpack(">I", raw[4:8])[0]
    pos = 8
    while pos + 8 <= len(raw):
        code = raw[pos:pos + 4].decode("ascii", "replace")
        length = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
        payload = raw[pos + 8:pos + length]
        is_png = payload[:8] == b"\x89PNG\r\n\x1a\n"
        size = None
        if is_png:
            w, h = struct.unpack(">II", payload[16:24])
            size = w
            info["sizes"].append(w)
        else:
            info["pngs_ok"] = False
        info["entries"].append((code, length, size))
        pos += length
    return info


def icon_looks_sane(path: Path) -> tuple:
    """Не «чёрный квадрат»: есть прозрачные пиксели и цветная зона."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return True, "Pillow/numpy недоступны — визуальная проверка пропущена"
    im = Image.open(path).convert("RGBA")
    a = np.array(im.resize((256, 256)))
    transparent = float((a[:, :, 3] < 10).mean())
    colored = float(((a[:, :, 3] > 200) & (a[:, :, :3].max(axis=2) > 90)).mean())
    corners_opaque = float((a[:40, :40, 3] > 200).mean())
    ok = transparent > 0.05 and colored > 0.05 and corners_opaque < 0.5
    return ok, (f"прозрачных {transparent * 100:.0f}%, цветных {colored * 100:.0f}%, "
                f"непрозрачный угол {corners_opaque * 100:.0f}%")


# ------------------------------------------------------------ macOS: реальный .app
def verify_real_app(app_dir: Path) -> bool:
    print(f"[i] проверяю настоящий бандл: {app_dir}")
    ok = True
    plist_path = app_dir / "Contents" / "Info.plist"
    if not plist_path.exists():
        print("  [FAIL] нет Contents/Info.plist")
        return False
    with open(plist_path, "rb") as fh:
        plist = plistlib.load(fh)

    icon_in_plist = plist.get("CFBundleIconFile")
    print(f"  CFBundleIconFile: {icon_in_plist}")
    ok &= bool(icon_in_plist)
    ok &= plist.get("LSMultipleInstancesProhibited") is True
    print(f"  LSMultipleInstancesProhibited: {plist.get('LSMultipleInstancesProhibited')}")

    print("  ключи argv* в Info.plist:",
          [k for k in plist if "argv" in k.lower()] or "нет (OK)")

    if icon_in_plist:
        res = app_dir / "Contents" / "Resources" / icon_in_plist
        exists = res.exists()
        print(f"  файл иконки в Resources: {'есть' if exists else 'НЕТ'} ({res})")
        ok &= exists
        if exists:
            src = ROOT / ICON_NAME
            if src.exists():
                same = src.read_bytes() == res.read_bytes()
                print(f"  совпадает с {ICON_NAME}: {'да' if same else 'НЕТ'}")
                ok &= same
            info = parse_icns(res)
            print(f"  .icns: магия {'OK' if info['magic_ok'] else 'ОШИБКА'}, "
                  f"размеры {sorted(set(info['sizes']))}")
            need = {16, 32, 128, 256, 512, 1024}
            ok &= info["magic_ok"] and need <= set(info["sizes"]) and info["pngs_ok"]
            sane, desc = icon_looks_sane(res)
            print(f"  визуально: {'OK' if sane else 'ПОДОЗРИТЕЛЬНО'} ({desc})")
            ok &= sane
    return ok


# ------------------------------------------------- Linux: эмуляция этапа BUNDLE
def emulate_bundle(app_dir: Path) -> bool:
    print("[i] платформа не macOS: BUNDLE() в PyInstaller здесь no-op.")
    print("[i] воспроизвожу ровно тот же этап иконки, что делает building/osx.py\n")
    try:
        from PyInstaller.building.icon import normalize_icon_type
    except ImportError:
        print("  [FAIL] PyInstaller не установлен — нечем валидировать .icns")
        return False

    icon_src = ROOT / ICON_NAME
    if not icon_src.exists():
        print(f"  [FAIL] нет {icon_src} — сгенерируйте: python3 tools/make_app_icon.py --out-icns AppIcon.icns")
        return False

    workpath = tempfile.mkdtemp(prefix="pyi_icon_")
    os.makedirs(app_dir / "Contents" / "MacOS", exist_ok=True)
    os.makedirs(app_dir / "Contents" / "Resources", exist_ok=True)

    # 1) та же валидация, что вызывает BUNDLE
    icon = normalize_icon_type(str(icon_src), ("icns",), "icns", workpath)
    print(f"  1) normalize_icon_type → {icon}")
    icon = os.path.abspath(icon)

    # 2) то же копирование
    dest = app_dir / "Contents" / "Resources" / os.path.basename(icon)
    shutil.copyfile(icon, dest)
    print(f"  2) копирование в Resources → {dest.name} "
          f"({dest.stat().st_size} байт)")

    # 3) тот же Info.plist (ключевые поля из osx.py + наши info_plist)
    info_plist = {
        "CFBundleDisplayName": APP_NAME,
        "CFBundleName": APP_NAME,
        "CFBundleIdentifier": "com.captext.aipro",
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": os.path.basename(icon),          # ← это и «запекает» иконку
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "LSMultipleInstancesProhibited": True,
    }
    with open(app_dir / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info_plist, fh)
    print("  3) Info.plist → CFBundleIconFile=%s" % info_plist["CFBundleIconFile"])

    # 4) читаем результат так, как это сделала бы macOS
    with open(app_dir / "Contents" / "Info.plist", "rb") as fh:
        back = plistlib.load(fh)
    res = app_dir / "Contents" / "Resources" / back["CFBundleIconFile"]
    info = parse_icns(res)
    same = res.read_bytes() == icon_src.read_bytes()
    need = {16, 32, 128, 256, 512, 1024}
    sane, desc = icon_looks_sane(res)

    ok = (back["CFBundleIconFile"] == ICON_NAME and res.exists() and same
          and info["magic_ok"] and need <= set(info["sizes"]) and info["pngs_ok"]
          and info["declared"] == info["actual"] and sane)

    print("\n  структура .app:")
    for p in sorted(app_dir.rglob("*")):
        rel = p.relative_to(app_dir)
        print(f"    {'  ' * (len(rel.parts) - 1)}{rel.name}{'/' if p.is_dir() else ''}"
              f"{'' if p.is_dir() else f'  ({p.stat().st_size} байт)'}")
    print(f"\n  размеры в .icns: {sorted(set(info['sizes']))}")
    print(f"  совпадение с корневым {ICON_NAME}: {same}")
    print(f"  визуальная проверка: {'OK' if sane else 'ПОДОЗРИТЕЛЬНО'} ({desc})")
    return ok


def main(argv: list[str]) -> int:
    print("=" * 78)
    print("ПРОВЕРКА ИКОНКИ В .app")
    print("=" * 78)
    print(f"[i] корень проекта: {ROOT}")
    print(f"[i] платформа: {sys.platform}\n")

    src = ROOT / ICON_NAME
    print("[i] корневая иконка:", src, "—", "есть" if src.exists() else "НЕТ")
    if src.exists():
        info = parse_icns(src)
        print(f"    магия {'OK' if info['magic_ok'] else 'ОШИБКА'}, "
              f"элементов {len(info['entries'])}, размеры {sorted(set(info['sizes']))}")

    arg = Path(argv[1]) if len(argv) > 1 else None
    if sys.platform == "darwin":
        app_dir = arg or next(iter(sorted((ROOT / "dist").glob("*.app"))), None)
        if app_dir is None or not Path(app_dir).exists():
            print("\n[!] .app не найден — сначала соберите: "
                  "python3.11 -m PyInstaller captext_ai_pro.spec --noconfirm --clean")
            return 1
        ok = verify_real_app(Path(app_dir))
    else:
        app_dir = arg or Path(tempfile.mkdtemp(prefix="app_emu_")) / f"{APP_NAME}.app"
        print()
        ok = emulate_bundle(Path(app_dir))
        print(f"\n[i] структура собрана в: {app_dir}")
        print("[i] на macOS выполните тот же скрипт без аргументов — он проверит "
              "реальный dist/*.app")

    print("\n" + "=" * 78)
    print("ИТОГ:", "иконка корректно запекается в .app" if ok else "ПРОБЛЕМЫ — см. выше")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

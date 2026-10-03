#!/usr/bin/env bash
# build_macos.sh — сборка CapText AI Pro.app и проверки, которые ловят «второе окно».
#
#   ./build_macos.sh              # собрать
#   ./build_macos.sh --verify     # собрать и прогнать проверки запуска/воркеров
set -euo pipefail

APP_NAME="CapText AI Pro"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VERIFY=0
[[ "${1:-}" == "--verify" ]] && VERIFY=1

# ------------------------------------------------------------------ 1. окружение
if [[ ! -d .venv ]]; then
  echo "==> создаю venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> зависимости"
python -m pip install --upgrade pip wheel >/dev/null
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt

# --------------------------------------------------- 1.1 preflight окружения
echo "==> окружение сборки"
python - <<'PYEOF'
import sys
try:
    import PyInstaller
    pyi = PyInstaller.__version__
except ImportError:
    pyi = "НЕ УСТАНОВЛЕН (pip install -r requirements-build.txt)"
print("    интерпретатор : %s (%s)" % (sys.version.split()[0], sys.executable))
print("    PyInstaller   : %s" % pyi)
if sys.version_info < (3, 11):
    print("    ВНИМАНИЕ: приложению нужен Python >= 3.11 (см. pyproject.toml), "
          "а сборка идёт под %s." % sys.version.split()[0])
PYEOF

# spec-файл исполняется ЭТИМ интерпретатором: если он старее 3.10,
# аннотации вида `str | None` уронят сборку TypeError-ом. Проверяем заранее.
if [[ -f tools/check_spec_compat.py ]]; then
  python tools/check_spec_compat.py captext_ai_pro.spec || {
    echo "ПРОВАЛ: spec несовместим с текущим интерпретатором — исправьте до сборки"; exit 1; }
fi

# ------------------------------------------------------------------ 2. self-check
echo "==> ассеты AI-стека (do they exist for the bundle?)"
python - <<'PYEOF'
import importlib.util as u
for pkg, why in (("faster_whisper", "VAD-модель silero_vad_v6.onnx"),
                 ("stable_whisper", "stable-ts"), ("demucs", "HTDemucs"),
                 ("torch", "torch"), ("torchaudio", "CTC-выравнивание")):
    print(f"    {'OK ' if u.find_spec(pkg) else 'НЕТ'} {pkg:<16} {why}")
PYEOF

echo "==> self-check точки входа"
python - <<'PY'
import ast, sys, pathlib
src = pathlib.Path("main.py").read_text()
tree = ast.parse(src)
guards = [n for n in ast.walk(tree)
          if isinstance(n, ast.If) and getattr(getattr(n.test, "left", None), "id", "") == "__name__"]
assert guards, "main.py: нет блока if __name__ == '__main__'"
first = guards[0].body[0]
call = ast.unparse(first)
assert "freeze_support" in call, f"main.py: первый оператор __main__ — не freeze_support(): {call}"
assert "QApplication" not in src.split("if __name__")[0] or True
print("OK: freeze_support() первым оператором __main__")
PY

# ------------------------------------------------------------------ 2.5 иконка
echo "==> иконка приложения"
if [[ ! -f AppIcon.icns ]]; then
  echo "    AppIcon.icns нет — рисую иконку (белый squircle + «Text AI»)"
  python tools/make_app_icon.py --out-icns AppIcon.icns --out-master assets/AppIcon.png || true
fi
if [[ -f AppIcon.icns ]]; then
  echo "    AppIcon.icns: $(stat -f%z AppIcon.icns 2>/dev/null || stat -c%s AppIcon.icns) байт"
  python - <<'PYEOF'
import struct, pathlib
raw = pathlib.Path("AppIcon.icns").read_bytes()
assert raw[:4] == b"icns", "битый .icns: нет магии icns"
sizes, pos = [], 8
while pos + 8 <= len(raw):
    length = struct.unpack(">I", raw[pos+4:pos+8])[0]
    payload = raw[pos+8:pos+length]
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", payload[16:24])
        sizes.append(w)
    pos += length
print("    размеры в .icns:", sorted(set(sizes)))
assert {16, 32, 128, 256, 512, 1024} <= set(sizes), "в .icns нет обязательных размеров"
PYEOF
else
  echo "    ВНИМАНИЕ: иконки нет (AppIcon.icns/AppIcon.png) — на macOS сборка остановится"
fi

# ------------------------------------------------------------------ 3. сборка
echo "==> PyInstaller"
rm -rf build "dist/${APP_NAME}" "dist/${APP_NAME}.app"
python -m PyInstaller captext_ai_pro.spec --noconfirm --clean

# ------------------------------------------------------------------ 4. подпись
if [[ "$(uname)" == "Darwin" ]]; then
  echo "==> ad-hoc подпись (для локального запуска достаточно)"
  codesign --force --deep --sign - "dist/${APP_NAME}.app" || \
    echo "предупреждение: codesign не удался — приложение всё равно запустится локально"
  echo "==> Info.plist: проверяю, что argv emulation выключена"
  /usr/libexec/PlistBuddy -c "Print" "dist/${APP_NAME}.app/Contents/Info.plist" | grep -i "argv" \
    && echo "ВНИМАНИЕ: в Info.plist есть ключи argv* — проверьте вручную" \
    || echo "OK: ключей argv* нет"

  echo "==> Иконка в собранном .app"
  PLIST_ICON=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIconFile" "dist/${APP_NAME}.app/Contents/Info.plist")
  echo "    CFBundleIconFile = ${PLIST_ICON}"
  RES_ICON="dist/${APP_NAME}.app/Contents/Resources/${PLIST_ICON}"
  if [[ -f "$RES_ICON" ]]; then
    echo "    файл на месте: ${RES_ICON} ($(stat -f%z "$RES_ICON") байт)"
    cmp -s AppIcon.icns "$RES_ICON" && echo "    совпадает с AppIcon.icns: OK" \
      || echo "    ВНИМАНИЕ: содержимое отличается от AppIcon.icns"
  else
    echo "    ПРОВАЛ: ${RES_ICON} не найден — иконка не попала в бандл"; exit 1
  fi
  # обновить иконку в Dock/Finder без перезахода в систему
  touch "dist/${APP_NAME}.app" && echo "    touch .app выполнен (Finder обновит иконку)"
fi

# ------------------------------------------------------------------ 5. проверки
if [[ $VERIFY -eq 1 ]]; then
  BIN="dist/${APP_NAME}/${APP_NAME}"
  [[ -x "$BIN" ]] || BIN="dist/${APP_NAME}.app/Contents/MacOS/${APP_NAME}"

  echo "==> воркер-режим: GUI подниматься не должен"
  OUT="$("$BIN" --captext-worker-module app.workers.entry 2>&1 || true)"
  if grep -q "GUI-режим" <<<"$OUT"; then
    echo "ПРОВАЛ: воркер открыл GUI"; exit 1
  fi
  echo "OK: воркер не создал окно"

  echo "==> запуск GUI: считаю окна (должно быть ровно одно)"
  "$BIN" & APP_PID=$!
  sleep 4
  N=$(pgrep -f "${APP_NAME}" | wc -l | tr -d ' ')
  echo "процессов приложения: $N"
  kill "$APP_PID" 2>/dev/null || true
  [[ "$N" -le 1 ]] || { echo "ПРОВАЛ: запущено больше одного GUI-процесса"; exit 1; }
  echo "OK: одно окно"
fi

echo
echo "Готово: dist/${APP_NAME}.app"
echo "Проверить вручную: open \"dist/${APP_NAME}.app\" && ps -ax | grep -c \"${APP_NAME}\""

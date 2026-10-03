#!/usr/bin/env python3
"""check_spec_compat.py — проверка, что spec-файл совместим со СТАРЫМИ Python.

Зачем: spec исполняется тем интерпретатором, под которым запущен PyInstaller
(это может быть системный python3, например 3.9). Аннотации вида ``str | None``
вычисляются при определении функции и падают на Python < 3.10 с ошибкой

    TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

Проверки:
  1. статически: в аннотациях нет ``X | Y`` (PEP 604) и нет встроенных generic-ов
     ``list[...] / dict[...] / tuple[...]`` (PEP 585);
  2. реально: файл исполняется целиком (со стабами классов PyInstaller) на том
     интерпретаторе, которым запущен этот скрипт.

Запуск на разных версиях:

    python3.8  check_spec_compat.py captext_ai_pro.spec
    python3.9  check_spec_compat.py captext_ai_pro.spec
    python3.11 check_spec_compat.py captext_ai_pro.spec
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}


def annotation_nodes(tree: ast.AST):
    """Все узлы, являющиеся аннотациями (аргументы, возврат, AnnAssign)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            yield node.annotation
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            yield node.returns


def check_annotations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []

    def show(node) -> str:
        """ast.unparse появился только в 3.9 — для 3.8 используем запасной вариант."""
        unparse = getattr(ast, "unparse", None)
        if unparse is not None:
            return unparse(node)
        if isinstance(node, ast.BinOp):
            return f"{show(node.left)} | {show(node.right)}"
        if isinstance(node, ast.Subscript):
            return f"{show(node.value)}[...]"
        return type(node).__name__
    for ann in annotation_nodes(tree):
        for sub in ast.walk(ann):
            if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                problems.append(f"строка {sub.lineno}: PEP 604 union в аннотации "
                                f"({show(sub)}) — падает на Python < 3.10")
            if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) \
                    and sub.value.id in BUILTIN_GENERICS:
                problems.append(f"строка {sub.lineno}: PEP 585 generic ({show(sub)}) — "
                                f"падает на Python < 3.9")
    # сам future-импорт обязателен как страховка
    src = path.read_text(encoding="utf-8")
    has_future = "from __future__ import annotations" in src
    if not has_future:
        problems.append("нет `from __future__ import annotations` — аннотации будут вычисляться")
    return problems


def make_stub_pyinstaller(root: Path) -> None:
    """Стаб PyInstaller.utils.hooks (чтобы исполнить spec без установки PyInstaller)."""
    pkg = root / "PyInstaller" / "utils" / "hooks"
    pkg.mkdir(parents=True, exist_ok=True)
    (root / "PyInstaller" / "__init__.py").write_text("__version__ = 'stub'\n")
    (root / "PyInstaller" / "utils" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text(
        "def collect_submodules(pkg, **kw):\n"
        "    import pathlib\n"
        "    base = pathlib.Path(pkg.replace('.', '/'))\n"
        "    mods = []\n"
        "    if base.exists():\n"
        "        for p in base.rglob('*.py'):\n"
        "            if p.name == '__init__.py':\n"
        "                mods.append('.'.join(p.parent.parts))\n"
        "            else:\n"
        "                mods.append('.'.join(p.with_suffix('').parts))\n"
        "    return sorted(set(mods))\n"
        "\n"
        "\n"
        "def collect_data_files(pkg, **kw):\n"
        "    \"\"\"Стаб: реальный PyInstaller вернул бы список (src, dest).\"\"\"\n"
        "    return []\n")


def exec_spec(path: Path) -> tuple[bool, str]:
    """Исполнить spec целиком со стабами классов сборки."""
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))

    # Если PyInstaller реально установлен — используем его (максимальная точность).
    # Стаб нужен только там, где PyInstaller нет (например, проверка spec на Python 3.8).
    try:
        import PyInstaller.utils.hooks  # noqa: F401
        print("  [i] используется настоящий PyInstaller")
    except ImportError:
        stubs = Path("/tmp/_spec_stubs")
        make_stub_pyinstaller(stubs)
        sys.path.insert(0, str(stubs))
        print("  [i] PyInstaller нет — использую стаб")

    calls: list[tuple] = []

    def recorder(name):
        def factory(*a, **kw):
            calls.append((name, a, kw))
            return types.SimpleNamespace(name=name, pure=[], scripts=[], binaries=[], datas=[])
        return factory

    ns = {name: recorder(name) for name in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")}
    src = path.read_text(encoding="utf-8")
    # dont_inherit=True — КРИТИЧНО для честной проверки: без него compile() наследует
    # future-флаги (в т.ч. `annotations`) из ЭТОГО модуля, аннотации станут ленивыми
    # и spec «пройдёт» даже на старом Python. PyInstaller так не делает.
    flags = 0
    try:
        code = compile(src, str(path), "exec", flags=flags, dont_inherit=True)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc}"
    try:
        exec(code, ns)
    except Exception as exc:                                   # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    used = [c[0] for c in calls]
    return True, "выполнено, вызваны: " + (", ".join(used) if used else "—")


def main(argv: list[str]) -> int:
    spec = Path(argv[1] if len(argv) > 1 else "captext_ai_pro.spec").resolve()
    print(f"[i] spec: {spec}")
    print(f"[i] интерпретатор: {sys.version.split()[0]} ({sys.executable})")

    problems = check_annotations(spec)
    print("\n--- 1. статический анализ аннотаций ---")
    if problems:
        for p in problems:
            print(f"  [FAIL] {p}")
    else:
        print("  [PASS] опасных аннотаций нет (нет `X | Y` и встроенных generic-ов)")

    print("\n--- 2. исполнение spec целиком ---")
    ok, info = exec_spec(spec)
    print(f"  [{'PASS' if ok else 'FAIL'}] {info}")

    print("\nИТОГ:", "spec совместим (можно собирать и старым python3)"
          if ok and not problems else "ЕСТЬ ПРОБЛЕМЫ — см. выше")
    return 0 if (ok and not problems) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

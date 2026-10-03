#!/usr/bin/env python3
"""make_app_icon.py — иконка CapText AI Pro: белый squircle + знак «Text AI».

Что делает
----------
Рисует иконку программно (никаких внешних картинок) и упаковывает её в
macOS-совместимый `AppIcon.icns` со всеми нужными размерами.

Почему рисуем, а не масштабируем один PNG
-----------------------------------------
Каждый размер отрисовывается **нативно** в своих пикселях, поэтому текст остаётся
резким: 1024 → «Text AI», мелкие размеры (32/16) → упрощённый знак «AI», который
читается, а не превращается в кашу.

Дизайн
------
* белая squircle-подложка по сетке Apple: плитка 824 из 1024, непрерывная кривизна
  (суперэллипс n≈5), тонкий холодный градиент и деликатный внешний контур;
* знак из двух строк: «Text» (чернильный `#0B0E13`) и «AI» (фирменный градиент
  `#00E5FF → #FF2D95` из `app/ui/theme.py`);
* тонкая градиентная линия под знаком — минималистичный акцент, отсылка к субтитрам.

Использование
-------------
    python3 tools/make_app_icon.py                 # AppIcon.icns в корень + assets/AppIcon.png
    python3 tools/make_app_icon.py --variant inline
    python3 tools/make_app_icon.py --sheet docs/icon_sheet.png
    python3 tools/make_app_icon.py --ico           # дополнительно Windows-иконка
    python3 tools/make_app_icon.py --check AppIcon.icns

Зависимости: pillow, numpy (и .ttf в assets/fonts — Inter, лицензия OFL рядом).
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                                   # pragma: no cover
    sys.exit("нужны pillow и numpy:  python3 -m pip install pillow numpy")

ROOT = Path(__file__).resolve().parent.parent

# --- геометрия по гайдлайнам Apple -----------------------------------------
CANVAS = 1024          # мастер-размер
TILE = 824             # плитка внутри холста (iOS/macOS Big Sur grid)
TILE_SOFT_INSET = 0.0  # у суперэллипса край строится по кривой, инсет не нужен
SUPERN = 5.0           # показатель суперэллипса: ≈ непрерывная кривизна Apple

# --- фирменные цвета (синхронно с app/ui/theme.py) -------------------------
INK = (11, 14, 19)             # #0B0E13
ACCENT = (0, 178, 214)         # #00B2D6 — фирменный #00E5FF углублён под белый фон
ACCENT_2 = (236, 32, 138)      # #EC208A — #FF2D95 чуть глубже
TILE_TOP = (255, 255, 255)     # белый верх плитки
TILE_BOTTOM = (240, 244, 250)  # лёгкий холодный низ — «материальность» без грязи
RIM = (11, 14, 19, 11)         # контур плитки, alpha 4% (тонкая отбивка от белого)

# --- раскладки -------------------------------------------------------------
FONT_BOLD = ROOT / "assets" / "fonts" / "Inter-Bold.ttf"
FONT_SEMI = ROOT / "assets" / "fonts" / "Inter-SemiBold.ttf"
FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)

ICNS_ENTRIES = [
    (b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128), (b"ic08", 256),
    (b"ic09", 512), (b"ic10", 1024),
    (b"ic11", 32), (b"ic12", 64), (b"ic13", 256), (b"ic14", 512),   # @2x-варианты
]
ALL_SIZES = sorted({size for _code, size in ICNS_ENTRIES})


def load_font(path: Path, fallbacks, size: int) -> ImageFont.FreeTypeFont:
    for cand in (path, *[Path(f) for f in fallbacks]):
        if cand.exists():
            return ImageFont.truetype(str(cand), size)
    return ImageFont.load_default(size)               # pragma: no cover


def superellipse_alpha(px: int, n: float = SUPERN, inset: float = 0.0,
                       ss: int | None = None) -> Image.Image:
    """Маска squircle с РЕЗКИМ краем (≈1 px).

    Раньше здесь было сглаживание «по расстоянию в норме d», из-за чего край
    размазывался на десятки пикселей и вокруг иконки появлялось серое гало.
    Правильно: жёсткий порог на суперсэмплированной сетке + уменьшение с
    LANCZOS — уменьшение само даёт сглаживание в один пиксель.
    """
    # суперсэмплинг адаптивный: на больших размерах память не резиновая
    if ss is None:
        ss = 8 if px <= 192 else (4 if px <= 512 else 2)
    big = px * ss
    yy, xx = np.mgrid[0:big, 0:big]
    x = xx.astype(np.float32)
    y = yy.astype(np.float32)
    del yy, xx
    c = (big - 1) / 2.0
    x -= c
    x /= (big / 2.0)
    y -= c
    y /= (big / 2.0)
    np.abs(x, out=x)
    np.abs(y, out=y)
    np.power(x, n, out=x)
    np.power(y, n, out=y)
    x += y
    del y
    np.power(x, 1.0 / n, out=x)                       # d
    limit = 1.0 - 2.0 * inset / max(1.0, px)          # стягивание формы внутрь
    hard = np.where(x <= limit, np.uint8(255), np.uint8(0))
    del x
    return Image.fromarray(hard, "L").resize((px, px), Image.LANCZOS)


def vertical_gradient(size: tuple, top: tuple, bottom: tuple) -> Image.Image:
    w, h = size
    ramp = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    arr = np.zeros((h, w, 3), dtype=np.float32)
    for i in range(3):
        arr[:, :, i] = (top[i] + (bottom[i] - top[i]) * ramp)[:, 0][:, None]
    return Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")


def horizontal_gradient(size: tuple, left: tuple, right: tuple) -> Image.Image:
    w, h = size
    ramp = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    arr = np.zeros((h, w, 3), dtype=np.float32)
    for i in range(3):
        arr[:, :, i] = (left[i] + (right[i] - left[i]) * ramp)
    return Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")


def text_mask(text: str, font: ImageFont.FreeTypeFont, tracking_px: float) -> Image.Image:
    """Маска строки с межбуквенным интервалом (PIL сам его не умеет)."""
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    widths = [probe.textlength(ch, font=font) for ch in text]
    total = int(round(sum(widths) + tracking_px * max(0, len(text) - 1)))
    asc, desc = font.getmetrics()
    mask = Image.new("L", (max(total, 1), asc + desc), 0)
    d = ImageDraw.Draw(mask)
    x = 0.0
    for ch, w in zip(text, widths):
        d.text((x, 0), ch, font=font, fill=255)
        x += w + tracking_px
    bbox = mask.getbbox()
    return mask.crop(bbox) if bbox else mask


def gradient_through_mask(mask: Image.Image, left: tuple, right: tuple) -> Image.Image:
    grad = horizontal_gradient(mask.size, left, right)
    out = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def rounded_rule(size_px: tuple, radius: float, left: tuple, right: tuple) -> Image.Image:
    w, h = size_px
    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1),
                                           radius=radius * 4, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    return gradient_through_mask(mask, left, right)


def draw_icon(px: int = CANVAS, variant: str = "inline") -> Image.Image:
    """Иконка размера px: прозрачный холст + белая squircle + знак «Text AI»."""
    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    k = px / CANVAS                                  # масштаб от мастера
    tile_px = max(2, int(round(TILE * k)))
    tile = Image.new("RGBA", (tile_px, tile_px), (0, 0, 0, 0))

    # --- плитка: белый градиент через squircle-маску
    alpha = superellipse_alpha(tile_px)
    fill = vertical_gradient((tile_px, tile_px), TILE_TOP, TILE_BOTTOM)
    tile.paste(fill, (0, 0), alpha)

    # --- деликатный контур: кольцо шириной ~1.2 px (только на крупных размерах,
    #     иначе на 16–32 px оно «съедает» иконку)
    if px >= 64:
        rim_px = max(1.0, 1.2 * k)
        inner = superellipse_alpha(tile_px, inset=rim_px)
        rim_mask = Image.fromarray(
            np.clip(np.asarray(alpha, np.int16) - np.asarray(inner, np.int16),
                    0, 255).astype(np.uint8), "L")
        tile.paste(Image.new("RGBA", tile.size, RIM), (0, 0), rim_mask)

    # --- знак
    T = tile_px
    full_mark = px >= 96                     # ниже — упрощённый знак (см. _mark_small)
    if full_mark and variant == "stacked":
        _mark_stacked(tile, T)
    elif full_mark and variant == "inline":
        _mark_inline(tile, T)
    elif full_mark and variant == "pill":
        _mark_pill(tile, T)
    else:
        _mark_small(tile, T)

    off = (px - tile_px) // 2
    canvas.alpha_composite(tile, (off, off))
    return canvas


def _fit_font(text: str, font_path: Path, target_w: float, tracking: float = 0.0,
              fallbacks=FALLBACK_FONTS) -> ImageFont.FreeTypeFont:
    """Подобрать размер шрифта так, чтобы строка заняла target_w пикселей."""
    probe = load_font(font_path, fallbacks, 100)
    base = probe.getlength(text) + tracking * 100 * max(0, len(text) - 1)
    size = max(6, int(round(100 * target_w / max(1.0, base))))
    return load_font(font_path, fallbacks, size)


def _paste_line(tile: Image.Image, mask: Image.Image, x: int, y: int,
                gradient: bool = False, color: tuple = INK) -> None:
    if gradient:
        tile.alpha_composite(gradient_through_mask(mask, ACCENT, ACCENT_2), (x, y))
    else:
        layer = Image.new("RGBA", mask.size, color + (255,))
        tile.alpha_composite(layer, (x, y), mask) if False else None
        tile.paste(layer, (x, y), mask)


def _mark_stacked(tile: Image.Image, T: int) -> None:
    """Две строки: «Text» чернилами, «AI» градиентом. Классический знак-локап."""
    m1 = text_mask("Text", _fit_font("Text", FONT_SEMI, 0.52 * T), tracking_px=0.012 * T)
    m2 = text_mask("AI", _fit_font("AI", FONT_BOLD, 0.34 * T), tracking_px=0.070 * T)
    gap = 0.070 * T
    block_h = m1.height + gap + m2.height
    top = (T - block_h) / 2 - 0.03 * T
    _paste_line(tile, m1, int((T - m1.width) / 2), int(top))
    _paste_line(tile, m2, int((T - m2.width) / 2), int(top + m1.height + gap), gradient=True)
    if T >= 160:
        rw, rh = int(0.24 * T), max(2, int(round(0.013 * T)))
        tile.alpha_composite(rounded_rule((rw, rh), rh / 2, ACCENT, ACCENT_2),
                             (int((T - rw) / 2), int(top + block_h + 0.070 * T)))


def _mark_inline(tile: Image.Image, T: int) -> None:
    """Одна строка «Text AI»: «Text» чернилами, «AI» градиентом, линия под ней."""
    f1 = _fit_font("Text", FONT_SEMI, 0.42 * T)
    f2 = _fit_font("AI", FONT_BOLD, 0.30 * T)
    m1 = text_mask("Text", f1, tracking_px=0.010 * T)
    m2 = text_mask("AI", f2, tracking_px=0.045 * T)
    space = 0.050 * T
    total_w = m1.width + space + m2.width
    base_y = int((T - max(m1.height, m2.height)) / 2 - 0.045 * T)
    x0 = int((T - total_w) / 2)
    _paste_line(tile, m1, x0, base_y + (max(m1.height, m2.height) - m1.height))
    _paste_line(tile, m2, int(x0 + m1.width + space),
                base_y + (max(m1.height, m2.height) - m2.height), gradient=True)
    if T >= 160:
        rw, rh = int(0.30 * T), max(2, int(round(0.013 * T)))
        tile.alpha_composite(rounded_rule((rw, rh), rh / 2, ACCENT, ACCENT_2),
                             (int((T - rw) / 2), int(base_y + max(m1.height, m2.height) + 0.085 * T)))


def _mark_pill(tile: Image.Image, T: int) -> None:
    """«Text» чернилами + градиентная «пилюля» с белым «AI» внутри."""
    f1 = _fit_font("Text", FONT_SEMI, 0.52 * T)
    m1 = text_mask("Text", f1, tracking_px=0.012 * T)
    f2 = _fit_font("AI", FONT_BOLD, 0.22 * T)
    m2 = text_mask("AI", f2, tracking_px=0.05 * T)

    pill_w = int(m2.width + 0.24 * T)
    pill_h = int(m2.height + 0.20 * T)
    gap = int(0.10 * T)
    block_h = m1.height + gap + pill_h
    top = int((T - block_h) / 2)

    _paste_line(tile, m1, int((T - m1.width) / 2), top)

    pill_mask = Image.new("L", (pill_w * 4, pill_h * 4), 0)
    ImageDraw.Draw(pill_mask).rounded_rectangle((0, 0, pill_w * 4 - 1, pill_h * 4 - 1),
                                                radius=pill_h * 2, fill=255)
    pill_mask = pill_mask.resize((pill_w, pill_h), Image.LANCZOS)
    pill = gradient_through_mask(pill_mask, ACCENT, ACCENT_2)
    px0 = int((T - pill_w) / 2)
    py0 = top + m1.height + gap
    tile.alpha_composite(pill, (px0, py0))
    white = Image.new("RGBA", m2.size, (255, 255, 255, 255))
    tile.paste(white, (int((T - m2.width) / 2), int(py0 + (pill_h - m2.height) / 2)), m2)


def _mark_small(tile: Image.Image, T: int) -> None:
    """Мелкие размеры — упрощение знака (как делают иконки macOS):

    * 32–64 px: «AI» — две буквы ещё читаются;
    * 16 px: одна «A» — в 16 пикселей две буквы превращаются в кашу.
    Полный знак «Text AI» остаётся на 128 px и выше.
    """
    glyph = "AI" if T >= 32 else "A"
    m = text_mask(glyph, _fit_font(glyph, FONT_BOLD, 0.60 * T),
                  tracking_px=0.05 * T if glyph == "AI" else 0.0)
    y = int((T - m.height) / 2 - (0.04 * T if T >= 48 else 0))
    _paste_line(tile, m, int((T - m.width) / 2), y, gradient=True)
    if T >= 48:                                   # акцентная линия — связь с крупной иконкой
        rw, rh = int(0.30 * T), max(2, int(round(0.035 * T)))
        tile.alpha_composite(rounded_rule((rw, rh), rh / 2, ACCENT, ACCENT_2),
                             (int((T - rw) / 2), int(y + m.height + 0.11 * T)))


# --------------------------------------------------------------------- упаковка
def pack_icns(out_path: Path, variant: str = "inline") -> list[int]:
    """Собрать .icns: каждый элемент отрисован нативно в своём размере."""
    import io

    elements, sizes = [], []
    for code, size in ICNS_ENTRIES:
        buf = io.BytesIO()
        draw_icon(size, variant).save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        elements.append(code + struct.pack(">I", len(data) + 8) + data)
        sizes.append(size)
    total = 8 + sum(len(e) for e in elements)
    with open(out_path, "wb") as fh:
        fh.write(b"icns" + struct.pack(">I", total))
        for e in elements:
            fh.write(e)
    return sorted(set(sizes))


def check_icns(path: Path) -> bool:
    raw = path.read_bytes()
    ok = raw[:4] == b"icns" and struct.unpack(">I", raw[4:8])[0] == len(raw)
    print(f"  магия/длина: {'OK' if ok else 'ОШИБКА'} ({len(raw)} байт)")
    pos, found = 8, []
    while pos + 8 <= len(raw):
        code = raw[pos:pos + 4].decode("ascii", "replace")
        length = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
        payload = raw[pos + 8:pos + length]
        if payload[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", payload[16:24])
            found.append((code, w, h))
        else:
            ok = False
        pos += length
    for code, w, h in found:
        print(f"    {code}: {w}×{h}")
    ok &= {16, 32, 64, 128, 256, 512, 1024} <= {w for _c, w, _h in found}
    ok &= len(found) == len(ICNS_ENTRIES)
    return bool(ok)


def build_sheet(out_path: Path, variant: str) -> None:
    """Превью: как иконка выглядит в Dock/Finder в разных размерах."""
    sizes = [512, 256, 128, 64, 32, 16]
    gap, pad, label = 30, 34, 30
    width = pad * 2 + sum(sizes) + gap * (len(sizes) - 1)
    height = pad * 2 + 512 + label
    sheet = Image.new("RGBA", (width, height), (244, 246, 250, 255))
    d = ImageDraw.Draw(sheet)
    font = load_font(FONT_SEMI, FALLBACK_FONTS, 16)
    x = pad
    for s in sizes:
        img = draw_icon(s, variant)
        top = pad + (512 - s) // 2
        if s <= 128:                                  # едва заметная тень под мелкой иконкой
            for dy, a in ((2, 6), (4, 9)):
                sh = Image.new("RGBA", (s + 10, s + 10), (0, 0, 0, 0))
                ImageDraw.Draw(sh).rounded_rectangle((5, 5 + dy, s + 4, s + 4 + dy),
                                                     radius=int(s * 0.24), fill=(11, 14, 19, a))
                sh = sh.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(1.6))
                sheet.alpha_composite(sh, (x - 5, top - 5))
        sheet.alpha_composite(img, (x, top))
        d.text((x, pad + 512 + 6), f"{s}×{s}", fill=(90, 100, 120, 255), font=font)
        x += s + gap
    sheet.convert("RGB").save(out_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Иконка CapText AI Pro → AppIcon.icns")
    ap.add_argument("--variant", default="inline", choices=["stacked", "inline", "pill"])
    ap.add_argument("--out-icns", default=str(ROOT / "AppIcon.icns"))
    ap.add_argument("--out-master", default=str(ROOT / "assets" / "AppIcon.png"))
    ap.add_argument("--sheet", default=None, help="сохранить превью с размерами")
    ap.add_argument("--ico", action="store_true", help="дополнительно Windows-иконка")
    ap.add_argument("--check", default=None, help="только проверить готовый .icns")
    args = ap.parse_args(argv)

    if args.check:
        print(f"[i] проверка {args.check}")
        return 0 if check_icns(Path(args.check)) else 1

    master = draw_icon(CANVAS, args.variant)
    Path(args.out_master).parent.mkdir(parents=True, exist_ok=True)
    master.save(args.out_master)
    print(f"[+] мастер: {args.out_master} ({CANVAS}×{CANVAS}, прозрачный фон)")

    sizes = pack_icns(Path(args.out_icns), args.variant)
    print(f"[+] {args.out_icns}: размеры {sizes}")
    ok = check_icns(Path(args.out_icns))

    if args.ico:
        ico = Path(args.out_icns).with_suffix(".ico")
        master.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        print(f"[+] {ico}")

    if args.sheet:
        build_sheet(Path(args.sheet), args.variant)
        print(f"[+] превью: {args.sheet}")

    print("\nИТОГ:", "иконка собрана" if ok else "ПРОБЛЕМА в .icns")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

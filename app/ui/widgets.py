"""Переиспользуемые UI-компоненты: карточки пресетов, сегментные кнопки, аккордеон."""
from __future__ import annotations

from typing import Callable, Iterable, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from app.ui.theme import ACCENT, BG_ALT, LINE, MUTED, PANEL, TEXT


class PresetCard(QFrame):
    """Кликабельная карточка пресета с живой миниатюрой текста."""

    clicked = Signal(str)

    def __init__(self, key: str, title: str, subtitle: str, sample: str,
                 fill: str = "#FFFFFF", stroke: str = "#000000",
                 glow: str = "#000000", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self._sample = sample
        self._fill, self._stroke, self._glow = fill, stroke, glow
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(84)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip(subtitle)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        self.thumb = _Thumbnail(sample, fill, stroke, glow)
        lay.addWidget(self.thumb)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet(f"font-weight:800; color:{TEXT};")
        self.lbl_sub = QLabel(subtitle)
        self.lbl_sub.setWordWrap(True)
        self.lbl_sub.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        text.addWidget(self.lbl_title)
        text.addWidget(self.lbl_sub)
        lay.addLayout(text, 1)
        self._restyle()

    def set_selected(self, value: bool) -> None:
        if self._selected != value:
            self._selected = value
            self._restyle()

    def _restyle(self) -> None:
        border = ACCENT if self._selected else LINE
        width = 2 if self._selected else 1
        self.setStyleSheet(
            f"PresetCard {{ background:{PANEL}; border:{width}px solid {border};"
            f" border-radius:12px; }}"
            f"PresetCard:hover {{ border-color:{ACCENT}; }}")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(event)


class _Thumbnail(QWidget):
    """Мини-превью стиля: текст с обводкой и свечением."""

    def __init__(self, sample: str, fill: str, stroke: str, glow: str) -> None:
        super().__init__()
        self.setFixedSize(QSize(92, 64))
        self._sample, self._fill, self._stroke, self._glow = sample, fill, stroke, glow

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0A0D14"))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)

        font = QFont("Inter", 13)
        font.setBold(True)
        path = QPainterPath()
        metrics_w = p.fontMetrics().horizontalAdvance(self._sample) or 40
        path.addText(max(6, (self.width() - metrics_w) / 2 - 4), 38, font, self._sample)

        glow = QColor(self._glow)
        if glow.isValid() and glow.name().upper() not in ("#000000",):
            pen = QPen(glow, 7)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setOpacity(0.35)
            p.strokePath(path, pen)
            p.setOpacity(1.0)
        p.strokePath(path, QPen(QColor(self._stroke), 4, Qt.SolidLine, Qt.RoundCap,
                                Qt.RoundJoin))
        p.fillPath(path, QColor(self._fill))
        p.end()


class SegmentedControl(QWidget):
    """Группа кнопок-переключателей (как сегмент-контрол в CapCut)."""

    changed = Signal(str)

    def __init__(self, options: Sequence[tuple[str, str]], default: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, title in options:
            btn = QPushButton(title)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background:{BG_ALT}; border:1px solid {LINE};"
                f" border-radius:9px; padding:7px 10px; }}"
                f"QPushButton:checked {{ background:{ACCENT}; color:#06090F; font-weight:800;"
                f" border-color:{ACCENT}; }}")
            btn.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
            self._group.addButton(btn)
            self._buttons[key] = btn
            lay.addWidget(btn, 1)
        if default and default in self._buttons:
            self._buttons[default].setChecked(True)

    def set_value(self, key: str) -> None:
        btn = self._buttons.get(key)
        if btn and not btn.isChecked():
            btn.setChecked(True)

    def value(self) -> str | None:
        for key, btn in self._buttons.items():
            if btn.isChecked():
                return key
        return None


class Collapsible(QWidget):
    """Сворачиваемая секция — сюда прячем всё «для разработчиков»."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._button = QToolButton()
        self._button.setText(title)
        self._button.setCheckable(True)
        self._button.setChecked(False)
        self._button.setCursor(Qt.PointingHandCursor)
        self._button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._button.setArrowType(Qt.RightArrow)
        self._button.setStyleSheet(
            f"QToolButton {{ background:transparent; border:none; color:{MUTED};"
            f" font-weight:700; padding:6px 2px; }}"
            f"QToolButton:hover {{ color:{ACCENT}; }}")
        self._button.toggled.connect(self._on_toggle)

        self.content = QWidget()
        self.content.setVisible(False)
        self._content_layout = QVBoxLayout(self.content)
        self._content_layout.setContentsMargins(6, 4, 6, 8)
        self._content_layout.setSpacing(8)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._button)
        lay.addWidget(self.content)

    def _on_toggle(self, checked: bool) -> None:
        self._button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.content.setVisible(checked)

    def add_widget(self, widget: QWidget) -> None:
        self._content_layout.addWidget(widget)

    def add_layout(self, layout) -> None:
        self._content_layout.addLayout(layout)


class CardGrid(QWidget):
    """Сетка карточек пресетов с единственным выбором."""

    selected = Signal(str)

    def __init__(self, columns: int = 1, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        self._columns = columns
        self._cards: dict[str, PresetCard] = {}
        self._count = 0

    def add_card(self, card: PresetCard) -> None:
        card.clicked.connect(self._on_click)
        self._cards[card.key] = card
        self._grid.addWidget(card, self._count // self._columns, self._count % self._columns)
        self._count += 1

    def _on_click(self, key: str) -> None:
        self.set_value(key)
        self.selected.emit(key)

    def set_value(self, key: str) -> None:
        for k, card in self._cards.items():
            card.set_selected(k == key)

    def value(self) -> str | None:
        for k, card in self._cards.items():
            if card._selected:
                return k
        return None

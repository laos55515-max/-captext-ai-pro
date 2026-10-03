"""Общая настройка тестов: headless Qt и единое QApplication на сессию."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session")
def qapp():
    """QApplication (не QGuiApplication!) — иначе QWidget-тесты падают с abort."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app

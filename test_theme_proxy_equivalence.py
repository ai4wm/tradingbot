# -*- coding: utf-8 -*-
"""프록시의 파이썬 `data()`를 없애고 테마 칸 처리를 `StockModel`로 옮긴 뒤에도 값이 같은지 본다.

옛 구현은 `git show HEAD:gui.py`(프록시에 `data()`가 있던 커밋)를 임시 모듈로 불러 와 같은
입력을 넣고, 모든 칸 × 주요 역할 × 테마 정렬 켬·끔의 값을 견준다. 그 커밋이 없으면(이력이
바뀐 환경) 건너뛴다.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

ROOT = Path(__file__).resolve().parent
OLD_REV = "cbe2080"          # 프록시에 파이썬 data()가 있던 마지막 커밋 계열(ceadb74 포함)


def load_old():
    try:
        text = subprocess.check_output(
            ["git", "show", "ceadb74:gui.py"], cwd=ROOT, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    path = Path(tempfile.mkdtemp()) / "gui_old.py"
    path.write_bytes(text)
    spec = importlib.util.spec_from_file_location("gui_old", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gui_old"] = module
    sys.path.insert(0, str(ROOT))
    spec.loader.exec_module(module)
    return module


def build(module):
    folder = tempfile.mkdtemp()
    settings = QSettings(os.path.join(folder, "t.ini"), QSettings.IniFormat)
    with patch.object(module, "QSettings", return_value=settings):
        screen = module.ConditionScreen()
    stocks = {
        "000001": ("로봇", "2차전지"), "000002": ("로봇",), "000003": ("바이오",),
        "000004": (), "000005": ("로봇", "AI"), "000006": ("건축자재",),
    }
    for index, code in enumerate(stocks):
        screen.model.add_stock(code, {
            "name": f"종목{index}", "price": 1_000 + index, "base": 1_000,
            "upper": 1_300, "lower": 700, "vol": 10_000 * (index + 1)})
    labels = {c: names for c, names in stocks.items() if names}
    screen.model.set_theme_labels(labels)
    screen.proxy.set_theme_labels(labels)
    screen.model.set_news_marks(
        {"000001": ("2차전지",), "000005": ("AI",)}, {"000001", "000005"},
        {"000005": ("AI",)}, {"000001": ("로봇 공급 계약", "T", "x")})
    screen.model.theme_leaders = {"000001"}
    screen.model.theme_singletons = {"000006"}
    return screen


def value_key(value):
    """QColor·QFont는 비교가 안 되니 눈에 보이는 값으로 바꾼다."""
    if isinstance(value, QColor):
        return ("color", value.name().lower())
    if isinstance(value, QFont):
        return ("font", value.bold())
    try:
        return ("v", str(value))
    except Exception:  # noqa: BLE001
        return ("v", repr(value))


def snapshot(module, screen, theme_mode):
    screen.proxy.theme_mode = theme_mode
    screen.proxy.invalidate()
    if theme_mode:
        screen.proxy._refresh_theme_sort_keys()
    else:
        screen.proxy.refresh_theme_keys()
    roles = [Qt.DisplayRole, Qt.UserRole, Qt.ForegroundRole, Qt.BackgroundRole,
             Qt.FontRole, Qt.ToolTipRole, Qt.TextAlignmentRole, module.THEME_COLOR_ROLE]
    out = {}
    for row in range(screen.proxy.rowCount()):
        for column in range(screen.proxy.columnCount()):
            index = screen.proxy.index(row, column)
            code = screen.model.codes[screen.proxy.mapToSource(index).row()]
            for role in roles:
                out[(code, module.FIELDS[column], int(role))] = value_key(
                    screen.proxy.data(index, role))
    return out


def demo():
    QApplication.instance() or QApplication([])
    old = load_old()
    if old is None:
        print("skip (옛 커밋 없음)")
        return
    new_screen, old_screen = build(gui), build(old)
    compared = 0
    for theme_mode in (False, True):
        a = snapshot(gui, new_screen, theme_mode)
        b = snapshot(old, old_screen, theme_mode)
        assert a.keys() == b.keys()
        diff = [(k, a[k], b[k]) for k in a if a[k] != b[k]]
        assert not diff, (theme_mode, diff[:5])
        compared += len(a)
    # 프록시가 더는 파이썬 data()를 갖지 않는다 — 그래야 값 요청이 한 번만 파이썬을 지난다.
    assert "data" not in gui.TieredProxy.__dict__
    print(f"ok (테마 칸·묶음색 값이 옛 프록시와 같음 {compared:,}건, 프록시 파이썬 data() 없음)")


if __name__ == "__main__":
    demo()

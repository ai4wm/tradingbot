"""분리 시계가 분석창 없이 켜지고 꺼지는지 본다. 임시 ini만 쓴다."""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton

import main as _main
import rank as _rank

_main._beep = _rank._beep = lambda *a, **k: None


def demo():
    app = QApplication.instance() or QApplication([])
    path = os.path.join(tempfile.mkdtemp(), "layout.ini")
    settings = QSettings(path, QSettings.IniFormat)
    clock = _main.ClockController(settings)
    button = QPushButton()
    button.setCheckable(True)
    clock.changed.connect(
        lambda on: _main._set_checked_quietly(button, on))
    button.toggled.connect(clock.set_on)

    button.setChecked(True)
    assert clock.is_on() and clock._clock_window.isVisible()
    assert settings.value("clock_detached") == "true"
    assert "<div" in clock._analysis_clock_label.text()

    clock._clock_window.close()  # 닫기 = 끄기, 버튼도 따라 꺼진다
    app.processEvents()
    assert not clock.is_on() and not button.isChecked()
    assert settings.value("clock_detached") == "false"

    clock.set_on(True)
    assert button.isChecked()
    _main._SHUTDOWN[0] = True  # 앱 종료 중 닫힘은 켜짐 상태를 남긴다
    try:
        clock._clock_window.close()
        assert settings.value("clock_detached") == "true"
    finally:
        _main._SHUTDOWN[0] = False
    print("ok")


if __name__ == "__main__":
    demo()

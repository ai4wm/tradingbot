# -*- coding: utf-8 -*-
"""뉴스 알림음이 체크 하나로 전부 켜지고 꺼지는지 검사.

전에는 실시간 뉴스 탭과 텔레그램 탭에 `소리` 체크가 따로 있었다. 실시간 쪽을
꺼도 텔레그램은 계속 울렸다. 네이버는 실시간 쪽 체크를 보고 있어 셋이 제각각
이었다. 실시간 뉴스 탭의 체크 하나로 묶는다.
"""
import io
import os
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.realtime_news_tab import RealtimeNewsTabMixin

SOURCES = ("ui/realtime_news_tab.py", "ui/stock_news_tab.py",
           "ui/telegram_news_tab.py")


def check_switch():
    enabled = RealtimeNewsTabMixin.news_sound_enabled
    off = types.SimpleNamespace(
        _ls_news_sound=types.SimpleNamespace(isChecked=lambda: False))
    on = types.SimpleNamespace(
        _ls_news_sound=types.SimpleNamespace(isChecked=lambda: True))
    assert enabled(on) is True
    assert enabled(off) is False
    # 탭이 만들어지기 전에 뉴스가 들어올 수 있다. 그때는 끈 것으로 본다.
    assert enabled(types.SimpleNamespace()) is False
    print("스위치   : 체크 True/False/없음 → True/False/False")


def check_call_sites():
    """알림음을 내는 세 곳이 모두 이 스위치를 본다."""
    for name in SOURCES:
        text = Path(name).read_text(encoding="utf-8")
        assert "_beep(" in text, name
        assert "self.news_sound_enabled()" in text, name
        # 탭별 체크가 남아 있으면 하나만 꺼도 다른 쪽이 울린다.
        assert "_telegram_sound" not in text, name
        assert "_ls_news_sound.isChecked()" not in text, name
    print(f"호출부   : {len(SOURCES)}개 파일 모두 스위치 경유")


def demo():
    check_switch()
    check_call_sites()
    print("ok")


if __name__ == "__main__":
    demo()

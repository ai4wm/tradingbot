# -*- coding: utf-8 -*-
"""알림음 종류별 끄기와 파일 지정을 검사한다.

`소리` 체크는 전체 스위치 그대로고 그 아래를 가른다. 거래소 공시는 하루
20여 건인데 기사는 4,000건이라, 기사 알림만 끄고 공시만 남길 수 있어야
한다(2026-09-18에 그래서 넣었다).

사용자 `layout.ini`를 건드리면 안 되므로 임시 폴더에서 돈다.
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="alert_sound_")
os.chdir(_TMP)  # QSettings("layout.ini")가 여기 쓰이게 한다
sys.path.insert(0, _PROJECT)

from PySide6.QtWidgets import QApplication  # noqa: E402

import rank  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def demo_mute():
    """끈 종류는 생성음까지 안 난다. 파일만 비우면 대체음이 울린다."""
    played = []
    rank.set_alert_sound_muted("krx_disclosure_top", True)
    assert rank.alert_sound_muted("krx_disclosure_top")
    # 옆 종류는 그대로여야 한다.
    assert not rank.alert_sound_muted("krx_disclosure")

    original = rank.winsound.PlaySound
    rank.winsound.PlaySound = lambda *a, **k: played.append(a[0])
    try:
        rank._beep("krx_disclosure_top")
        assert played == [], played          # 껐으므로 아무것도 안 난다
        rank._beep("krx_disclosure")
        assert len(played) == 1, played      # 안 끈 것은 난다
    finally:
        rank.winsound.PlaySound = original
        rank.set_alert_sound_muted("krx_disclosure_top", False)
    assert not rank.alert_sound_muted("krx_disclosure_top")
    print("ok (종류별 끄기)")


def demo_file_override():
    kind = "krx_disclosure"
    default = rank.KIWOOM_ALERT_FILES[kind]
    assert rank.alert_sound_path(kind) == default

    custom = os.path.join(_TMP, "내소리.wav")
    with open(custom, "wb") as handle:
        handle.write(rank._tone_wav(rank.TONES[kind]))
    rank.set_alert_sound(kind, custom)
    assert str(rank.alert_sound_path(kind)) == custom

    # 파일이 사라지면 기본으로 돌아간다. 없는 경로를 계속 물고 있으면
    # 그 종류만 조용히 안 울린다.
    os.remove(custom)
    assert rank.alert_sound_path(kind) == default

    rank.set_alert_sound(kind, None)
    assert rank.alert_sound_path(kind) == default
    print("ok (파일 지정·복귀)")


def demo_dialog():
    dialog = rank.SoundSettingsDialog()
    assert dialog._table.rowCount() == len(rank.SOUND_LABELS)
    kinds = [kind for kind, _ in rank.SOUND_LABELS]
    # 제3자배정이 맨 위다. 제일 센 신호를 제일 먼저 찾게 한다.
    assert kinds[0] == "krx_disclosure_top", kinds[0]
    # 창에 올린 종류는 전부 실제로 울리는 것이어야 한다.
    for kind in kinds:
        assert kind in rank.TONES, kind
    # 옛 이름 호환용은 창에 안 올린다.
    assert "ls_news" not in kinds and "telegram_news" not in kinds

    # 체크를 끄면 그 자리에서 저장된다.
    check = dialog._checks["jumsang"]
    check.setChecked(False)
    assert rank.alert_sound_muted("jumsang")
    check.setChecked(True)
    assert not rank.alert_sound_muted("jumsang")
    print(f"ok (설정창) {len(kinds)}종류")


if __name__ == "__main__":
    try:
        demo_mute()
        demo_file_override()
        demo_dialog()
    finally:
        os.chdir(_PROJECT)
        shutil.rmtree(_TMP, ignore_errors=True)

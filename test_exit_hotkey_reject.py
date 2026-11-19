# -*- coding: utf-8 -*-
"""청산키 전역 등록이 거부됐을 때 화면이 거짓말을 안 하는지 검사.

청산키는 앱이 뒤에 있을 때 쓰는 물건이다. 등록에 실패했는데 칸에 키가 남으면
눌러도 아무 일이 없다는 것을 모른다. 2026-09-09 224060에 F12를 걸었을 때가
그랬다. 윈도우가 F12를 디버거용으로 영구 예약해 RegisterHotKey가 절대
받아주지 않는다(같은 세션에서 F9는 됐다).

키충돌 표시는 알림이라 다시 걸거나 해제하면 사라져야 한다. 남으면 주문상태
칸에 저장돼 켤 때마다 되살아난다.
"""
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다

CODE = "224060"
SPEC = {"key": 0x01000038, "modifiers": 0, "text": "", "label": "F12"}


class FakeModel:
    def __init__(self):
        self.exit_hotkeys = {}
        self.cancel_hotkeys = {}
        self.order_status = {}

    def set_order_status(self, code, text, cancellable=False):
        if text:
            self.order_status[code] = text
        else:
            self.order_status.pop(code, None)


class FakeScreen:
    prefix = ""

    def __init__(self):
        self.model = FakeModel()
        self.refreshed = []

    def _refresh_exit_hotkey_cell(self, code):
        self.refreshed.append(code)

    refresh_exit_hotkey_cell = _refresh_exit_hotkey_cell

    def _release_excluded(self, code):
        pass


class FakeHotkeys:
    def __init__(self, ok, refuse=()):
        self.ok = ok
        self.refuse = set(refuse)   # 다른 프로그램이 잡고 있는 키
        self.unregistered = []
        self.live = {}              # token -> 키 이름 (지금 등록된 것)

    def register(self, token, spec, payload):
        self.unregister(token)
        ok = self.ok and str(spec.get("label") or "") not in self.refuse
        if ok:
            self.live[token] = str(spec.get("label") or "")
        return ok

    def unregister(self, token):
        self.unregistered.append(token)
        self.live.pop(token, None)


def _app(ok: bool, refuse=()):
    app = main.App.__new__(main.App)
    app._exit_hotkey_specs = {}
    app._global_hotkeys = FakeHotkeys(ok, refuse)
    app.views = [types.SimpleNamespace(screen=FakeScreen())]
    app._save_order_settings = lambda: None
    for name in ("_set_global_exit_hotkey", "_clear_hotkey_conflict",
                 "_auto_assign_exit_hotkey", "_set_cancel_hotkey"):
        setattr(app, name, types.MethodType(getattr(main.App, name), app))
    return app


def demo():
    # 1) 거부되면 칸에서 키를 뺀다. gui가 먼저 넣어 둔 배정을 되돌리는 것이다.
    app = _app(ok=False)
    screen = app.views[0].screen
    screen.model.exit_hotkeys[CODE] = (0x01000038, "F12")
    app._set_global_exit_hotkey(screen, CODE, SPEC, persist=False)
    assert screen.model.exit_hotkeys == {}, screen.model.exit_hotkeys
    assert screen.model.order_status == {CODE: "키충돌"}, screen.model.order_status
    assert screen.refreshed == [CODE], screen.refreshed
    assert app._exit_hotkey_specs == {}, app._exit_hotkey_specs
    print("거부   : 칸에서 키를 빼고 키충돌 표시")

    # 2) 다음에 다른 키로 성공하면 키충돌은 사라진다.
    app = _app(ok=True)
    screen = app.views[0].screen
    screen.model.order_status[CODE] = "키충돌"
    screen.model.exit_hotkeys[CODE] = (0x01000038, "F9")
    app._set_global_exit_hotkey(screen, CODE, dict(SPEC, label="F9"),
                                persist=False)
    assert screen.model.order_status == {}, screen.model.order_status
    assert screen.model.exit_hotkeys, screen.model.exit_hotkeys  # 성공은 유지
    assert app._exit_hotkey_specs[""][CODE]["label"] == "F9"
    print("성공   : 키충돌 사라지고 배정 유지")

    # 3) 해제해도 사라진다. 다시 걸지 않고도 지울 길이 있어야 한다.
    app = _app(ok=True)
    screen = app.views[0].screen
    screen.model.order_status[CODE] = "키충돌"
    app._set_global_exit_hotkey(screen, CODE, None, persist=False)
    assert screen.model.order_status == {}, screen.model.order_status
    assert app._global_hotkeys.unregistered, app._global_hotkeys.unregistered

    # 진짜 주문상태는 건드리지 않는다.
    screen.model.order_status[CODE] = "접수"
    app._set_global_exit_hotkey(screen, CODE, None, persist=False)
    assert screen.model.order_status == {CODE: "접수"}, screen.model.order_status
    print("해제   : 키충돌만 지우고 주문상태는 그대로")

    check_auto_assign()
    print("ok")


def _label(app, code, prefix=""):
    return (app._exit_hotkey_specs.get(prefix) or {}).get(code, {}).get("label")


def check_auto_assign():
    """주문과 함께 청산키를 자동으로 건다. 후보 앞에서부터 빈 키를 집는다."""
    # 청산은 홀수 키, 짝 취소키는 바로 옆 짝수 키다(F1·F2, F3·F4 …).
    assert main.AUTO_EXIT_HOTKEY_LABELS == ("F1", "F3", "F5", "F7"), \
        main.AUTO_EXIT_HOTKEY_LABELS
    assert "F12" not in main.AUTO_EXIT_HOTKEY_LABELS, "윈도우 예약키는 후보 밖"

    # 첫 주문은 맨 앞 후보를 집는다.
    app = _app(ok=True)
    screen = app.views[0].screen
    app._auto_assign_exit_hotkey(screen, "000001")
    assert _label(app, "000001") == "F1", app._exit_hotkey_specs
    assert screen.model.exit_hotkeys["000001"][1] == "F1"

    # 다음 종목은 그다음 빈 키로 간다. 셋까지 차례대로다.
    app._auto_assign_exit_hotkey(screen, "000002")
    app._auto_assign_exit_hotkey(screen, "000003")
    assert [_label(app, c) for c in ("000001", "000002", "000003")] == \
        ["F1", "F3", "F5"], app._exit_hotkey_specs
    # 짝 취소키가 바로 옆 키로 같이 걸린다.
    assert screen.model.cancel_hotkeys == {
        "000001": "F2", "000002": "F4", "000003": "F6"}, screen.model.cancel_hotkeys
    assert sorted(app._global_hotkeys.live.values()) == [
        "F1", "F2", "F3", "F4", "F5", "F6"], app._global_hotkeys.live

    # 이미 걸린 종목은 다시 걸지 않는다. 손으로 고른 키를 덮으면 안 된다.
    app._auto_assign_exit_hotkey(screen, "000001")
    assert _label(app, "000001") == "F1"

    # F3이 풀리면 짝 F4도 같이 풀리고, 다음 주문은 앞으로 돌아가 F3을 집는다.
    app._set_global_exit_hotkey(screen, "000002", None)
    assert "000002" not in screen.model.cancel_hotkeys
    assert "F4" not in app._global_hotkeys.live.values(), app._global_hotkeys.live
    app._auto_assign_exit_hotkey(screen, "000004")
    assert _label(app, "000004") == "F3", app._exit_hotkey_specs
    assert screen.model.cancel_hotkeys["000004"] == "F4"

    # 취소키 칸에서 손으로 고른 키가 짝을 대신하고, 청산키 설정에 붙어 저장된다.
    app._set_custom_cancel_hotkey(screen, "000001", {
        "key": 0x01000037, "modifiers": 0, "text": "", "label": "F8"})
    assert screen.model.cancel_hotkeys["000001"] == "F8"
    assert app._exit_hotkey_specs[""]["000001"]["cancel"]["label"] == "F8"
    assert "F2" not in app._global_hotkeys.live.values(), app._global_hotkeys.live
    app._set_cancel_hotkey(screen, "000001", app._exit_hotkey_specs[""]["000001"])
    assert screen.model.cancel_hotkeys["000001"] == "F8", "재시작 복원도 손 지정 키"
    app._set_custom_cancel_hotkey(screen, "000001", None)      # 손으로 풂
    assert "000001" not in screen.model.cancel_hotkeys
    app._set_cancel_hotkey(screen, "000001", app._exit_hotkey_specs[""]["000001"])
    assert "000001" not in screen.model.cancel_hotkeys, "푼 것은 복원에도 안 살아남"
    app._set_global_exit_hotkey(screen, "000001", None)        # 청산키를 풀면
    assert "000001" not in app._exit_hotkey_specs[""]          # 손 지정도 같이

    # 취소키 전부 해제: 취소키만 풀고 청산키는 남긴다. 복원에도 안 살아난다.
    # 메뉴의 개수는 화면 함수를 바꿔 끼운 상태로 센다(자기 호출이면 메뉴가 안 뜸).
    screen.cancel_hotkey_count = app._cancel_hotkey_count
    assert screen.cancel_hotkey_count() == 2, screen.model.cancel_hotkeys
    app._clear_all_cancel_hotkeys()
    assert screen.model.cancel_hotkeys == {}, screen.model.cancel_hotkeys
    assert sorted(app._global_hotkeys.live.values()) == ["F3", "F5"], \
        app._global_hotkeys.live
    app._set_cancel_hotkey(screen, "000004", app._exit_hotkey_specs[""]["000004"])
    assert "000004" not in screen.model.cancel_hotkeys
    app._auto_assign_exit_hotkey(screen, "000008")             # 새 청산키엔 다시 짝
    assert screen.model.cancel_hotkeys == {"000008": "F2"}, screen.model.cancel_hotkeys

    # 다른 프로그램이 F1을 잡고 있으면 거부되고 다음 후보로 넘어간다.
    blocked = _app(ok=True, refuse={"F1"})
    blocked_screen = blocked.views[0].screen
    blocked._auto_assign_exit_hotkey(blocked_screen, "000005")
    assert _label(blocked, "000005") == "F3", blocked._exit_hotkey_specs
    # 실패한 F1의 흔적이 남으면 안 된다. 칸에도 키충돌에도.
    assert blocked_screen.model.exit_hotkeys["000005"][1] == "F3"
    assert blocked_screen.model.cancel_hotkeys == {"000005": "F4"}
    assert blocked_screen.model.order_status == {}, \
        blocked_screen.model.order_status

    # 후보가 전부 막히면 조용히 포기한다. 주문 자체를 막으면 안 된다.
    none_left = _app(ok=False)
    none_screen = none_left.views[0].screen
    none_left._auto_assign_exit_hotkey(none_screen, "000006")
    assert none_left._exit_hotkey_specs.get("", {}) == {}
    assert none_screen.model.order_status == {"000006": "키충돌"}, \
        none_screen.model.order_status
    # 짝 키만 막혀 있으면 청산키는 살리고 취소키만 충돌로 둔다.
    pair_blocked = _app(ok=True, refuse={"F2"})
    pb_screen = pair_blocked.views[0].screen
    pair_blocked._auto_assign_exit_hotkey(pb_screen, "000007")
    assert _label(pair_blocked, "000007") == "F1"
    assert pb_screen.model.cancel_hotkeys == {"000007": "충돌"}

    # 짝이 없는 키(F9, 수정키 조합)는 취소키를 걸지 않는다.
    assert main._cancel_key_label({"label": "F9", "modifiers": 0}) == ""
    assert main._cancel_key_label({"label": "F2", "modifiers": 0}) == ""
    assert main._cancel_key_label({"label": "F1", "modifiers": 0x4000000}) == ""
    assert main._cancel_key_label({"label": "F7", "modifiers": 0}) == "F8"
    check_capture_guard()
    print("자동배정: F1→F3→F5(짝 F2·F4·F6), 손 지정 취소키, 풀리면 앞으로, 막히면 건너뜀")


def check_capture_guard():
    """칸에서 새 키를 받는 중에 이미 쓰는 키를 누르면 그 종목을 청산하지 않는다."""
    fired = []
    app = main.App.__new__(main.App)
    screen = types.SimpleNamespace(_hotkey_capture_code="000002",
                                   _hotkey_capture_cancel=True)
    app.views = [types.SimpleNamespace(screen=screen)]
    app._emergency_exit = lambda *a: fired.append("exit")
    app._cancel_key_pressed = lambda *a: fired.append("cancel")
    shown, main.QToolTip.showText = main.QToolTip.showText, lambda *a: None
    try:
        main.App._on_global_exit_hotkey(app, (screen, "000001", "F1"))
        assert fired == [] and screen._hotkey_capture_code == "", fired
        main.App._on_global_exit_hotkey(app, (screen, "000001", "F2", "cancel"))
        assert fired == ["cancel"], fired          # 받기가 끝나면 평소대로
    finally:
        main.QToolTip.showText = shown


if __name__ == "__main__":
    demo()

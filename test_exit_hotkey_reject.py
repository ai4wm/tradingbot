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


class FakeHotkeys:
    def __init__(self, ok):
        self.ok = ok
        self.unregistered = []

    def register(self, token, spec, payload):
        return self.ok

    def unregister(self, token):
        self.unregistered.append(token)


def _app(ok: bool):
    app = main.App.__new__(main.App)
    app._exit_hotkey_specs = {}
    app._global_hotkeys = FakeHotkeys(ok)
    app.views = [types.SimpleNamespace(screen=FakeScreen())]
    for name in ("_set_global_exit_hotkey", "_clear_hotkey_conflict"):
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

    print("ok")


if __name__ == "__main__":
    demo()

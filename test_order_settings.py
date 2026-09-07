# -*- coding: utf-8 -*-
"""주문설정 저장·복원 검사.

3단매도와 자동취소는 조건이 맞으면 스스로 주문을 낸다. 그래서 저장은 하되
앱을 켤 때 자동으로 켜지지는 않는다. 복원 대기함에만 담고, 사용자가 복원
버튼을 눌러야 감시가 돈다. 자동으로 되살리면 켠 순간 잔량이 기준선을
스치기만 해도 매도가 나간다.

청산키도 앱이 뒤에 있어도 먹는 전역키다. 어제 배정이 남으면 오늘 화면에
없는 종목이 청산되므로 다 그날만 유효해야 한다.
"""
import json
import logging
import os
import types
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

import main

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다

INI = os.path.join(
    os.environ.get("TEMP", "."), "trading_bot_order_settings_test.ini")
CODE = "011330"
SETTING = {"at_upper": True, "first": 1_000_000, "second": 500_000,
           "third": 0, "first_ratio": -1.0, "second_ratio": 1.0,
           "third_ratio": 1.0, "market_sell": True}


class FakeModel:
    def __init__(self, codes):
        self.rows = {code: {} for code in codes}
        self.codes = list(codes)
        self.balance_sell_settings = {}
        self.balance_sell_stage = {}
        self.order_status = {}
        self.order_cancellable = set()
        self.armed = set()

    def set_account_auto_cancel_armed(self, code, armed):
        (self.armed.add if armed else self.armed.discard)(code)

    def set_order_status(self, code, text, cancellable=False):
        if code not in self.rows:
            return
        if text:
            self.order_status[code] = text
        else:
            self.order_status.pop(code, None)
        (self.order_cancellable.add if cancellable
         else self.order_cancellable.discard)(code)


class FakeScreen:
    def __init__(self, prefix, codes):
        self.prefix = prefix
        self.model = FakeModel(codes)
        self.restore_text = None

    def set_order_restore(self, summary):
        self.restore_text = summary

    def refresh_restored_cells(self):
        pass


class Stub:
    """`App`에서 주문설정 저장·복원에 필요한 부분만 세운 대역."""

    def __init__(self, codes=(CODE,)):
        self._settings = QSettings(INI, QSettings.IniFormat)
        self._account_auto_cancel_armed = set()
        self._balance_sell_settings = {}
        self._balance_sell_stage = {}
        self._balance_sell_date = {}
        self._exit_hotkey_specs = {}
        self._pending_order_restore = {}
        self.views = [types.SimpleNamespace(screen=FakeScreen("", codes))]
        for name in ("_save_order_settings", "_load_order_settings",
                     "_order_restore_summary", "_show_order_restore",
                     "_apply_order_restore", "_dismiss_order_restore",
                     "_restore_stock_order_settings"):
            setattr(self, name,
                    types.MethodType(getattr(main.App, name), self))


def seed(**values):
    settings = QSettings(INI, QSettings.IniFormat)
    settings.clear()
    for key, value in values.items():
        settings.setValue("order/" + key, json.dumps(value, ensure_ascii=False))
    settings.sync()


def demo():
    today = datetime.now().strftime("%Y%m%d")
    hotkeys = {"": {CODE: {"key": 1}}}

    # 1) 저장은 하되 켜지지는 않는다. 복원 대기함에만 담긴다.
    seed(auto_state={"date": today, "balance_sell": {CODE: SETTING},
                     "balance_stage": {CODE: 1},
                     "auto_cancel": [CODE],
                     "order_status": {"": {CODE: ["체결", True]}}},
         exit_hotkeys={"date": today, "specs": hotkeys})
    app = Stub()
    app._load_order_settings()
    assert app._balance_sell_settings == {}, app._balance_sell_settings
    assert app._account_auto_cancel_armed == set(), app._account_auto_cancel_armed
    assert app._pending_order_restore, app._pending_order_restore
    assert app._exit_hotkey_specs == hotkeys, app._exit_hotkey_specs
    summary = app._order_restore_summary()
    assert summary == "3단매도 1 · 자동취소 1 · 주문상태 1", summary

    # 2) 버튼을 눌러야 감시가 돈다. 진행도까지 그대로 돌아온다.
    app._show_order_restore()
    assert app.views[0].screen.restore_text == summary
    app._apply_order_restore()
    assert app._balance_sell_settings == {CODE: SETTING}, app._balance_sell_settings
    assert app._balance_sell_stage == {CODE: 1}, app._balance_sell_stage
    assert app._balance_sell_date == {CODE: today}, app._balance_sell_date
    assert app._account_auto_cancel_armed == {CODE}
    model = app.views[0].screen.model
    assert model.balance_sell_settings == {CODE: SETTING}, model.balance_sell_settings
    assert model.balance_sell_stage == {CODE: 1}, model.balance_sell_stage
    assert model.armed == {CODE}, model.armed
    assert model.order_status == {CODE: "체결"}, model.order_status
    assert model.order_cancellable == {CODE}, model.order_cancellable
    # 다 썼으면 버튼은 사라지고 두 번 눌러도 아무 일 없다.
    assert app.views[0].screen.restore_text == ""
    app._apply_order_restore()
    assert app._order_restore_summary() == ""

    # 3) 우클릭(무시)하면 되살리지 않고 저장분까지 버린다.
    seed(auto_state={"date": today, "balance_sell": {CODE: SETTING},
                     "auto_cancel": [CODE]})
    app = Stub()
    app._load_order_settings()
    assert app._pending_order_restore, app._pending_order_restore
    app._dismiss_order_restore()
    assert app._pending_order_restore == {}, app._pending_order_restore
    assert app._balance_sell_settings == {}, app._balance_sell_settings
    assert app.views[0].screen.restore_text == ""
    assert QSettings(INI, QSettings.IniFormat).value("order/auto_state") is None

    # 4) 복원도 무시도 안 한 채 또 껐다 켜도 저장분은 남아 있어야 한다.
    #    종료 저장이 빈 값으로 덮으면 두 번 재시작만으로 사라진다.
    seed(auto_state={"date": today, "balance_sell": {CODE: SETTING},
                     "auto_cancel": [CODE]})
    app = Stub()
    app._load_order_settings()
    app._save_order_settings()          # 종료 저장
    app = Stub()
    app._load_order_settings()          # 다시 실행
    assert app._pending_order_restore.get("balance_sell") == {CODE: SETTING}, \
        app._pending_order_restore

    # 5) 어제 저장분은 버린다. 기준 잔량도 진행도도 오늘과 무관하다.
    seed(auto_state={"date": "20260101", "balance_sell": {CODE: SETTING},
                     "auto_cancel": [CODE]},
         exit_hotkeys={"date": "20260101", "specs": hotkeys})
    app = Stub()
    app._load_order_settings()
    assert app._pending_order_restore == {}, app._pending_order_restore
    assert app._exit_hotkey_specs == {}, app._exit_hotkey_specs
    disk = QSettings(INI, QSettings.IniFormat)
    assert disk.value("order/auto_state") is None
    assert disk.value("order/exit_hotkeys") is None

    # 6) 날짜 없는 옛 형식 청산키도 언제 배정한 것인지 모르므로 버린다.
    seed(exit_hotkeys=hotkeys)
    app = Stub()
    app._load_order_settings()
    assert app._exit_hotkey_specs == {}, app._exit_hotkey_specs

    # 7) 저장 내용 확인. 화면의 주문상태까지 함께 남는다.
    seed()
    app = Stub()
    app._account_auto_cancel_armed = {CODE}
    app._balance_sell_settings = {CODE: SETTING}
    app._balance_sell_stage = {CODE: 2}
    app._exit_hotkey_specs = dict(hotkeys)
    app.views[0].screen.model.set_order_status(CODE, "접수", True)
    app._save_order_settings()
    disk = QSettings(INI, QSettings.IniFormat)
    state = json.loads(disk.value("order/auto_state"))
    assert state["date"] == today, state
    assert state["balance_sell"] == {CODE: SETTING}, state
    assert state["balance_stage"] == {CODE: 2}, state
    assert state["auto_cancel"] == [CODE], state
    assert state["order_status"] == {"": {CODE: ["접수", True]}}, state
    # 옛 키는 남기지 않는다.
    assert disk.value("order/auto_cancel_armed") is None
    assert disk.value("order/balance_sell") is None

    # 8) 켜 둔 것이 없으면 빈 값으로 남아 다음 실행에 버튼이 안 뜬다.
    seed()
    app = Stub()
    app._exit_hotkey_specs = dict(hotkeys)
    app._save_order_settings()
    app = Stub()
    app._load_order_settings()
    assert app._pending_order_restore == {}, app._pending_order_restore
    assert app._order_restore_summary() == ""

    # 9) 저장값이 깨져도 앱은 죽지 않고 빈 상태로 시작한다.
    broken = QSettings(INI, QSettings.IniFormat)
    broken.clear()
    broken.setValue("order/auto_state", "{not json")
    broken.setValue("order/exit_hotkeys", "{not json")
    broken.sync()
    app = Stub()
    app._load_order_settings()
    assert app._pending_order_restore == {}, app._pending_order_restore
    assert app._exit_hotkey_specs == {}, app._exit_hotkey_specs

    QSettings(INI, QSettings.IniFormat).clear()
    if os.path.exists(INI):
        os.remove(INI)
    print("ok")


if __name__ == "__main__":
    demo()

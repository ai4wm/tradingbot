# -*- coding: utf-8 -*-
"""3단매도가 화면과 같은 출처의 매수잔량만 보는지 확인한다.

`[NXT]등락률순위` 창이 같은 종목을 `_NX`로 등록하면 NXT 단독 잔량 틱이
함께 온다. 그것이 통합 기준선에 섞이면 3단매도가 일찍 발동한다.
"""
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main  # noqa: E402
import ws  # noqa: E402


def _app(suffix="_AL"):
    app = types.SimpleNamespace(
        views=[], ws=types.SimpleNamespace(real_suffix=suffix),
        checked=[], split=[], _balance_sell_settings={"028300": {}},
        _position_book={}, _bid_split_logged={}, _bid_split_last={})
    app._check_balance_sell = lambda code, qty: app.checked.append(qty)
    real_split = main.App._log_bid_split
    app._log_bid_split = lambda code, fields: (
        app.split.append(code), real_split(app, code, fields))
    return app


def demo_only_same_source_reaches_balance_sell():
    app = _app("_AL")
    on_real = main.App._on_real
    on_real(app, "028300", {"bid_qty": 1_798_266, "_real_suffix": "_AL"})
    on_real(app, "028300", {"bid_qty": 600_000, "_real_suffix": "_NX"})
    on_real(app, "028300", {"bid_qty": 1_700_000})           # REST 백필
    assert app.checked == [1_798_266, 1_700_000], app.checked

    krx = _app("")
    on_real(krx, "028300", {"bid_qty": 500_000, "_real_suffix": ""})
    on_real(krx, "028300", {"bid_qty": 1_798_266, "_real_suffix": "_AL"})
    assert krx.checked == [500_000], krx.checked
    print("ok (NXT 단독 틱은 3단매도에 안 들어간다)")


def demo_split_fields_and_log():
    code, fields = ws.parse_real_item({
        "item": "028300_AL", "type": "0D",
        "values": {"71": "1798266", "6054": "600000", "6076": "1198266"}})
    assert fields["bid_qty"] == 1_798_266
    assert fields["bid_qty_krx"] == 600_000
    assert fields["bid_qty_nxt"] == 1_198_266

    app = _app("_AL")
    fields["_real_suffix"] = "_AL"
    main.App._on_real(app, code, fields)
    main.App._on_real(app, code, fields)     # 30초 안 두 번째는 안 찍는다
    assert app.split == [code, code] and len(app._bid_split_logged) == 1
    assert app._bid_split_last[code] == (600_000, 1_198_266)  # 발동 줄에 실린다
    print("ok (거래소별 잔량 매핑 · 30초 표본)")


if __name__ == "__main__":
    demo_only_same_source_reaches_balance_sell()
    demo_split_fields_and_log()

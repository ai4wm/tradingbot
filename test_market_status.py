# -*- coding: utf-8 -*-
"""저유동성 정보와 조건/조회순위 종목명 배경·글씨 대비를 검증한다."""
import asyncio
import io
import logging
import os
import types
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

import api
import gui
import liquidity_info
import main
import rank

logging.disable(logging.CRITICAL)


def record(market, code="004545", low=b"Y", group=b"ST"):
    row = bytearray(b" " * liquidity_info.RECORD_LENGTHS[market])
    row[:9] = code.encode("ascii").ljust(9)
    row[9:21] = b"KR7004541009"
    row[21:61] = "깨끗한나라우".encode("cp949").ljust(40)
    row[61:63] = group
    row[77:78] = low
    return bytes(row)


def master_zip(market, code):
    content = b"\r\n".join([
        record(market, f"{i:06d}", b"N") for i in range(500)
    ] + [record(market, code)])
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(f"{market}_code.mst", content)
    return output.getvalue()


class MarketStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def test_master_bytes_and_bad_data(self):
        for market in liquidity_info.RECORD_LENGTHS:
            content = b"\n".join([
                record(market), record(market, "005930", b"N"),
                record(market, "F70100034", b"N", b"BC"),
            ])
            self.assertEqual(
                liquidity_info.parse_low_liquidity(content, market), {"004545"})
            for bad in (b"", record(market)[:-1], record(market, low=b"X")):
                with self.assertRaises(ValueError):
                    liquidity_info.parse_low_liquidity(bad, market)

    def test_downloads_both_markets_without_auth(self):
        requested = []

        def handle(request):
            self.assertNotIn("authorization", request.headers)
            requested.append(str(request.url))
            market = "kospi" if "kospi_" in request.url.path else "kosdaq"
            code = "111111" if market == "kospi" else "222222"
            return httpx.Response(200, content=master_zip(market, code))

        real_client = httpx.AsyncClient
        with patch.object(liquidity_info.httpx, "AsyncClient", side_effect=(
                lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))):
            result = asyncio.run(liquidity_info.fetch_low_liquidity())
        self.assertEqual(result, {"111111", "222222"})
        self.assertEqual(len(requested), 2)

    def test_kiwoom_small_preference_and_other_warnings(self):
        async def request(_api_id, body):
            if body["mrkt_tp"] == "10":
                return {"list": []}
            return {"list": [
                {"code": "004545", "marketCode": "0", "listCount": "366160",
                 "orderWarning": "0", "state": "관리종목"},
                {"code": "451760", "orderWarning": "3"},
                {"code": "011155", "orderWarning": "2"},
                {"code": "005930", "marketCode": "0", "listCount": "5800000000",
                 "orderWarning": "0"},
            ]}

        market = asyncio.run(api.RestClient.market_info(
            types.SimpleNamespace(request=request)))
        self.assertEqual(market.low_liquidity, {"004545"})
        self.assertIn("004545", market.single)
        self.assertEqual(market.short_overheat, {"451760"})
        self.assertEqual(market.liquidation, {"011155"})

    def test_background_merge_and_failed_download(self):
        market = api.MarketInfo(low_liquidity={"004545"}, single={"004545"})
        updates, ranks = [], []
        app = types.SimpleNamespace(
            _market=market, views=["main", "extra"],
            _inject_market=updates.append,
            _rank=types.SimpleNamespace(set_market=ranks.append))
        with patch.object(main, "fetch_low_liquidity", AsyncMock(return_value={"134380"})):
            asyncio.run(main.App._load_low_liquidity(app))
        self.assertEqual(market.low_liquidity, {"004545", "134380"})
        self.assertEqual(market.single, {"004545"})  # 시세·주문 판정은 보완 조회로 바꾸지 않음
        self.assertEqual(updates, ["main", "extra"])
        self.assertEqual(ranks, [market])
        with patch.object(main, "fetch_low_liquidity", AsyncMock(side_effect=OSError("offline"))):
            asyncio.run(main.App._load_low_liquidity(app))
        self.assertEqual(market.low_liquidity, {"004545", "134380"})

    def test_tooltip_and_rank_background_flags(self):
        market = api.MarketInfo(
            low_liquidity={"004545", "134380"}, single={"004545", "451760"},
            short_overheat={"451760"}, admin={"004545"})
        stock = gui.StockModel()
        stock.add_stocks([("004545", {"name": "깨끗한나라우"}),
                          ("451760", {"name": "컨텍"}),
                          ("134380", {"name": "미원화학"}),
                          ("005930", {"name": "삼성전자"})])
        view = types.SimpleNamespace(screen=types.SimpleNamespace(model=stock))
        app = types.SimpleNamespace(
            views=[view], rest=types.SimpleNamespace(suffix=""),
            _market=market, _limit_cnt=None)
        main.App._inject_market(app, view)
        idx = stock.index(0, gui.NAME_COL)
        self.assertTrue(idx.data(gui.LOW_LIQUIDITY_ROLE))
        self.assertFalse(idx.data(gui.SHORT_OVERHEAT_ROLE))
        self.assertEqual(idx.data(Qt.ForegroundRole), gui.ADMIN)
        self.assertIn("관리종목", idx.data(Qt.ToolTipRole))
        self.assertIn("30분 단일가", stock.index(0, gui.NAME_COL).data(Qt.ToolTipRole))
        self.assertTrue(stock.index(1, gui.NAME_COL).data(gui.SHORT_OVERHEAT_ROLE))
        self.assertTrue(stock.index(2, gui.NAME_COL).data(gui.LOW_LIQUIDITY_ROLE))
        self.assertNotIn("30분", stock.index(2, gui.NAME_COL).data(Qt.ToolTipRole))
        self.assertFalse(stock.index(3, gui.NAME_COL).data(gui.LOW_LIQUIDITY_ROLE))

        ranked = rank.RankModel()
        ranked.set_rows([{"code": "451760", "name": "컨텍"},
                         {"code": "004545", "name": "깨끗한나라우"}])
        rank.RankScreen.set_market(types.SimpleNamespace(model=ranked), market)
        self.assertTrue(ranked.index(0, rank.FIELDS.index("name")).data(gui.SHORT_OVERHEAT_ROLE))
        self.assertTrue(ranked.index(1, rank.FIELDS.index("name")).data(gui.LOW_LIQUIDITY_ROLE))
        self.assertIn("30분 단일가", ranked.index(1, rank.FIELDS.index("name")).data(Qt.ToolTipRole))

    def test_both_themes_background_priority_and_text_contrast(self):
        def luminance(color):
            channels = (color.redF(), color.greenF(), color.blueF())
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                      for v in channels]
            return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))

        for dark in (True, False):
            palette = QPalette()
            palette.setColor(QPalette.Base, QColor("#202124" if dark else "#ffffff"))
            palette.setColor(QPalette.Text, QColor("#eeeeee" if dark else "#111111"))
            for kind in ("short", "low", "nxt", "both"):
                expected = (
                    gui.SHORT_OVERHEAT_BG_DARK if dark else gui.SHORT_OVERHEAT_BG_LIGHT
                ) if kind in ("short", "both") else (
                    gui.LOW_LIQUIDITY_BG_DARK if dark else gui.LOW_LIQUIDITY_BG_LIGHT
                ) if kind == "low" else (
                    gui.NXT_EXCLUDE_BG_DARK if dark else gui.NXT_EXCLUDE_BG_LIGHT)
                for foreground in (palette.text().color(), gui.ADMIN, gui.ADMIN_KOSDAQ,
                                   gui.PURPLE):
                    model = gui.StockModel()
                    model.add_stocks([("004545", {"name": "깨끗한나라우"})])
                    if foreground in (gui.ADMIN, gui.ADMIN_KOSDAQ):
                        model.admin.add("004545")
                    if foreground in (gui.ADMIN_KOSDAQ, gui.PURPLE):
                        model.kosdaq.add("004545")
                    if kind in ("short", "both"):
                        model.short_overheat.add("004545")
                    if kind in ("low", "both"):
                        model.low_liquidity.add("004545")
                    model.nxt.add("004545")  # 단기과열/저유동성이 NXT보다 우선
                    image = QImage(220, 28, QImage.Format_ARGB32)
                    image.fill(Qt.transparent)
                    painter = QPainter(image)
                    option = QStyleOptionViewItem()
                    option.rect = QRect(0, 0, 220, 28)
                    option.palette = palette
                    gui.NameDelegate().paint(painter, option, model.index(0, gui.NAME_COL))
                    painter.end()
                    self.assertEqual(image.pixelColor(210, 5), expected)
                    bg, fg = gui.name_marker_colors(
                        palette, foreground, kind in ("short", "both"),
                        kind in ("low", "both"), True)
                    low, high = sorted((luminance(bg), luminance(fg)))
                    self.assertGreaterEqual((high + .05) / (low + .05), 4.5,
                                            (dark, kind, fg.name()))
                    if foreground != palette.text().color():
                        # 경고색이 배경 위에서 흰/검 글씨로 바뀌면 안 된다
                        # (2026-11-11 대호특수강우: 코스닥 관리종목 분홍이 흰색).
                        self.assertGreater(fg.saturation(), 60, (dark, kind, fg.name()))
        self.assertEqual(len(gui.COLUMNS), 23)
        self.assertEqual(len(rank.COLUMNS), 6)
        self.assertNotIn("market_status", gui.FIELDS)
        self.assertNotIn("market_status", rank.FIELDS)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""DB 연상(limit_up_events.consecutive_days)이 매매거래정지일을 건너뛰는지 검사한다.

2026-03-25 우리로 5연상 → 03-26 정지(거래량 0) → 03-27 상한가가 1로 저장됐다.
앱 화면 연상(api.daily_summary)은 정지일을 건너뛰어 6으로 셌다.
운영 DB를 건드리지 않도록 임시 DB에서 돈다.
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

from analysis_db import initialize, save_krx_market_day


def _day(date, rate, volume):
    return [{"BAS_DD": date, "ISU_SRT_CD": "046970", "ISU_ABBRV": "우리로",
             "FLUC_RT": str(rate), "TDD_CLSPRC": "1000", "CMPPREVDD_PRC": "0",
             "ACC_TRDVOL": str(volume), "_market": "KOSDAQ"}]


def demo():
    temp = Path(tempfile.mkdtemp(prefix="streak_halt_"))
    db_path = temp / "test.db"
    try:
        initialize(db_path)
        for date, rate, volume in [
            ("20260323", 29.9, 100), ("20260324", 29.9, 100),
            ("20260325", 29.9, 100), ("20260326", 0, 0),      # 정지
            ("20260327", 29.9, 100), ("20260330", 5.0, 100),  # 끊김
            ("20260331", 29.9, 100),
        ]:
            save_krx_market_day(_day(date, rate, volume), db_path)
        with sqlite3.connect(db_path) as connection:
            got = dict(connection.execute(
                "SELECT trade_date, consecutive_days FROM limit_up_events"))
        assert got == {"20260323": 1, "20260324": 2, "20260325": 3,
                       "20260327": 4, "20260331": 1}, got
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print("ok")


if __name__ == "__main__":
    demo()

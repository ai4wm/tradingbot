# -*- coding: utf-8 -*-
"""여러 테마에 걸린 종목이 실제로 함께 움직이는 테마로 묶이는지 확인한다.

SK이터닉스처럼 태양광·ESS 양쪽에 속한 종목이 자기 혼자 최고 등락률이면
최고 등락률이 동점이 된다. 이때 이름순이 아니라 평균 등락률로 갈려야 한다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

TARGET = "475150"  # SK이터닉스 — 태양광·ESS 동시 편입
# 태양광은 블록으로 강세, ESS는 대상 종목만 튀는 상황.
RATES = {TARGET: 21.0,
         "SOLAR1": 9.0, "SOLAR2": 8.0,
         "ESS1": 0.5, "ESS2": 0.3}
LABELS = {TARGET: ("고체산화물 연료전지(SOFC)", "전력저장장치(ESS)",
                   "태양광에너지", "풍력에너지"),
          "SOLAR1": ("태양광에너지",), "SOLAR2": ("태양광에너지",),
          "ESS1": ("전력저장장치(ESS)",), "ESS2": ("전력저장장치(ESS)",)}


class _Model:
    """프록시가 읽는 부분만 흉내낸다. 상한가는 없는 상태(upper=0)."""

    def __init__(self):
        self.codes = list(RATES)
        self.rows = {c: {"rate": r, "price": 0, "upper": 0, "time": "",
                         "name": c} for c, r in RATES.items()}
        self.theme_leaders = set()
        self.theme_singletons = set()

    def index(self, *_):
        return None

    class _Signal:
        @staticmethod
        def emit(*_):
            pass

    dataChanged = _Signal()


def demo():
    app = QApplication.instance() or QApplication([])
    proxy = gui.ConditionSortProxy() if hasattr(gui, "ConditionSortProxy") else None
    if proxy is None:  # 클래스명이 바뀌면 프록시를 이름으로 찾는다.
        import inspect
        proxy = next(
            obj for _, obj in inspect.getmembers(gui, inspect.isclass)
            if hasattr(obj, "_refresh_theme_sort_keys"))()
    model = _Model()
    proxy.setSourceModel(None)
    proxy.sourceModel = lambda: model  # 실제 QAbstractItemModel 없이 검증
    proxy.theme_mode = True
    proxy.set_theme_labels(LABELS)

    proxy._refresh_theme_sort_keys()
    picked = proxy._theme_group_keys[TARGET]
    assert picked == ("theme", "태양광에너지"), (
        f"함께 움직이는 테마가 아닌 곳에 묶임: {picked}")

    # 반대 상황이면 ESS로 붙어야 한다(평균 기준이 방향을 가리는지 확인).
    for code in ("SOLAR1", "SOLAR2"):
        model.rows[code]["rate"] = 0.2
    for code in ("ESS1", "ESS2"):
        model.rows[code]["rate"] = 9.0
    proxy._refresh_theme_sort_keys()
    picked = proxy._theme_group_keys[TARGET]
    assert picked == ("theme", "전력저장장치(ESS)"), picked

    # 나머지가 다 잠잠하면 자기 혼자인 SOFC/풍력이 이겨서는 안 된다.
    # (평균을 자기 포함으로 내면 단독 테마 평균이 곧 자기 등락률이라 늘 이긴다.)
    for code in ("ESS1", "ESS2"):
        model.rows[code]["rate"] = 0.1
    proxy._refresh_theme_sort_keys()
    picked = proxy._theme_group_keys[TARGET]
    assert picked[1] not in ("고체산화물 연료전지(SOFC)", "풍력에너지"), (
        f"단독 테마가 대표로 붙음: {picked}")
    del app
    print("ok")


if __name__ == "__main__":
    demo()

# -*- coding: utf-8 -*-
"""오늘 처음 붙은 테마에만 ★를 다는지 검사.

재료는 며칠에 걸쳐 다시 기사화된다. 마지막 언급으로는 오늘 새로 터진 재료와
재탕을 가를 수 없다(2026-08-25 이건산업: 자사주는 8/24 공시의 재탕이고 그날의
새 재료는 자산매각이다). 표식을 갈라 두면 순서를 억지로 맞출 필요가 없다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui  # noqa: E402

LABELS = ("자산매각", "자사주", "건축자재")
NEWS = ("자산매각", "자사주")


def demo():
    # 오늘 처음 붙은 것은 ★, 며칠 된 뉴스 테마는 •, 분류 테마는 표식 없음.
    text = gui._theme_cell_text(LABELS, NEWS, fresh_themes=("자산매각",))
    assert text == "★자산매각·•자사주·건축자재", text

    # 오늘 새 재료가 없으면 전부 •다. 재탕이 오늘 재료로 위장하지 못한다.
    stale = gui._theme_cell_text(LABELS, NEWS)
    assert stale == "•자산매각·•자사주·건축자재", stale

    # 같은 날 함께 터진 재료는 둘 다 ★다. 순서를 가릴 이유가 없다.
    both = gui._theme_cell_text(LABELS, NEWS, fresh_themes=NEWS)
    assert both == "★자산매각·★자사주·건축자재", both

    # 사건 재료는 여전히 맨 앞이다.
    priority = gui._theme_cell_text(
        ("건축자재", "인수합병"), ("인수합병",), fresh_themes=("인수합병",))
    assert priority == "★인수합병·건축자재", priority

    # 오늘 재료 기사가 없는 종목은 앞에 표식이 붙는다.
    none_today = gui._theme_cell_text(LABELS, NEWS, has_news=False)
    assert none_today.startswith(gui.NO_NEWS_MARK), none_today
    print("ok")


if __name__ == "__main__":
    demo()

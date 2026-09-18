# -*- coding: utf-8 -*-
"""한국거래소 공시 판별과 재료급 선별을 확인한다.

LS는 공시를 기사와 같은 줄로 흘려보낸다. 하루 351건 전부 공시인데 그중
소리를 낼 값어치가 있는 것은 6~8건뿐이라, 무엇을 울리고 무엇을 넘길지가
이 화면의 전부다. 실제 제목은 2026-08-20~09-18 수신분에서 가져왔다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.realtime_news_tab import (  # noqa: E402
    DEFAULT_LS_NEWS_SEARCH_PRESETS, LATEST_NEWS_BANNER_COLORS,
    is_krx_disclosure, is_material_disclosure, is_top_disclosure)

KRX = "한국거래소"

# (제목, 재료급인가) — 앞 넷이 실제로 상한가로 이어진 공시다.
SAMPLES = (
    ("(주)미투온 유상증자결정(제3자배정)", True),
    ("(주)앤씨앤 유상증자결정(제3자배정)", True),
    ("(주)미투온 최대주주 변경을 수반하는 주식양수도 계약 체결", True),
    ("(주)엑시온그룹 타법인주식및출자증권취득결정", True),
    ("(주)아무개 단일판매ㆍ공급계약체결", True),
    ("(주)아무개 풍문 또는 보도에 대한 해명(미확정)", True),
    ("(주)아무개 투자판단 관련 주요경영사항", True),
    # 매일 나오는 정기물 — 울리면 안 된다.
    ("(주)아무개 기업설명회(IR) 개최(안내공시)", False),
    ("(주)아무개 주주명부폐쇄기간 또는 기준일 설정", False),
    ("[투자주의]소수계좌 거래집중 종목", False),
    ("(주)아무개 주주총회소집결의(임시주주총회)", False),
    ("(주)아무개 대표이사변경", False),
    ("(주)아무개 공매도 과열종목 지정(공매도 거래 금지 적용)", False),
    # 정정은 원본(3.7건/일)보다 많다(4.3건/일). 빼지 않으면 소리가 두 배다.
    ("(주)아무개 (정정)단일판매ㆍ공급계약체결", False),
    ("(주)아무개 (정정)타인에대한채무보증결정", False),
)


def demo_source():
    assert is_krx_disclosure(KRX)
    assert is_krx_disclosure(f" {KRX} ")
    for other in ("이데일리", "연합뉴스", "인포스탁데일리", "", None):
        assert not is_krx_disclosure(other), other
    # 출처가 거래소가 아니면 제목이 공시 모양이어도 공시가 아니다.
    assert not is_material_disclosure(
        "이데일리", "미투온, 279억 규모 제3자배정 유상증자 결정")
    print("ok (출처 판별)")


def demo_material():
    for title, expected in SAMPLES:
        got = is_material_disclosure(KRX, title)
        assert got is expected, (title, got, expected)
    print(f"ok (재료급 선별) {len(SAMPLES)}건")


def demo_top():
    """최상급은 「유상증자결정(제3자배정)」 원본뿐이다.

    2026-05-01~09-18 실측, 5영업일 안 상한가(무작위 2만 표본 기저 1.41%):

        유상증자결정 원본   135건  20.0%   ← 최상급
        (정정)              261건   8.8%   ← 재료급
        발행결과             63건   6.3%   ← 재료급
        추가상장            122건   2.5%   ← 재료급, 기저와 거의 같다
        철회                 14건   7.1%   ← 악재, 최상급에서 뺀다

    한때 이 넷을 뭉뚱그려 15배라 불렀다. 실제로 센 것은 원본 하나뿐이고
    추가상장이 최상급 소리의 34%를 먹고 있었다 — 2026-09-18 빨간 소리
    세 번이 전부 추가상장이었고, 그날 원본은 0건이었다.
    """
    tops = (
        "(주)미투온 유상증자결정(제3자배정)",
        "(주)앱튼 유상증자결정(제3자배정)",
        "(주)하이딥 유상증자결정(제3자배정-현물출자)",
        "(주)본느 유상증자결정(제3자배정-소액공모)",
    )
    for title in tops:
        assert is_top_disclosure(KRX, title), title
        # 최상급은 재료급의 부분집합이다. 소리 분기가 뒤집히면 안 된다.
        assert is_material_disclosure(KRX, title), title

    # 한 칸 아래에 머무는 것들. 소리는 나되 빨간 소리는 아니다.
    seconds = (
        # 이미 끝난 증자의 뒤처리다. 재료가 아니라 물량 출회다.
        "(주)앤로보틱스 추가상장(유상증자(제3자배정))",
        "(주)한탑 추가상장(유상증자(제3자배정))",
        "(주)엑시온그룹 증권 발행결과(자율공시)(제3자배정 유상증자)",
        "(주)아무개 유상증자최종발행가액확정(제3자배정-소액공모)",
        # 제3자배정이 아닌 유상증자.
        "(주)아무개 유상증자결정(주주배정후 실권주 일반공모)",
        # 2026-09-18 앤씨앤(2연상 점상 중)·사토시홀딩스가 이 모양이었다.
        # 전에는 정정을 통째로 빼서 화면에도 안 뜨고 소리도 안 났다.
        "(주)앤씨앤 (정정)유상증자결정(제3자배정)",
        "사토시홀딩스(주) (정정)유상증자결정(제3자배정)",
        # 철회는 악재다. 붙여 쓴 것도 띄어 쓴 것도 빨간 소리를 주지 않는다.
        "(주)아이톡시 (정정)유상증자결정(제3자배정-철회)",
        "퓨쳐메디신 주식회사 기타 주요경영사항(유상증자결정(제3자배정) 철회)",
        "(주)캐리 기타 주요경영사항(제3자배정 유상증자 결정 철회)",
    )
    for title in seconds:
        assert not is_top_disclosure(KRX, title), title

    # 출처가 기사면 제목이 같아도 아니다.
    assert not is_top_disclosure("이데일리", tops[0])
    print(f"ok (제3자배정 최상급) {len(tops)}건 · 한 칸 아래 {len(seconds)}건")


def demo_correction_still_sounds():
    """제3자배정 유상증자결정의 정정만 정정 제외를 면제받는다.

    8.8%로 기저(1.41%)의 6배다. 발행가나 납입일이 바뀌면 그것이 재료다.
    나머지 정정은 그대로 조용하다 — 원본보다 많아서 소리가 두 배가 된다.
    """
    assert is_material_disclosure(KRX, "(주)앤씨앤 (정정)유상증자결정(제3자배정)")
    silent = (
        "(주)아무개 (정정)단일판매ㆍ공급계약체결",
        "(주)아무개 (정정)타법인주식및출자증권취득결정",
        "(주)아무개 (정정)유상증자결정(주주배정후 실권주 일반공모)",
        "(주)아무개 (정정)최대주주변경",
    )
    for title in silent:
        assert not is_material_disclosure(KRX, title), title
    print(f"ok (제3자배정 정정만 통과) 조용한 정정 {len(silent)}건")


def demo_banner_color():
    # 전광판이 공시를 기사와 다른 색으로 낸다. 네 값이 다 있어야 한다.
    seen = set()
    for key in ("LS", "KRX", "KRX_TOP"):
        colors = LATEST_NEWS_BANNER_COLORS[key]
        assert len(colors) == 4, (key, colors)
        assert colors not in seen, key
        seen.add(colors)
    print("ok (전광판 색) 3단계")


def demo_presets():
    # 기본 즐겨찾기는 그대로 검색창에 넣으면 동작해야 한다.
    from ui.realtime_news_tab import parse_ls_news_search_query
    for query in DEFAULT_LS_NEWS_SEARCH_PRESETS:
        include, exclude = parse_ls_news_search_query(query)
        assert include, query
        assert any(KRX in term for group in include for term in group), query
    # 첫 식은 최상급만 남긴다. 이게 제일 센 신호라 맨 앞이다.
    # 「유상증자결정」이 있어야 추가상장·발행결과가 빠지고, 정정은 들어온다.
    include, exclude = parse_ls_news_search_query(
        DEFAULT_LS_NEWS_SEARCH_PRESETS[0])
    flat = {term for group in include for term in group}
    assert {"제3자배정", "유상증자결정"} <= flat, flat
    assert "철회" in exclude, exclude
    assert "정정" not in exclude, exclude
    # ETF·ETN을 걷어내는 식도 하나 있어야 한다(잡음이 99건/일).
    assert any(
        {"ETF", "ETN"} <= set(parse_ls_news_search_query(query)[1])
        for query in DEFAULT_LS_NEWS_SEARCH_PRESETS)
    print("ok (즐겨찾기 기본값)")


def demo_star_never_deletes():
    """★은 넣기만 한다.

    토글로 뒀더니 즐겨찾기를 고른 직후 검색창에 그 식이 그대로 들어가 있어서,
    저장하려고 누른 ★이 그것을 지웠다(2026-09-18: 4개가 2개로 줄었다).
    빼기는 목록 우클릭으로 옮겼다.
    """
    import shutil
    import tempfile
    import types
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit
    from ui.realtime_news_tab import RealtimeNewsTabMixin as Mixin

    QApplication.instance() or QApplication([])
    project, temp = os.getcwd(), tempfile.mkdtemp(prefix="ls_presets_")
    os.chdir(temp)  # 사용자 layout.ini를 건드리지 않는다
    try:
        # __dict__로 꺼내야 staticmethod 래퍼가 살아 있다.
        screen = type("Fake", (), {
            name: Mixin.__dict__[name] for name in (
                "_as_preset_list",
                "_ls_news_search_preset_list",
                "_reload_ls_news_search_presets",
                "_save_ls_news_search_presets",
                "_add_ls_news_search_preset")})()
        screen._settings = QSettings("layout.ini", QSettings.IniFormat)
        screen._ls_news_search = QLineEdit()
        screen._ls_news_search_presets = QComboBox()
        screen.statusBar = lambda: types.SimpleNamespace(
            showMessage=lambda *a, **k: None)

        before = list(DEFAULT_LS_NEWS_SEARCH_PRESETS)
        # 즐겨찾기를 고르면 검색창에 그 식이 그대로 들어간다. 그 상태로 ★.
        screen._ls_news_search.setText(before[0])
        for _ in range(3):
            screen._add_ls_news_search_preset()
            assert screen._ls_news_search_preset_list() == before, \
                screen._ls_news_search_preset_list()

        # 새 식은 맨 앞에 들어간다.
        screen._ls_news_search.setText("한국거래소 무상증자")
        screen._add_ls_news_search_preset()
        assert screen._ls_news_search_preset_list() == (
            ["한국거래소 무상증자"] + before)

        # 빈 검색어로는 아무 일도 없다.
        screen._ls_news_search.setText("   ")
        screen._add_ls_news_search_preset()
        assert len(screen._ls_news_search_preset_list()) == len(before) + 1

        # 저장분에 기본값이 빠져 있으면 다시 채운다. ★ 오작동으로 잃어버린
        # 것을 재시작만으로 되찾게 하려는 것이다.
        screen._settings.setValue(
            "analysis_ls_news_search_presets", ["내가 넣은 식"])
        screen._settings.sync()
        healed = screen._ls_news_search_preset_list()
        assert healed == ["내가 넣은 식"] + before, healed

        # 손으로 뺀 기본값만 안 돌아온다.
        screen._settings.setValue(
            "analysis_ls_news_search_presets_dropped", [before[-1]])
        screen._settings.sync()
        after = screen._ls_news_search_preset_list()
        assert before[-1] not in after, after
        assert len(after) == len(before), after
        print("ok (★은 지우지 않는다 · 기본값 자동 복구)")
    finally:
        os.chdir(project)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    demo_source()
    demo_material()
    demo_top()
    demo_correction_still_sounds()
    demo_banner_color()
    demo_presets()
    demo_star_never_deletes()

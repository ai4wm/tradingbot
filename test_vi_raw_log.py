# -*- coding: utf-8 -*-
"""VI 원본 기록 검사.

1h는 등록 종목만이 아니라 시장 전체가 온다(2026-09-08 실측: 36분에 미등록
535건, 등록 120건). 상한가는 +10% 정적VI를 반드시 지나므로 이 파일이 화면
밖 종목까지 담은 상한가 후보 목록이 된다.

FID를 골라 담으면 안 된다. 9068이 발동/해제인지 정적/동적 구분인지 아직
확정이 아니라, 골라 담은 뒤에는 확인할 방법이 없다.
"""
import json
import logging
import os
import shutil
import tempfile

import ws

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다

RAW = {"9001": "189860_AL", "9068": "1", "1221": "13650", "1225": "2"}


def _client():
    client = ws.WSClient.__new__(ws.WSClient)
    client._reg_codes = {("189860", None): 1}
    client._vi_raw_failed = False
    client._vi_seen = {}
    client.on_vi = None
    return client


def check_active_flag():
    """발동/해제는 1224(해제 시각)로 가른다. 9068은 정적/동적 구분이다.

    9068을 발동 플래그로 쓰던 시절에는 정적VI가 해제돼도 화면이 안 꺼지고
    동적VI는 발동해도 안 켜졌다. 2026-09-09 수신 1,114건에서 9068은 1225와
    100% 일치했다.
    """
    fire = dict(RAW, **{"1223": "090403", "1224": "000000"})
    release = dict(RAW, **{"1223": "090403", "1224": "090611"})
    assert ws._vi_active(fire) is True, fire
    assert ws._vi_active(release) is False, release
    # 동적VI(9068=2)도 발동이면 켜져야 한다. 예전에는 여기서 꺼졌다.
    assert ws._vi_active(dict(fire, **{"9068": "2", "1225": "동적"})) is True
    # 정적VI 해제는 꺼져야 한다. 예전에는 여기서 켜진 채로 남았다.
    assert ws._vi_active(dict(release, **{"9068": "1"})) is False
    # 값이 아예 없으면 발동으로 본다. 끄는 신호로 오해하면 안 된다.
    assert ws._vi_active({}) is True
    print("발동·해제  : 1224로 가름 (9068은 정적/동적)")


def demo():
    check_active_flag()
    client = _client()
    saved, ws.VI_RAW_DIR = ws.VI_RAW_DIR, tempfile.mkdtemp()
    try:
        client._on_vi({"type": "1h", "values": RAW})
        client._on_vi({"type": "1h", "values": dict(RAW, **{"9001": "0011A0_AL"})})
        with open(ws._vi_raw_path(), encoding="utf-8") as file:
            rows = [json.loads(line) for line in file]

        # 원본이 통째로 남는다. 고른 FID가 아니라 values 전체다.
        assert rows[0]["v"] == RAW, rows[0]
        assert rows[0]["code"] == "189860", rows[0]
        assert len(rows[0]["ts"]) == 8, rows[0]  # HH:MM:SS

        # 등록 종목과 화면 밖 종목을 가른다. 화면 밖이 오늘의 발견이다.
        assert rows[0]["mine"] is True, rows[0]
        assert rows[1]["mine"] is False, rows[1]
        assert rows[1]["code"] == "0011A0", rows[1]  # 글자 섞인 코드

        # 파일 이름은 하루 하나. 이어 붙는다.
        assert os.path.basename(ws._vi_raw_path()).startswith("vi_raw_")
        assert len(rows) == 2, rows

        # 1h는 같은 이벤트를 두 번씩 보낸다. 콜백은 한 번만 불러야 조회가
        # 두 번 나가지 않는다. 원본 기록은 두 벌 다 남긴다.
        twice = _client()
        got = []
        twice.on_vi = lambda *args: got.append(args)
        event = {"type": "1h", "values": dict(RAW, **{"1223": "090403",
                                                      "1224": "000000"})}
        twice._on_vi(event)
        twice._on_vi(event)
        assert got == [("189860", True, 13650)], got
        with open(ws._vi_raw_path(), encoding="utf-8") as file:
            assert len(file.readlines()) == 4, "원본은 두 벌 다 남아야 한다"
        # 해제는 발동과 다른 이벤트다. 접히면 안 된다.
        twice._on_vi({"type": "1h", "values": dict(
            RAW, **{"1223": "090403", "1224": "090611"})})
        assert len(got) == 2, got
        assert got[1] == ("189860", False, 13650), got
        # 기억은 최근 것만 둔다. 하루치를 다 들고 있으면 안 된다.
        for n in range(ws.VI_DEDUP_KEEP + 50):
            twice._on_vi({"type": "1h", "values": dict(
                RAW, **{"1223": f"{n:06d}", "1224": "000000"})})
        assert len(twice._vi_seen) == ws.VI_DEDUP_KEEP, len(twice._vi_seen)
        print("중복 수신  : 콜백 1회, 원본은 두 벌 다 남김")

        # 쓸 수 없어도 웹소켓 수신은 멈추면 안 된다. 경고는 한 번뿐이다.
        ws.VI_RAW_DIR = os.path.join(ws.VI_RAW_DIR, "vi_raw_x.jsonl")
        open(ws.VI_RAW_DIR, "w").close()   # 디렉터리 자리에 파일 -> makedirs 실패
        client._on_vi({"type": "1h", "values": RAW})
        assert client._vi_raw_failed is True
    finally:
        shutil.rmtree(os.path.dirname(ws.VI_RAW_DIR), ignore_errors=True)
        ws.VI_RAW_DIR = saved

    print("ok")


if __name__ == "__main__":
    demo()

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
    client.on_vi = None
    return client


def demo():
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

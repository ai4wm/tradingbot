# -*- coding: utf-8 -*-
"""도메인/상수. 모의<->실전은 IS_MOCK 하나로 전환."""
import os

from dotenv import load_dotenv

load_dotenv()

IS_MOCK = False  # 실전. 주문은 UI의 '주문허용'을 사용자가 직접 체크해야만 전송된다.

HOST = "https://mockapi.kiwoom.com" if IS_MOCK else "https://api.kiwoom.com"
# 웹소켓 경로. ⚠️ 문서 확인: 포트/경로가 다르면 여기만 고치면 됨.
WS_URL = ("wss://mockapi.kiwoom.com:10000" if IS_MOCK else "wss://api.kiwoom.com:10000") \
    + "/api/dostk/websocket"

APPKEY = os.getenv("KIWOOM_APPKEY", "")
SECRETKEY = os.getenv("KIWOOM_SECRETKEY", "")

REAL_REG_LIMIT = 95  # 실시간 등록 종목 수 상한
REST_RATE_LIMIT = 1.0  # 초당 REST 호출 수 (TR당 1req/s)
TICK_MAX_PAGES = 6  # 상한가 진입 틱조회 최대 페이지 (초과 시 분봉 분단위 폴백). 6페이지=최악 ~6초

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
DART_API_KEY = os.getenv("DART_API_KEY", "")
KRX_API_KEY = os.getenv("KRX_API_KEY", "")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# 키움 공지 점검 시간(KST). 이 구간에는 토큰/REST/웹소켓 요청을 보내지 않고
# 종료 시각에 한 번만 연결을 시작한다. 다음 공지가 나오면 이 목록에 추가한다.
KIWOOM_MAINTENANCE_WINDOWS = (
    ("2026-08-01 13:00", "2026-08-02 05:00"),
)

# LS증권 실시간 뉴스(NWS). 키움 시세 연결과는 별도 OAuth/웹소켓을 사용한다.
LS_HOST = "https://openapi.ls-sec.co.kr:8080"
LS_WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"
LS_APPKEY = os.getenv("LS_APPKEY", "")
LS_APPSECRET = os.getenv("LS_APPSECRET", "")

# 앱이 꺼져 있던 동안 우분투의 24시간 LS 수집 DB에 쌓인 제목을 증분 동기화한다.
# SSH 별칭은 PowerShell에서 사용하는 ``ssh w``와 동일하다.
LS_NEWS_SYNC_ENABLED = os.getenv(
    "LS_NEWS_SYNC_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
LS_NEWS_SYNC_SSH_HOST = os.getenv("LS_NEWS_SYNC_SSH_HOST", "w").strip()
LS_NEWS_SYNC_DB_PATH = os.getenv(
    "LS_NEWS_SYNC_DB_PATH",
    "/home/ubuntu/trading-bot/data/nws.sqlite3",
).strip()
LS_NEWS_SYNC_BATCH_SIZE = 500

# 텔레그램 채널 뉴스(Telethon 사용자 API). 세션 파일은 Git에 커밋하지 않는다.
TG_API_ID = os.getenv("TG_API_ID", "").strip()
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_PHONE = os.getenv("TG_PHONE", "").strip()
TG_CHANNELS = tuple(
    name.strip() for name in os.getenv("TG_CHANNELS", "").split(",")
    if name.strip()
)
TG_SESSION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "telegram.session")
# 채널별 첫 실행 소급 수집 건수. 이후에는 저장된 마지막 메시지 ID부터 채운다.
TG_FIRST_RUN_LIMIT = 50
TG_BACKFILL_LIMIT = 500

REAL_REG_LIMIT = 95  # 실시간 등록 종목 수 상한
REST_RATE_LIMIT = 1.0  # 초당 REST 호출 수 (TR당 1req/s)
TICK_MAX_PAGES = 6  # 상한가 진입 틱조회 최대 페이지 (초과 시 분봉 분단위 폴백). 6페이지=최악 ~6초

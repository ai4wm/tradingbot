# Trading Bot

키움 REST·웹소켓을 중심으로 조건검색, 실시간 시세, 주문·잔고 관리와 시장 분석을 제공하는 Windows용 PySide6 데스크톱 앱입니다. LS증권 실시간 뉴스, 네이버 종목뉴스, KRX·DART 데이터도 함께 사용합니다.

## 주요 기능

- 키움 조건검색 및 KRX·NXT 실시간 시세
- 조회순위, 등락률·거래량·거래대금 순위
- 예상체결, VI, 상한가 진입시각과 체결 강도 지표
- 분할매수, 주문 취소, 보유종목 단계별 매도와 비상청산 보조
- LS증권 실시간 뉴스 및 누락 뉴스 서버 동기화
- 네이버 관심종목 뉴스와 종목토론실
- KRX 상한가 이력, DART 공시, 테마·수급·시장 지표 분석
- 상한가 후보 예측 및 워크포워드 검증
- 창별 레이아웃, 테마와 글꼴 크기 저장

실제 주문은 화면의 `주문허용`을 직접 체크한 동안에만 전송됩니다. 실계좌에서 사용하기 전에 주문 수량과 계좌 설정을 반드시 확인해야 합니다.

## 실행 환경

- Windows
- Python 3.11 이상
- 키움 REST API 사용 권한
- 선택 기능에 따라 LS증권, DART, KRX, 네이버 API 자격 증명

## 설치 및 실행

```powershell
cd D:\Python\trading-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`에 사용할 API 값을 입력한 뒤 실행합니다.

```powershell
.\.venv\Scripts\python.exe main.py
```

콘솔 없이 실행하려면 `run.bat`을 사용할 수 있습니다.

## 환경 변수

| 이름 | 용도 |
|---|---|
| `KIWOOM_APPKEY`, `KIWOOM_SECRETKEY` | 키움 REST·웹소켓 및 주문 |
| `LS_APPKEY`, `LS_APPSECRET` | LS증권 실시간 뉴스 |
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 네이버 관심종목 뉴스 |
| `DART_API_KEY` | 전자공시 수집 |
| `KRX_API_KEY` | KRX 데이터 수집 |
| `TG_API_ID`, `TG_API_HASH` | 텔레그램 채널 뉴스(Telethon 사용자 API) |
| `TG_PHONE` | 텔레그램 최초 로그인 전화번호(생략 시 실행 중 입력) |
| `TG_CHANNELS` | 수집할 텔레그램 채널 목록(쉼표 구분) |
| `LS_NEWS_SYNC_ENABLED` | 외부 LS 뉴스 DB 누락분 동기화 여부 |
| `LS_NEWS_SYNC_SSH_HOST` | 외부 수집 서버의 SSH 호스트 또는 별칭 |
| `LS_NEWS_SYNC_DB_PATH` | 외부 서버의 뉴스 SQLite 경로 |

`.env`, 텔레그램을 포함한 각종 세션 파일, API 키와 계좌 정보는 Git에 커밋하지 마십시오.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `main.py` | 앱 조립, 창 수명주기, 분석 화면과 작업 제어 |
| `gui.py` | 조건검색 표, 주문·잔고 화면과 표시 모델 |
| `api.py` | 키움 REST 클라이언트와 시장 조회 |
| `ws.py` | 키움 웹소켓, 조건검색과 실시간 등록 |
| `order.py` | 주문 분할 및 주문 처리 |
| `analysis_db.py` | 분석용 SQLite 스키마와 조회·저장 |
| `ls_news_ws.py` | LS증권 실시간 뉴스 수신 |
| `ls_news_server_sync.py` | 앱 종료 중 누락된 LS 뉴스 보완 |
| `prediction_model.py` | 상한가 후보 학습과 예측 |
| `walkforward_validation.py` | 예측 모델 워크포워드 검증 |

검증 스크립트를 실행하면 `data/walkforward_v1_v4_report.md`와 JSON 결과가 새로 생성됩니다.

## 로컬 파일

- `.env`: API 자격 증명
- `layout.ini`: 창 위치, 열 너비와 사용자 설정
- `bot.log`: 실행 로그
- `data/market_analysis.db`: 분석 데이터베이스

위 파일은 실행 환경의 로컬 상태이며 저장소 문서의 기준으로 사용하지 않습니다.

## 기본 점검

```powershell
.\.venv\Scripts\python.exe -m py_compile config.py api.py ws.py gui.py rank.py main.py analysis_db.py prediction_model.py ls_news_ws.py ls_news_server_sync.py
git diff --check
```

워크포워드 보고서를 다시 생성하려면 다음을 실행합니다.

```powershell
.\.venv\Scripts\python.exe walkforward_validation.py
```

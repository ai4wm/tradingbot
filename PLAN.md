# 키움 REST API 실시간 조건검색 화면 — 구현 계획서

> 이 문서만 보고 구현할 수 있도록 작성됨. 구현자는 각 단계를 순서대로 진행하고,
> "⚠️ 문서 확인" 표시가 있는 항목은 키움 OpenAPI 포털(https://openapi.kiwoom.com)의
> REST API 문서와 대조해 정확한 필드명/메시지 포맷을 확정한 뒤 코딩할 것.

## 1. 목표와 범위

**1차 목표 (이 계획서의 범위):** 영웅문 [0156] 조건검색실시간 화면 재현.
웹소켓으로 조건검색 편입/이탈 + 편입 종목의 실시간 시세를 받아 GUI 그리드에 표시한다.

**범위 밖 (백로그, 지금 만들지 말 것):** 자동매매 주문, 체결통보, 잔고 조회,
다중창(MDI), 상세 컬럼(L일봉H 차트, 예상체결). §9 참조.

## 2. 현재 상태

- `gui.py` **완성됨. 수정 금지** (버그 수정 제외). PySide6 그리드 화면 전체가 들어 있다.
- 파이썬 3.11 / PySide6 6.11.1 설치됨. `python gui.py`로 더미 데이터 데모가 실행된다.
- 나머지 파일(`config.py`, `api.py`, `ws.py`, `main.py`)은 없음 — 이 계획서로 만든다.

### gui.py 인터페이스 계약 (웹소켓 계층이 호출할 메서드)

`ConditionScreen` 위젯의 메서드 3개만 호출하면 화면이 갱신된다. 전부 **GUI 스레드에서 호출**해야 한다 (qasync 사용 시 자동 충족, §3).

```python
screen.on_included(code: str, data: dict)   # 조건 편입 → 행 추가
screen.on_excluded(code: str)               # 조건 이탈 → 행 제거 (이탈삭제 체크 시)
screen.on_tick(code: str, fields: dict)     # 실시간 시세 → 바뀐 필드만 부분 갱신
```

dict 키 (필요한 것만 넣으면 됨):

| 키 | 타입 | 의미 |
|---|---|---|
| `rate` | float | 등락률 (예: 29.96) |
| `name` | str | 종목명 |
| `sector` | str | 업종명 |
| `price` | int | 현재가 (절대값, 부호 제거) |
| `prev_vol` | int | 전일거래량 |
| `vol` | int | 누적거래량 |
| `ask_qty` | int | 매도잔량 |
| `bid_qty` | int | 매수잔량 |
| `time` | str | 편입시간 "HH:MM:SS" |

기타 UI 연결점: `screen.condition_combo` (조건식 목록 채우기),
`screen.start_btn` (등록 토글 버튼, checkable), `screen.auto_remove` (이탈삭제 체크박스).
`gui.py`의 `_demo()` 함수와 `__main__` 블록의 `_demo(screen)` 호출은 실데이터 연결 후 삭제.

## 3. 아키텍처 결정 (변경하지 말 것)

1. **단일 스레드 + qasync.** `websockets`/`httpx`는 asyncio 기반이므로, `qasync`로
   Qt 이벤트 루프 안에서 asyncio를 돌린다. 웹소켓 수신 → `screen.on_*()` 직접 호출이
   같은 스레드라 큐·락·시그널 마샬링이 전부 불필요하다. **별도 통신 스레드를 만들지 말 것.**
2. **화면 = 위젯.** `ConditionScreen`이 자기완결형이므로 다중창이 필요해지면
   나중에 `QMdiArea`에 넣기만 하면 된다. 지금은 `QMainWindow` 단일 창.
3. **모의투자(mockapi) 우선.** 실전 도메인은 config 상수 전환으로만.

추가 라이브러리: `qasync`, `websockets`, `httpx`, `python-dotenv` (`pip install`).
이 외 새 의존성 금지.

## 4. 파일 구조

```
trading-bot/
├── .env          # KIWOOM_APPKEY, KIWOOM_SECRETKEY (git 제외)
├── config.py     # 도메인·상수·모의/실전 스위치 (~20줄)
├── api.py        # 토큰 발급/갱신 + REST 호출 (ka10001 등)
├── ws.py         # 웹소켓: 접속/LOGIN/PING/재접속 + 조건검색 + 실시간 시세
├── gui.py        # (완성) ConditionScreen
└── main.py       # qasync 조립 + 이벤트 배선
```

파일을 더 쪼개지 말 것. 400줄 넘는 파일이 생기면 그때 분리.

## 5. 사전 준비 (사용자 확인 필요)

1. 키움증권 OpenAPI 포털에서 REST API 사용 신청, **모의투자 신청**, appkey/secretkey 발급.
2. `.env` 작성:
   ```
   KIWOOM_APPKEY=발급받은키
   KIWOOM_SECRETKEY=발급받은시크릿
   ```
3. HTS(영웅문)에서 조건식이 1개 이상 저장돼 있어야 CNSRLST에 목록이 나온다.
4. **1일차 리스크 확인 (최우선):** 모의투자 웹소켓에서 조건검색(CNSRREQ)이 실제로
   동작하는지 Step 3 완료 즉시 확인. 모의투자 미지원이면 실전 키로 **조회만** 하는
   방식으로 전환해야 하므로 이후 계획에 영향이 크다.

## 6. 구현 단계

### Step 1 — config.py + api.py (토큰)

- `config.py`: 도메인 상수.
  ```python
  IS_MOCK = True
  HOST = "https://mockapi.kiwoom.com" if IS_MOCK else "https://api.kiwoom.com"
  WS_URL = ("wss://mockapi.kiwoom.com:10000" if IS_MOCK else "wss://api.kiwoom.com:10000") + "/api/dostk/websocket"
  ```
  ⚠️ 문서 확인: 정확한 도메인/포트/웹소켓 경로.
- `api.py`: 토큰 발급 `POST {HOST}/oauth2/token`, body
  `{"grant_type": "client_credentials", "appkey": ..., "secretkey": ...}`.
  응답의 `token`, `expires_dt`(yyyyMMddHHmmss) 저장. ⚠️ 문서 확인: au10001 정확한 스펙.
- 갱신 규칙: 만료 10분 전이면 재발급. REST 호출이 401이면 1회 재발급 후 재시도.
- 검증: `python api.py` 실행 시 토큰 발급 성공 출력하는 `__main__` 블록 포함.

### Step 2 — ws.py 웹소켓 코어

- `websockets.connect(WS_URL)` → 접속 직후 `{"trnm": "LOGIN", "token": <접근토큰>}` 전송,
  `return_code == 0` 확인. ⚠️ 문서 확인: LOGIN 패킷 스펙.
- 서버가 `{"trnm": "PING", ...}`을 보내면 **받은 메시지 그대로 echo** 응답.
- 수신 루프: JSON 파싱 → `trnm`별 디스패치 (LOGIN/PING/CNSRLST/CNSRREQ/REAL/...).
- **재접속:** 연결이 끊기면 지수 백오프(1s→2s→...→최대 30s)로 무한 재접속.
  재접속 성공 시 LOGIN부터 다시 하고, 등록돼 있던 조건검색·실시간 시세를 자동 재등록.
  이게 이 파일에서 제일 중요한 로직이다 — 대충 만들지 말 것.
- 로깅: 표준 `logging`으로 송수신 원문을 DEBUG, 접속/끊김/재등록을 INFO로 기록.
  `bot.log` 파일 핸들러 (새벽 사고 추적용).

### Step 3 — 조건검색

- 목록조회: `{"trnm": "CNSRLST"}` 전송 → 응답 `data`가 `[일련번호, 조건명]` 목록.
- 실시간 등록: `{"trnm": "CNSRREQ", "seq": <일련번호>, "search_type": "1", "stex_tp": "K"}`.
  응답에 **초기 편입 종목 리스트**가 오고, 이후 편입/이탈이 실시간 메시지로 온다.
  ⚠️ 문서 확인: 요청 필드명, 응답 내 종목코드 필드, 편입("I")/이탈("D") 구분 필드.
- 해제: `{"trnm": "CNSRCLR", "seq": <일련번호>}`.
- 콜백 시그니처 (main.py가 연결):
  ```python
  ws_client.on_condition_event = callback(code: str, is_insert: bool, time_str: str)
  ```

### Step 4 — 실시간 시세 + 종목 정보

- 편입 종목을 실시간 시세에 등록:
  `{"trnm": "REG", "grp_no": "1", "refresh": "1", "data": [{"item": [종목코드들], "type": ["0B", "0D"]}]}`.
  0B(주식체결)에서 현재가·등락률·누적거래량, 0D(주식호가잔량)에서 매도/매수 총잔량.
  ⚠️ 문서 확인: REAL 메시지의 필드 ID(예: 10=현재가, 12=등락율 등)를 문서의
  실시간 항목표와 대조. 현재가는 부호가 붙어올 수 있으므로 `abs()` 처리.
- 이탈 시 `{"trnm": "REMOVE", ...}`로 해당 종목 해제.
- **95개 제한 카운터:** 등록 종목 수를 ws.py가 추적, 95개 도달 시 신규 등록 스킵하고
  WARNING 로그. (조건검색 자체는 별도)
- 종목명·업종·전일거래량은 실시간 메시지에 없음 → 편입 시 REST `ka10001`(주식기본정보)
  1회 조회로 채운다 (`api.py`에 함수 추가). REST 호출은 **초당 1건 rate limit**을
  `asyncio.Semaphore` + sleep으로 지키기. ⚠️ 문서 확인: ka10001 응답 필드명.

### Step 5 — main.py 조립

```
QApplication + qasync.QEventLoop 셋업
→ ConditionScreen 생성, 단일 QMainWindow에 배치
→ 토큰 발급 → 웹소켓 접속/LOGIN → CNSRLST로 콤보박스 채움
→ start_btn 토글 ON: 선택 조건식 CNSRREQ (초기 리스트를 on_included로 밀어넣기)
   토글 OFF: CNSRCLR + 시세 전체 REMOVE
→ 편입 이벤트: ka10001 조회 → screen.on_included() → 시세 REG
→ 이탈 이벤트: screen.on_excluded() → 시세 REMOVE
→ 시세 이벤트: screen.on_tick()
```

- 예외로 코루틴이 죽지 않게 수신 루프 최상위에 try/except + 로그.
- `gui.py`의 `_demo` 관련 코드 삭제.

### Step 6 — 검증 체크리스트 (전부 통과해야 완료)

- [ ] `python api.py` — 토큰 발급 성공
- [ ] `python main.py` — 창 표시, 콤보박스에 실제 조건식 목록
- [ ] 등록 토글 → 초기 편입 종목이 그리드에 뜨고 현재가·등락률이 실시간으로 움직임
- [ ] 장중 신규 편입 시 행 추가 + 편입시간 표시, 이탈 시 행 제거 (이탈삭제 체크 시)
- [ ] 네트워크 강제 차단(와이파이 off/on) → 자동 재접속 + 조건검색/시세 자동 재등록
- [ ] 30분 방치 후에도 수신 지속 (PING 처리 확인)
- [ ] `bot.log`에 접속/편입/이탈/재접속 기록

**장 마감 후에는** 편입 이벤트가 없으므로: CNSRREQ 초기 리스트 표시와 PING/재접속까지만
검증하고, 실시간 편입은 장중에 확인한다고 명시할 것.

## 7. 구현 규칙

- 스레드 만들지 말 것 (qasync 하나로 끝). `time.sleep` 금지, `asyncio.sleep`만.
- 새 파일·새 의존성·추상 클래스·플러그인 구조 금지. 계획서의 5개 파일로 끝낸다.
- UI 갱신 throttle은 **만들지 말 것** — 부분 갱신이라 종목 수십 개 수준에선 충분하다.
  실측으로 버벅일 때만 추가 (그때는 on_tick 앞에 100ms 배칭 한 겹).
- 각 파일에 `__main__` 자가 검증 블록 하나씩 (api.py=토큰, ws.py=접속+CNSRLST 출력).
- 비밀키를 로그·코드·커밋에 절대 남기지 말 것.

## 8. 알려진 함정

- PING 무응답 → 서버가 연결을 끊는다. 수신 루프에서 최우선 처리.
- 토큰 만료를 웹소켓이 별도 통지하지 않을 수 있음 → 재접속 시 항상 새 토큰으로 LOGIN.
- 실시간 가격 필드는 부호 포함("-4620" = 하락 4620원) → `abs()` + 등락률로 색 판단.
- 모의투자는 일부 TR/실시간 미지원 (§5-4 리스크).
- 조건검색 편입이 폭주하면 ka10001 조회(1req/s)가 병목 → 종목명 없이 먼저 행을 띄우고
  조회 완료 후 `on_tick`으로 이름 채우는 식으로 처리 (그리드는 이미 지원함).

## 9. 백로그 (이번에 하지 않음)

1. 자동매매: 편입 → 매수, 목표수익률/손절 → 매도 (kt10000~10003)
2. **주문 체결통보 실시간(00/04) 등록** — 자동매매 전에 필수
3. 시작 시 잔고 조회로 포지션 복구
4. 다중창: `QMdiArea`에 ConditionScreen 여러 개
5. L일봉H 미니차트 컬럼 (커스텀 델리게이트), 예상체결가 컬럼
6. PyInstaller 패키징, 장 시작/종료 스케줄

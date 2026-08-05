# CLAUDE.md

이 문서는 `trading-bot`의 구조 개편 원칙과 진행 상태, 다음 작업 순서를 기록합니다.

## 프로젝트 원칙

- 사용자에게는 존대어로 응답합니다.
- 현재 정상 작동하는 기능을 유지하면서 단계적으로 구조를 분리합니다.
- 구조 개편과 기능 변경을 한 단계에서 함께 처리하지 않습니다.
- 각 단계가 끝날 때 Python 문법 검사, import 검사와 주요 화면 실행 검증을 수행합니다.
- API 키, 계좌 정보, 텔레그램 세션과 `.env`는 Git에 커밋하지 않습니다.
- 실계좌 주문 기능을 변경할 때는 주문 수량, 중복 주문 방지와 비상 정지 동작을 우선 확인합니다.
- 검증에는 `.\.venv\Scripts\python.exe`만 사용합니다.
- 사용자 `layout.ini`와 운영 DB를 시험 목적으로 수정하지 않습니다. 시험은 임시 설정 파일로 진행합니다.

## 현재 구조

```text
main.py                  앱 제어, 창 수명주기, AnalysisWindow 뼈대, 분리 시계
ui/
├─ __init__.py           뉴스 탭 공용 상수(신규 표시 색상·역할)
├─ realtime_news_tab.py  LS 실시간 뉴스 탭, 상단 전광판, 본문 상세창
├─ stock_news_tab.py     네이버 종목뉴스 탭, 종목토론실
├─ telegram_news_tab.py  텔레그램 뉴스 탭, 원문 상세창
├─ limit_up_tab.py       상한가 탭
└─ theme_tab.py          테마 탭
telegram_news.py         Telethon 사용자 API 수집기, 종목 추출
ls_news_ws.py            LS 실시간 뉴스 수신
ls_news_server_sync.py   앱 종료 중 누락 LS 뉴스 보완
analysis_db.py           분석 SQLite 스키마와 조회·저장
```

`AnalysisWindow`는 Mixin 조합으로 구성합니다.

```python
class AnalysisWindow(
    LimitUpTabMixin, RealtimeNewsTabMixin, StockNewsTabMixin,
    TelegramNewsTabMixin, ThemeTabMixin, QMainWindow):
```

탭 코드는 파일만 분리한 상태이며 `self`를 통해 `AnalysisWindow`의 속성을 공유합니다. 완전한 위젯 분리와 Qt Signal 통신은 아직 적용하지 않았습니다.

## 완료된 작업

### 1. 현재 상태 보존 — 완료

기능 단위 커밋과 실행 기준점을 확보했습니다.

### 2. 뉴스 계층 분리(`news/` 패키지) — 보류

사용자 판단으로 건너뛰었습니다. 뉴스 공급처별 클라이언트는 각각 최상위 모듈(`ls_news_ws.py`, `naver_news_api.py`, `telegram_news.py`)로 유지하고, 저장·중복 방지는 `analysis_db.py`가 담당합니다. 공급처가 더 늘어나 중복이 커지면 다시 검토합니다.

### 3·5. 분석창 탭 분리 — 완료

`main.py`가 11,182줄에서 약 5,000줄로 줄었습니다. 뉴스 3개 탭과 상한가·테마 탭을 `ui/` 아래 Mixin으로 옮겼고, 도달하지 않는 화면 코드 52개 메서드를 함께 제거했습니다. 화면 모양과 동작은 바꾸지 않았습니다.

### 4. 텔레그램 뉴스 — 완료

- `TG_API_ID`/`TG_API_HASH` 기반 Telethon 사용자 API 연결, 전화번호·2단계 인증 처리
- 세션 파일 `data/telegram.session`, Git 제외
- 채널별 마지막 메시지 ID를 `telegram_news` 표에 저장하고 실행 시 소급 수집
- `(channel, message_id)` 조합으로 중복 저장 방지
- 탭 표시: 번호, 게시 시각, 채널명, 종목명·종목코드, 제목, 본문
- 채널·종목명·종목코드·본문 검색, 관심종목만 보기, 종목 있는 글만 보기
- 신규 강조와 `신규 해제` 버튼, 종목코드 유무에 따른 알림음 구분
- 종목 셀 좌클릭은 대표 종목코드 복사, 우클릭은 관심종목 추가 후 종토방 열기
- 행 클릭 시 앱 안 상세창에서 원문 표시(저장 본문을 먼저 보여 준 뒤 임베드로 교체), 창 크기와 글자 크기 저장
- 새 글은 분석창 상단 전광판에 `채널명 · 제목` 형태로 표시하며 소급 수집분은 제외

### 6. 분석창 시계 분리 — 완료

- 타이틀바 없는 반투명 창으로 분리하고 전체 영역 드래그, 정비율 크기 조절, 휠 투명도 조절을 지원합니다.
- 분리 상태·위치·크기·투명도를 `layout.ini`에 저장하고 다음 실행과 분석창 재표시 때 복원합니다.
- 화면 밖 좌표로 저장되어도 보이는 위치로 되돌립니다.
- 분석창의 `📌 항상 위`와 분리 시계의 항상 위는 서로 영향을 주지 않습니다.

## 다음 작업 순서

1. 시장·수급 분석과 공시·예측 영역 정리 — 남은 `main.py` 분석 코드의 실제 사용 여부를 먼저 확인한 뒤 이동 대상을 정합니다.
2. 뉴스 탭을 Mixin에서 독립 위젯으로 전환 — 상단 전광판, 알림음, 관심종목 상태를 Qt Signal 공통 이벤트로 바꿉니다.
3. 주문·잔고와 키움 실시간 등록 코드 검토 — 위 단계가 끝난 뒤 별도로 진행합니다.

## 공통 뉴스 데이터 권장 형식

```python
NewsItem(
    provider: str,
    external_id: str,
    channel: str,
    published_at: datetime,
    title: str,
    body: str,
    stock_codes: tuple[str, ...],
    url: str,
)
```

- 공급처별 원본 데이터는 클라이언트에서 공통 형식으로 변환합니다.
- UI는 공급처별 API 응답 구조를 직접 알지 않도록 합니다.
- DB 중복키는 `provider + channel + external_id`를 기본으로 사용합니다.

현재는 공급처별 표(`ls_realtime_news`, `telegram_news`)를 각각 사용하며, 위 형식은 통합 시점의 기준입니다.

## LS 뉴스 종목코드 형식

LS NWS는 종목코드를 12자리 고정 폭으로 이어 붙여 보냅니다. 앞 6자리는 `000000` 채움이고 뒤 6자리가 실제 코드입니다.

```text
0000000006600000000059300000000193T0
→ 000660, 005930, 0193T0
```

- 테마·업종 코드는 글자를 포함할 수 있으므로 숫자 여부로 분리하면 안 됩니다.
- 분리는 `analysis_db.split_ls_news_stock_codes`만 사용합니다.
- 종목명 표시는 이름이 확인되는 첫 종목을 대표로 사용합니다.

## 단계별 검증

```powershell
.\.venv\Scripts\python.exe -m py_compile config.py api.py ws.py gui.py rank.py main.py analysis_db.py prediction_model.py ls_news_ws.py ls_news_server_sync.py telegram_news.py ui\realtime_news_tab.py ui\stock_news_tab.py ui\telegram_news_tab.py ui\limit_up_tab.py ui\theme_tab.py
.\.venv\Scripts\python.exe telegram_news.py
git diff --check
```

추가로 확인할 항목:

- 분석창을 숨겨도 활성화된 뉴스 알림음이 유지되는지 확인
- LS 종목코드 있음·없음 사운드가 구분되는지 확인
- 네이버 신규 뉴스 사운드와 상단 전광판 표시 확인
- 텔레그램 새 글의 전광판 표시와 알림음 구분 확인
- 앱 재시작 후 뉴스 중복 저장 및 누락 보완 확인
- 본창 재조회 체크가 오전 09:02:20에 자동 해제되는지 확인
- 추가 조건검색창의 재조회 체크에는 영향을 주지 않는지 확인
- 시계를 분리한 채 종료한 뒤 재실행과 분석창 재표시에서 복원되는지 확인

## 하지 말아야 할 작업

- 구조 개편 중 기존 주문 로직을 동시에 재작성하지 않습니다.
- 기존 DB를 삭제하거나 전체 재생성하지 않습니다.
- 사용자 `.env`, `layout.ini`, 운영 DB와 세션 파일을 덮어쓰지 않습니다.
- 리팩터링이 끝나기 전에 기존 파일을 먼저 삭제하지 않습니다.
- 검증 없이 여러 대형 클래스를 한 번에 이동하지 않습니다.

# -*- coding: utf-8 -*-
"""분석용 SQLite 저장소.

시세·상한가·공시·테마·백테스트 데이터를 한 DB에서 연결하되, 원천 데이터와
가공 결과를 분리한다. 모든 날짜는 YYYY-MM-DD, 시각은 ISO 8601 문자열로 저장한다.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "data" / "market_analysis.db"
SCHEMA_VERSION = 1
ANALYSIS_STOCK_TYPES = (
    "COMMON", "PREFERRED", "SPAC", "FOREIGN", "REIT", "INFRA",
)


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stocks (
    stock_code TEXT PRIMARY KEY,
    stock_name TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    stock_type TEXT NOT NULL DEFAULT '',
    sector_code TEXT NOT NULL DEFAULT '',
    sector_name TEXT NOT NULL DEFAULT '',
    listed_date TEXT,
    shares_outstanding INTEGER,
    dart_corp_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_prices (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    open_price INTEGER,
    high_price INTEGER,
    low_price INTEGER,
    close_price INTEGER,
    prev_close INTEGER,
    upper_price INTEGER,
    lower_price INTEGER,
    volume INTEGER,
    trading_value INTEGER,
    market_cap INTEGER,
    change_rate REAL,
    source TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS limit_up_events (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    closed_at_limit INTEGER NOT NULL DEFAULT 0,
    touched_limit INTEGER NOT NULL DEFAULT 0,
    first_hit_time TEXT,
    last_entry_time TEXT,
    break_count INTEGER,
    consecutive_days INTEGER NOT NULL DEFAULT 1,
    reason_text TEXT,
    reason_confidence REAL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code),
    FOREIGN KEY (trade_date, stock_code)
        REFERENCES daily_prices(trade_date, stock_code)
);

CREATE TABLE IF NOT EXISTS disclosures (
    receipt_no TEXT PRIMARY KEY,
    receipt_date TEXT NOT NULL,
    stock_code TEXT,
    dart_corp_code TEXT,
    report_name TEXT NOT NULL DEFAULT '',
    disclosure_type TEXT NOT NULL DEFAULT '',
    submitter TEXT NOT NULL DEFAULT '',
    correction INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disclosure_collection_ranges (
    stock_code TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, date_from, date_to),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS themes (
    theme_id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_sources (
    theme_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_code TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (theme_id, source),
    FOREIGN KEY (theme_id) REFERENCES themes(theme_id)
);

CREATE TABLE IF NOT EXISTS stock_themes (
    stock_code TEXT NOT NULL,
    theme_id INTEGER NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT,
    source TEXT NOT NULL DEFAULT '',
    confidence REAL,
    PRIMARY KEY (stock_code, theme_id, valid_from),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code),
    FOREIGN KEY (theme_id) REFERENCES themes(theme_id)
);

CREATE TABLE IF NOT EXISTS investor_flows (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    trading_value_million INTEGER,
    individual_net INTEGER,
    foreign_net INTEGER,
    institution_net INTEGER,
    financial_investment_net INTEGER,
    insurance_net INTEGER,
    investment_trust_net INTEGER,
    pension_net INTEGER,
    private_fund_net INTEGER,
    other_corporation_net INTEGER,
    source TEXT NOT NULL DEFAULT 'KIWOOM',
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS post_event_performance (
    event_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    open_return REAL,
    close_return REAL,
    max_return REAL,
    max_drawdown REAL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (event_date, stock_code, horizon_days),
    FOREIGN KEY (event_date, stock_code)
        REFERENCES limit_up_events(trade_date, stock_code)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_type TEXT NOT NULL,
    date_from TEXT,
    date_to TEXT,
    status TEXT NOT NULL,
    processed_count INTEGER NOT NULL DEFAULT 0,
    saved_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_code_date
    ON daily_prices(stock_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_limit_up_events_date
    ON limit_up_events(trade_date);
CREATE INDEX IF NOT EXISTS idx_disclosures_stock_date
    ON disclosures(stock_code, receipt_date);
CREATE INDEX IF NOT EXISTS idx_disclosure_ranges_stock_dates
    ON disclosure_collection_ranges(stock_code, date_from, date_to);
CREATE INDEX IF NOT EXISTS idx_collection_runs_type_started
    ON collection_runs(data_type, started_at);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def initialize(db_path: Path = DB_PATH) -> Path:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection:
        with connection:
            connection.executescript(SCHEMA)
            row = connection.execute(
                "SELECT version FROM schema_info ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row is None or row["version"] != SCHEMA_VERSION:
                connection.execute(
                    "INSERT INTO schema_info(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, now),
                )
    return db_path


def database_stats(db_path: Path = DB_PATH) -> dict:
    if not db_path.exists():
        return {
            "exists": False,
            "path": str(db_path),
            "size": 0,
            "stocks": 0,
            "daily_prices": 0,
            "limit_up_events": 0,
            "disclosures": 0,
            "themes": 0,
            "stock_themes": 0,
            "last_trade_date": "",
            "last_run": "",
        }
    with closing(connect(db_path)) as connection:
        result = {
            "exists": True,
            "path": str(db_path),
            "size": db_path.stat().st_size,
        }
        for table in (
            "stocks", "daily_prices", "limit_up_events", "disclosures",
            "themes", "stock_themes",
        ):
            result[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        result["last_trade_date"] = connection.execute(
            "SELECT MAX(trade_date) FROM daily_prices"
        ).fetchone()[0] or ""
        row = connection.execute(
            """SELECT data_type, status, started_at
               FROM collection_runs ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
        result["last_run"] = (
            f"{row['started_at']} / {row['data_type']} / {row['status']}"
            if row else ""
        )
        return result


def save_stock_history(
    stock: dict, bars: list[dict], date_from: str, date_to: str,
    db_path: Path = DB_PATH,
) -> tuple[int, int]:
    """종목 하나의 일봉과 상한가 이벤트를 원자적으로 저장한다."""
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    history = sorted(
        (bar for bar in bars if bar["date"] <= date_to),
        key=lambda bar: bar["date"],
    )
    selected = [bar for bar in history if bar["date"] >= date_from]
    if not selected:
        return 0, 0
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """INSERT INTO stocks(
                   stock_code, stock_name, market, stock_type, sector_name,
                   listed_date, shares_outstanding, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(stock_code) DO UPDATE SET
                   stock_name=excluded.stock_name, market=excluded.market,
                   stock_type=excluded.stock_type,
                   sector_name=excluded.sector_name,
                   listed_date=excluded.listed_date,
                   shares_outstanding=excluded.shares_outstanding,
                   updated_at=excluded.updated_at""",
            (stock["code"], stock.get("name", ""), stock.get("market", ""),
             stock.get("stock_type", ""), stock.get("sector_name", ""),
             stock.get("listed_date") or None, stock.get("shares") or None, now),
        )
        price_rows = []
        event_rows = []
        streak = 0
        previous = None
        previous_by_date = {
            bar["date"]: history[index - 1] if index else None
            for index, bar in enumerate(history)
        }
        for bar in selected:
            close = int(bar.get("close") or 0)
            previous = previous_by_date[bar["date"]]
            prev_close = int(previous["close"]) if previous else 0
            rate = ((close - prev_close) / prev_close * 100) if prev_close else None
            is_limit = rate is not None and rate >= 29.5
            streak = streak + 1 if is_limit else 0
            price_rows.append((
                bar["date"], stock["code"], bar.get("open"), bar.get("high"),
                bar.get("low"), close, prev_close or None, bar.get("volume"),
                bar.get("trading_value"), rate, "KIWOOM", now,
            ))
            if is_limit:
                event_rows.append(
                    (bar["date"], stock["code"], 1, 1, streak, now))
        connection.executemany(
            """INSERT INTO daily_prices(
                   trade_date, stock_code, open_price, high_price, low_price,
                   close_price, prev_close, volume, trading_value, change_rate,
                   source, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                   open_price=excluded.open_price, high_price=excluded.high_price,
                   low_price=excluded.low_price, close_price=excluded.close_price,
                   prev_close=excluded.prev_close, volume=excluded.volume,
                   trading_value=excluded.trading_value,
                   change_rate=excluded.change_rate,
                   source=excluded.source, collected_at=excluded.collected_at""",
            price_rows,
        )
        connection.executemany(
            """INSERT INTO limit_up_events(
                   trade_date, stock_code, closed_at_limit, touched_limit,
                   consecutive_days, collected_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                   closed_at_limit=excluded.closed_at_limit,
                   touched_limit=excluded.touched_limit,
                   consecutive_days=excluded.consecutive_days,
                   collected_at=excluded.collected_at""",
            event_rows,
        )
    return len(price_rows), len(event_rows)


def save_krx_market_day(rows: list[dict], db_path: Path = DB_PATH
                        ) -> tuple[int, int]:
    """KRX 하루 전체시장 응답을 일봉 및 상한가 이벤트로 저장한다."""
    if not rows:
        return 0, 0

    def number(value, decimal=False):
        text = str(value or "0").replace(",", "").strip()
        try:
            return float(text) if decimal else int(float(text))
        except ValueError:
            return 0.0 if decimal else 0

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    stock_rows, price_rows, event_rows = [], [], []
    codes_by_date: dict[str, list[str]] = {}
    limit_codes_by_date: dict[str, set[str]] = {}
    for row in rows:
        trade_date = str(row.get("BAS_DD", "")).strip()
        code = str(row.get("ISU_SRT_CD") or row.get("ISU_CD") or "").strip()
        name = str(row.get("ISU_ABBRV") or row.get("ISU_NM") or "").strip()
        if not trade_date or not code:
            continue
        rate = number(row.get("FLUC_RT"), decimal=True)
        close = number(row.get("TDD_CLSPRC"))
        change = number(row.get("CMPPREVDD_PRC"))
        stock_type = "PREFERRED" if (
            name.endswith("우") or name.endswith("우B")
            or "우선주" in str(row.get("SECT_TP_NM", ""))
        ) else "COMMON"
        stock_rows.append((
            code, name, row.get("_market", ""), stock_type,
            str(row.get("SECT_TP_NM", "")), number(row.get("LIST_SHRS")), now,
        ))
        price_rows.append((
            trade_date, code, number(row.get("TDD_OPNPRC")),
            number(row.get("TDD_HGPRC")), number(row.get("TDD_LWPRC")),
            close, close - change if close else None,
            number(row.get("ACC_TRDVOL")), number(row.get("ACC_TRDVAL")),
            number(row.get("MKTCAP")), rate, "KRX", now,
        ))
        codes_by_date.setdefault(trade_date, []).append(code)
        if rate >= 29.5:
            event_rows.append((trade_date, code, 1, 1, 1, now))
            limit_codes_by_date.setdefault(trade_date, set()).add(code)

    with closing(connect(db_path)) as connection, connection:
        connection.executemany(
            """INSERT INTO stocks(
                   stock_code, stock_name, market, stock_type, sector_name,
                   shares_outstanding, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(stock_code) DO UPDATE SET
                   stock_name=excluded.stock_name, market=excluded.market,
                   sector_name=excluded.sector_name,
                   shares_outstanding=excluded.shares_outstanding,
                   updated_at=excluded.updated_at""",
            stock_rows,
        )
        connection.executemany(
            """INSERT INTO daily_prices(
                   trade_date, stock_code, open_price, high_price, low_price,
                   close_price, prev_close, volume, trading_value, market_cap,
                   change_rate, source, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                   open_price=excluded.open_price, high_price=excluded.high_price,
                   low_price=excluded.low_price, close_price=excluded.close_price,
                   prev_close=excluded.prev_close, volume=excluded.volume,
                   trading_value=excluded.trading_value,
                   market_cap=excluded.market_cap,
                   change_rate=excluded.change_rate,
                   source=excluded.source, collected_at=excluded.collected_at""",
            price_rows,
        )
        for trade_date, codes in codes_by_date.items():
            non_limit_codes = [
                code for code in codes
                if code not in limit_codes_by_date.get(trade_date, set())
            ]
            if non_limit_codes:
                placeholders = ",".join("?" for _ in non_limit_codes)
                connection.execute(
                    f"""DELETE FROM limit_up_events
                        WHERE trade_date=? AND stock_code IN ({placeholders})""",
                    (trade_date, *non_limit_codes),
                )
        connection.executemany(
            """INSERT INTO limit_up_events(
                   trade_date, stock_code, closed_at_limit, touched_limit,
                   consecutive_days, collected_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                   closed_at_limit=excluded.closed_at_limit,
                   touched_limit=excluded.touched_limit,
                   collected_at=excluded.collected_at""",
            event_rows,
        )
    return len(price_rows), len(event_rows)


def krx_collected_dates(date_from: str, date_to: str,
                        db_path: Path = DB_PATH) -> set[str]:
    if not db_path.exists():
        return set()
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT DISTINCT trade_date FROM daily_prices
               WHERE source='KRX' AND trade_date BETWEEN ? AND ?""",
            (date_from, date_to),
        ).fetchall()
        return {row["trade_date"] for row in rows}


def sync_stock_catalog(stocks: list[dict], db_path: Path = DB_PATH) -> int:
    """최신 종목 목록의 유형/시장 정보를 기존 원천 데이터에 덧붙인다."""
    if not stocks or not db_path.exists():
        return 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = [
        (
            stock.get("name", ""), stock.get("market", ""),
            stock.get("stock_type", ""), stock.get("sector_name", ""),
            stock.get("listed_date") or None, stock.get("shares") or None,
            now, stock["code"],
        )
        for stock in stocks if stock.get("code")
    ]
    with closing(connect(db_path)) as connection, connection:
        connection.executemany(
            """UPDATE stocks SET stock_name=?, market=?, stock_type=?,
                      sector_name=?, listed_date=?, shares_outstanding=?,
                      updated_at=?
               WHERE stock_code=?""",
            rows,
        )
    return len(rows)


def start_collection(data_type: str, date_from: str, date_to: str,
                     db_path: Path = DB_PATH) -> int:
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """UPDATE collection_runs
               SET status='INTERRUPTED',
                   message=CASE WHEN message='' THEN '이전 실행 중 앱 종료'
                                ELSE message END,
                   finished_at=?
               WHERE status='RUNNING'""",
            (now,),
        )
        cursor = connection.execute(
            """INSERT INTO collection_runs(
                   data_type, date_from, date_to, status, started_at)
               VALUES (?, ?, ?, 'RUNNING', ?)""",
            (data_type, date_from, date_to, now),
        )
        return cursor.lastrowid


def update_collection(run_id: int, status: str, processed: int, saved: int,
                      errors: int, message: str = "", db_path: Path = DB_PATH):
    finished = (
        datetime.now().astimezone().isoformat(timespec="seconds")
        if status != "RUNNING" else None
    )
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """UPDATE collection_runs SET status=?, processed_count=?,
                   saved_count=?, error_count=?, message=?, finished_at=?
               WHERE run_id=?""",
            (status, processed, saved, errors, message, finished, run_id),
        )


def limit_up_rows(date_from: str, date_to: str, stock_query: str = "",
                  theme_query: str = "",
                  db_path: Path = DB_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """WITH theme_labels AS (
               SELECT st.stock_code,
                          GROUP_CONCAT(DISTINCT CASE
                              WHEN st.source='NAVER' THEN t.theme_name END
                          ) AS naver_themes,
                          GROUP_CONCAT(DISTINCT CASE
                              WHEN st.source='KIWOOM' THEN t.theme_name END
                          ) AS kiwoom_themes,
                          GROUP_CONCAT(DISTINCT CASE
                              WHEN st.source='WICS' THEN t.theme_name END
                          ) AS wics_themes,
                          GROUP_CONCAT(DISTINCT CASE
                              WHEN st.source='KRX' THEN t.theme_name END
                          ) AS krx_themes,
                          GROUP_CONCAT(DISTINCT CASE
                              WHEN st.source='DART' THEN t.theme_name END
                          ) AS dart_themes
                     FROM stock_themes st
                     JOIN themes t ON t.theme_id=st.theme_id
                    WHERE st.valid_to IS NULL
                    GROUP BY st.stock_code
               )
               SELECT e.trade_date, e.stock_code, s.stock_name, s.market,
                      e.last_entry_time,
                      p.close_price, p.change_rate, p.volume, p.trading_value,
                      e.consecutive_days,
                      COUNT(DISTINCT d.receipt_no) AS disclosure_count,
                      COALESCE(tl.naver_themes, tl.kiwoom_themes, tl.wics_themes,
                               tl.krx_themes, tl.dart_themes, '') AS theme_names
               FROM limit_up_events e
               JOIN stocks s ON s.stock_code=e.stock_code
               JOIN daily_prices p ON p.trade_date=e.trade_date
                                  AND p.stock_code=e.stock_code
               LEFT JOIN theme_labels tl ON tl.stock_code=e.stock_code
               LEFT JOIN disclosures d ON d.stock_code=e.stock_code
                    AND d.receipt_date BETWEEN ? AND ?
               WHERE e.trade_date BETWEEN ? AND ?
                 AND s.stock_type IN (?, ?, ?, ?, ?, ?)
                 AND (?='' OR s.stock_code LIKE ? OR s.stock_name LIKE ?)
                 AND (?='' OR COALESCE(
                       tl.naver_themes, tl.kiwoom_themes, tl.wics_themes,
                       tl.krx_themes, tl.dart_themes, '') LIKE ?)
               GROUP BY e.trade_date, e.stock_code
               ORDER BY e.trade_date DESC, p.trading_value DESC""",
            (
                date_from, date_to, date_from, date_to, *ANALYSIS_STOCK_TYPES,
                stock_query, f"%{stock_query}%", f"%{stock_query}%",
                theme_query, f"%{theme_query}%",
            ),
        ).fetchall()
        return [dict(row) for row in rows]


def theme_summary_rows(query: str = "", db_path: Path = DB_PATH) -> list[dict]:
    """현재 유효한 테마별 구성 종목과 상한가 이력 종목 수를 반환한다."""
    if not db_path.exists():
        return []
    query = str(query or "").strip()
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT st.source, t.theme_name, COALESCE(ts.source_code, '') AS source_code,
                      COUNT(DISTINCT st.stock_code) AS member_count,
                      COUNT(DISTINCT CASE WHEN e.stock_code IS NOT NULL
                                         THEN st.stock_code END) AS limit_up_count,
                      GROUP_CONCAT(DISTINCT
                          st.stock_code || ' ' || COALESCE(s.stock_name, '')
                      ) AS members
                 FROM stock_themes st
                 JOIN themes t ON t.theme_id=st.theme_id
                 LEFT JOIN theme_sources ts
                   ON ts.theme_id=st.theme_id AND ts.source=st.source
                 LEFT JOIN stocks s ON s.stock_code=st.stock_code
                LEFT JOIN limit_up_events e ON e.stock_code=st.stock_code
                WHERE st.valid_to IS NULL
                  AND (?='' OR t.theme_name LIKE ? OR EXISTS (
                      SELECT 1
                        FROM stock_themes sx
                        LEFT JOIN stocks ss ON ss.stock_code=sx.stock_code
                       WHERE sx.theme_id=st.theme_id
                         AND sx.source=st.source
                         AND sx.valid_to IS NULL
                         AND (sx.stock_code LIKE ? OR ss.stock_name LIKE ?)))
                GROUP BY st.source, t.theme_id, t.theme_name
                ORDER BY CASE st.source
                           WHEN 'NAVER' THEN 1 WHEN 'KIWOOM' THEN 2
                           WHEN 'WICS' THEN 3 WHEN 'KRX' THEN 4
                           WHEN 'DART' THEN 5 ELSE 9 END,
                         limit_up_count DESC, member_count DESC, t.theme_name""",
            (query, f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
        return [dict(row) for row in rows]


def market_dashboard(db_path: Path = DB_PATH) -> dict:
    """최근 거래일 기준 시장 요약·테마·주도주·상한가·수급을 반환한다."""
    empty = {
        "trade_date": "", "markets": [], "themes": [],
        "leaders": [], "limit_ups": [], "flows": [],
    }
    if not db_path.exists():
        return empty
    with closing(connect(db_path)) as connection:
        trade_date = connection.execute(
            "SELECT MAX(trade_date) FROM daily_prices"
        ).fetchone()[0] or ""
        if not trade_date:
            return empty
        markets = connection.execute(
            """SELECT s.market, COUNT(*) AS stock_count,
                      SUM(CASE WHEN p.change_rate > 0 THEN 1 ELSE 0 END) AS rising,
                      SUM(CASE WHEN p.change_rate < 0 THEN 1 ELSE 0 END) AS falling,
                      SUM(CASE WHEN p.change_rate = 0 THEN 1 ELSE 0 END) AS unchanged,
                      SUM(COALESCE(p.trading_value, 0)) AS trading_value,
                      COUNT(e.stock_code) AS limit_up_count
                 FROM daily_prices p
                 JOIN stocks s ON s.stock_code=p.stock_code
                 LEFT JOIN limit_up_events e
                   ON e.trade_date=p.trade_date AND e.stock_code=p.stock_code
                WHERE p.trade_date=?
                  AND s.stock_type IN (?, ?, ?, ?, ?, ?)
                  AND s.market IN ('KOSPI', 'KOSDAQ')
                GROUP BY s.market ORDER BY s.market""",
            (trade_date, *ANALYSIS_STOCK_TYPES),
        ).fetchall()
        themes = connection.execute(
            """SELECT t.theme_name,
                      COUNT(DISTINCT st.stock_code) AS member_count,
                      ROUND(AVG(p.change_rate), 2) AS average_rate,
                      SUM(COALESCE(p.trading_value, 0)) AS trading_value,
                      COUNT(DISTINCT e.stock_code) AS limit_up_count
                 FROM stock_themes st
                 JOIN themes t ON t.theme_id=st.theme_id
                 JOIN daily_prices p
                   ON p.stock_code=st.stock_code AND p.trade_date=?
                 LEFT JOIN limit_up_events e
                   ON e.stock_code=st.stock_code AND e.trade_date=p.trade_date
                WHERE st.source='NAVER' AND st.valid_to IS NULL
                GROUP BY st.theme_id, t.theme_name
                HAVING COUNT(DISTINCT st.stock_code) >= 2
                ORDER BY average_rate DESC, trading_value DESC LIMIT 15""",
            (trade_date,),
        ).fetchall()
        leaders = connection.execute(
            """WITH naver_labels AS (
                   SELECT st.stock_code,
                          GROUP_CONCAT(DISTINCT t.theme_name) AS themes
                     FROM stock_themes st
                     JOIN themes t ON t.theme_id=st.theme_id
                    WHERE st.source='NAVER' AND st.valid_to IS NULL
                    GROUP BY st.stock_code
               )
               SELECT p.stock_code, s.stock_name, s.market, p.change_rate,
                      p.trading_value, COALESCE(n.themes, '') AS themes
                 FROM daily_prices p
                 JOIN stocks s ON s.stock_code=p.stock_code
                 LEFT JOIN naver_labels n ON n.stock_code=p.stock_code
                WHERE p.trade_date=?
                  AND s.stock_type IN (?, ?, ?, ?, ?, ?)
                ORDER BY p.trading_value DESC LIMIT 20""",
            (trade_date, *ANALYSIS_STOCK_TYPES),
        ).fetchall()
        limit_ups = connection.execute(
            """SELECT e.stock_code, s.stock_name, e.last_entry_time,
                      p.trading_value
                 FROM limit_up_events e
                 JOIN stocks s ON s.stock_code=e.stock_code
                 JOIN daily_prices p
                   ON p.trade_date=e.trade_date AND p.stock_code=e.stock_code
                WHERE e.trade_date=?
                ORDER BY COALESCE(e.last_entry_time, '99:99:99'),
                         p.trading_value DESC LIMIT 20""",
            (trade_date,),
        ).fetchall()
        flow_date = connection.execute(
            "SELECT MAX(trade_date) FROM investor_flows WHERE trade_date<=?",
            (trade_date,),
        ).fetchone()[0]
        flows = []
        if flow_date:
            flows = connection.execute(
                """SELECT f.stock_code, s.stock_name,
                          COALESCE(f.foreign_net, 0)
                            + COALESCE(f.institution_net, 0) AS net
                     FROM investor_flows f
                     JOIN stocks s ON s.stock_code=f.stock_code
                    WHERE f.trade_date=?
                    ORDER BY net DESC LIMIT 15""",
                (flow_date,),
            ).fetchall()
        return {
            "trade_date": trade_date,
            "flow_date": flow_date or "",
            "markets": [dict(row) for row in markets],
            "themes": [dict(row) for row in themes],
            "leaders": [dict(row) for row in leaders],
            "limit_ups": [dict(row) for row in limit_ups],
            "flows": [dict(row) for row in flows],
        }


def limit_up_backtest_rows(date_from: str, date_to: str,
                           db_path: Path = DB_PATH) -> list[dict]:
    """상한가 다음 거래일 시가 진입 후 기간별 종가 성과를 계산한다."""
    if not db_path.exists():
        return []
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """WITH future_prices AS (
                   SELECT e.trade_date AS event_date, e.stock_code,
                          s.stock_name, s.market, e.last_entry_time,
                          p.trade_date, p.open_price, p.high_price,
                          p.low_price, p.close_price,
                          ROW_NUMBER() OVER (
                              PARTITION BY e.trade_date, e.stock_code
                              ORDER BY p.trade_date
                          ) AS horizon
                     FROM limit_up_events e
                     JOIN stocks s ON s.stock_code=e.stock_code
                     JOIN daily_prices p
                       ON p.stock_code=e.stock_code
                      AND p.trade_date>e.trade_date
                    WHERE e.trade_date BETWEEN ? AND ?
                      AND s.stock_type IN (?, ?, ?, ?, ?, ?)
               ),
               aggregated AS (
                   SELECT event_date, stock_code, stock_name, market,
                          last_entry_time,
                          MAX(CASE WHEN horizon=1 THEN trade_date END) AS entry_date,
                          MAX(CASE WHEN horizon=1 THEN open_price END) AS entry_price,
                          MAX(CASE WHEN horizon=1 THEN close_price END) AS close_1,
                          MAX(CASE WHEN horizon=3 THEN close_price END) AS close_3,
                          MAX(CASE WHEN horizon=5 THEN close_price END) AS close_5,
                          MAX(CASE WHEN horizon=10 THEN close_price END) AS close_10,
                          MAX(CASE WHEN horizon=20 THEN close_price END) AS close_20,
                          MAX(CASE WHEN horizon<=20 THEN high_price END) AS max_high_20,
                          MIN(CASE WHEN horizon<=20 THEN low_price END) AS min_low_20
                     FROM future_prices
                    WHERE horizon<=20
                    GROUP BY event_date, stock_code
               )
               SELECT *,
                      ROUND((close_1-entry_price)*100.0/entry_price, 2) AS return_1,
                      ROUND((close_3-entry_price)*100.0/entry_price, 2) AS return_3,
                      ROUND((close_5-entry_price)*100.0/entry_price, 2) AS return_5,
                      ROUND((close_10-entry_price)*100.0/entry_price, 2) AS return_10,
                      ROUND((close_20-entry_price)*100.0/entry_price, 2) AS return_20,
                      ROUND((max_high_20-entry_price)*100.0/entry_price, 2)
                          AS max_return_20,
                      ROUND((min_low_20-entry_price)*100.0/entry_price, 2)
                          AS max_drawdown_20
                 FROM aggregated
                WHERE entry_price IS NOT NULL AND entry_price<>0
                ORDER BY event_date DESC, stock_code""",
            (date_from, date_to, *ANALYSIS_STOCK_TYPES),
        ).fetchall()
        return [dict(row) for row in rows]


def save_investor_flows(stock_code: str, rows: list[dict],
                        db_path: Path = DB_PATH) -> int:
    """키움 투자자별 순매수 응답을 일별로 중복 없이 저장한다."""
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    def number(value) -> int:
        try:
            return int(str(value or "0").replace(",", "").replace("+", ""))
        except ValueError:
            return 0

    values = [
        (
            str(row.get("dt") or ""), stock_code,
            number(row.get("acc_trde_prica")),
            number(row.get("ind_invsr")), number(row.get("frgnr_invsr")),
            number(row.get("orgn")), number(row.get("fnnc_invt")),
            number(row.get("insrnc")), number(row.get("invtrt")),
            number(row.get("penfnd_etc")), number(row.get("samo_fund")),
            number(row.get("etc_corp")), "KIWOOM", now,
        )
        for row in rows if row.get("dt")
    ]
    with closing(connect(db_path)) as connection, connection:
        connection.executemany(
            """INSERT INTO investor_flows(
                   trade_date, stock_code, trading_value_million,
                   individual_net, foreign_net, institution_net,
                   financial_investment_net, insurance_net,
                   investment_trust_net, pension_net, private_fund_net,
                   other_corporation_net, source, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                   trading_value_million=excluded.trading_value_million,
                   individual_net=excluded.individual_net,
                   foreign_net=excluded.foreign_net,
                   institution_net=excluded.institution_net,
                   financial_investment_net=excluded.financial_investment_net,
                   insurance_net=excluded.insurance_net,
                   investment_trust_net=excluded.investment_trust_net,
                   pension_net=excluded.pension_net,
                   private_fund_net=excluded.private_fund_net,
                   other_corporation_net=excluded.other_corporation_net,
                   source=excluded.source, collected_at=excluded.collected_at""",
            values,
        )
    return len(values)


def investor_flow_rows(date_from: str, date_to: str, query: str = "",
                       view_mode: str = "date",
                       limit: int = 2000,
                       db_path: Path = DB_PATH) -> tuple[list[dict], int]:
    if not db_path.exists():
        return [], 0
    query = str(query or "").strip()
    with closing(connect(db_path)) as connection:
        params = (date_from, date_to, query, f"%{query}%", f"%{query}%")
        total = connection.execute(
            """SELECT COUNT(*)
                 FROM investor_flows f
                 JOIN stocks s ON s.stock_code=f.stock_code
                WHERE f.trade_date BETWEEN ? AND ?
                  AND (?='' OR f.stock_code LIKE ? OR s.stock_name LIKE ?)""",
            params,
        ).fetchone()[0]
        order_by = (
            "c.stock_code, c.trade_date DESC"
            if view_mode == "stock"
            else "c.trade_date DESC, c.foreign_inst_net DESC"
        )
        rows = connection.execute(
            f"""WITH base AS (
                   SELECT f.*,
                          COALESCE(f.foreign_net, 0)
                            + COALESCE(f.institution_net, 0)
                              AS foreign_inst_net,
                          SUM(COALESCE(f.foreign_net, 0)
                              + COALESCE(f.institution_net, 0))
                            OVER (PARTITION BY f.stock_code
                                  ORDER BY f.trade_date
                                  ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
                              AS foreign_inst_5d,
                          SUM(COALESCE(f.foreign_net, 0)
                              + COALESCE(f.institution_net, 0))
                            OVER (PARTITION BY f.stock_code
                                  ORDER BY f.trade_date
                                  ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
                              AS foreign_inst_20d,
                          SUM(CASE WHEN COALESCE(f.foreign_net, 0)
                                            + COALESCE(f.institution_net, 0) <= 0
                                   THEN 1 ELSE 0 END)
                            OVER (PARTITION BY f.stock_code
                                  ORDER BY f.trade_date) AS break_group
                     FROM investor_flows f
               ),
               calculated AS (
                   SELECT base.*,
                          CASE WHEN foreign_inst_net > 0 THEN
                               SUM(CASE WHEN foreign_inst_net > 0
                                        THEN 1 ELSE 0 END)
                               OVER (PARTITION BY stock_code, break_group
                                     ORDER BY trade_date)
                               ELSE 0 END AS consecutive_buy_days
                     FROM base
               )
               SELECT c.trade_date, c.stock_code, s.stock_name, s.market,
                      CASE WHEN e.stock_code IS NOT NULL THEN 1 ELSE 0 END
                           AS is_limit_up,
                      c.individual_net, c.foreign_net, c.institution_net,
                      c.foreign_inst_net, c.foreign_inst_5d,
                      c.foreign_inst_20d, c.consecutive_buy_days,
                      c.trading_value_million,
                      CASE WHEN c.trading_value_million <> 0 THEN
                           ROUND(c.foreign_inst_net * 100.0
                                 / c.trading_value_million, 2)
                           ELSE 0 END AS foreign_inst_ratio,
                      c.financial_investment_net, c.investment_trust_net,
                      c.pension_net
                 FROM calculated c
                 JOIN stocks s ON s.stock_code=c.stock_code
                 LEFT JOIN limit_up_events e
                   ON e.trade_date=c.trade_date AND e.stock_code=c.stock_code
                WHERE c.trade_date BETWEEN ? AND ?
                  AND (?='' OR c.stock_code LIKE ? OR s.stock_name LIKE ?)
                ORDER BY {order_by}
                LIMIT ?""",
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows], int(total)


def pending_investor_flow_stocks(date_from: str, date_to: str,
                                 top_n: int = 100,
                                 db_path: Path = DB_PATH) -> list[dict]:
    """시장별 거래대금 상위 N종목 중 수급이 빠진 종목을 반환한다."""
    if not db_path.exists():
        return []
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """WITH ranked AS (
                   SELECT p.trade_date, p.stock_code, s.stock_name, s.market,
                          ROW_NUMBER() OVER (
                              PARTITION BY p.trade_date, s.market
                              ORDER BY p.trading_value DESC
                          ) AS market_rank
                     FROM daily_prices p
                     JOIN stocks s ON s.stock_code=p.stock_code
                    WHERE p.trade_date BETWEEN ? AND ?
                      AND s.stock_type IN (?, ?, ?, ?, ?, ?)
                      AND s.market IN ('KOSPI', 'KOSDAQ')
                      AND COALESCE(p.trading_value, 0) > 0
               ),
               targets AS (
                   SELECT trade_date, stock_code, stock_name
                     FROM ranked WHERE market_rank <= ?
                   UNION
                   SELECT e.trade_date, e.stock_code, s.stock_name
                     FROM limit_up_events e
                     JOIN stocks s ON s.stock_code=e.stock_code
                    WHERE e.trade_date BETWEEN ? AND ?
               )
               SELECT stock_code, MAX(stock_name) AS stock_name
                 FROM targets r
                WHERE 1=1
                  AND NOT EXISTS (
                      SELECT 1 FROM investor_flows f
                       WHERE f.trade_date=r.trade_date
                         AND f.stock_code=r.stock_code)
                GROUP BY stock_code ORDER BY stock_code""",
            (
                date_from, date_to, *ANALYSIS_STOCK_TYPES, int(top_n),
                date_from, date_to,
            ),
        ).fetchall()
        return [dict(row) for row in rows]


def save_theme_snapshot(theme_rows: list[dict], snapshot_date: str,
                        source: str = "KIWOOM",
                        confidence: float = 0.8,
                        db_path: Path = DB_PATH) -> tuple[int, int]:
    """현재 테마 구성 스냅샷을 변경 구간 형태로 저장한다."""
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    previous_date = (
        datetime.strptime(snapshot_date, "%Y%m%d") - timedelta(days=1)
    ).strftime("%Y%m%d")
    source = source.upper()
    with closing(connect(db_path)) as connection, connection:
        stock_codes = {
            row[0] for row in connection.execute(
                "SELECT stock_code FROM stocks").fetchall()
        }
        theme_ids: dict[str, int] = {}
        for theme in theme_rows:
            name = str(theme.get("name") or "").strip()
            if not name:
                continue
            connection.execute(
                """INSERT INTO themes(theme_name, description, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(theme_name) DO UPDATE SET updated_at=excluded.updated_at""",
                (name, str(theme.get("code") or ""), now),
            )
            theme_ids[name] = connection.execute(
                "SELECT theme_id FROM themes WHERE theme_name=?", (name,)
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO theme_sources(
                       theme_id, source, source_code, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(theme_id, source) DO UPDATE SET
                       source_code=excluded.source_code,
                       updated_at=excluded.updated_at""",
                (
                    theme_ids[name], source,
                    str(theme.get("code") or "").strip(), now,
                ),
            )

        target = {
            (str(code).removesuffix("_AL"), theme_ids[name])
            for theme in theme_rows
            for name in [str(theme.get("name") or "").strip()]
            for code in theme.get("members", [])
            if name in theme_ids
            and str(code).removesuffix("_AL") in stock_codes
        }
        active = {
            (row["stock_code"], row["theme_id"])
            for row in connection.execute(
                """SELECT stock_code, theme_id FROM stock_themes
                   WHERE source=? AND valid_to IS NULL""",
                (source,),
            ).fetchall()
        }
        removed = active - target
        added = target - active
        connection.executemany(
            """UPDATE stock_themes SET valid_to=?
               WHERE stock_code=? AND theme_id=? AND source=?
                 AND valid_to IS NULL""",
            ((previous_date, code, theme_id, source)
             for code, theme_id in removed),
        )
        connection.executemany(
            """INSERT INTO stock_themes(
                   stock_code, theme_id, valid_from, valid_to, source, confidence)
               VALUES (?, ?, ?, NULL, ?, ?)
               ON CONFLICT(stock_code, theme_id, valid_from) DO UPDATE SET
                   valid_to=NULL, source=excluded.source,
                   confidence=excluded.confidence""",
            ((code, theme_id, snapshot_date, source, confidence)
             for code, theme_id in added),
        )
        return len(theme_ids), len(target)


def save_source_classifications(mapping: dict[str, str], snapshot_date: str,
                                source: str, confidence: float,
                                db_path: Path = DB_PATH) -> tuple[int, int]:
    """종목별 단일 분류를 기존 테마 이력 구조에 저장한다."""
    grouped: dict[str, list[str]] = {}
    for code, label in mapping.items():
        label = str(label or "").strip()
        if label:
            grouped.setdefault(label, []).append(code)
    rows = [
        {"code": "", "name": label, "members": codes}
        for label, codes in grouped.items()
    ]
    return save_theme_snapshot(
        rows, snapshot_date, source, confidence, db_path)


def limit_up_codes_without_sources(
    sources: tuple[str, ...], db_path: Path = DB_PATH
) -> list[str]:
    """상한가 이력 종목 중 지정 출처의 현재 분류가 없는 종목코드."""
    placeholders = ",".join("?" for _ in sources)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            f"""SELECT DISTINCT e.stock_code
                  FROM limit_up_events e
                  JOIN stocks s ON s.stock_code=e.stock_code
                 WHERE s.stock_type IN (?, ?, ?, ?, ?, ?)
                   AND NOT EXISTS (
                       SELECT 1 FROM stock_themes st
                        WHERE st.stock_code=e.stock_code
                          AND st.source IN ({placeholders})
                          AND st.valid_to IS NULL)
                 ORDER BY e.stock_code""",
            (*ANALYSIS_STOCK_TYPES, *sources),
        ).fetchall()
        return [row[0] for row in rows]


def dart_inferred_classifications(
    stock_codes: list[str], db_path: Path = DB_PATH
) -> dict[str, str]:
    """저장된 DART 공시 제목에서 마지막 보완용 사건 분류를 추정한다."""
    if not stock_codes:
        return {}
    keyword_labels = (
        ("단일판매", "공급계약"), ("공급계약", "공급계약"),
        ("임상시험", "임상·바이오"), ("품목허가", "임상·바이오"),
        ("특허권", "특허"), ("합병", "인수합병"),
        ("타법인주식", "지분투자"), ("유상증자", "자금조달"),
    )
    placeholders = ",".join("?" for _ in stock_codes)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            f"""SELECT stock_code, report_name
                  FROM disclosures
                 WHERE stock_code IN ({placeholders})
                 ORDER BY receipt_date DESC""",
            stock_codes,
        ).fetchall()
    result: dict[str, str] = {}
    for row in rows:
        if row["stock_code"] in result:
            continue
        for keyword, label in keyword_labels:
            if keyword in row["report_name"]:
                result[row["stock_code"]] = label
                break
    return result


def limit_up_stocks(date_from: str, date_to: str,
                    db_path: Path = DB_PATH) -> list[dict]:
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT DISTINCT s.stock_code, s.stock_name, s.dart_corp_code
               FROM limit_up_events e
               JOIN stocks s ON s.stock_code=e.stock_code
               WHERE e.trade_date BETWEEN ? AND ?
                 AND s.stock_type IN (?, ?, ?, ?, ?, ?)
               ORDER BY s.stock_code""",
            (date_from, date_to, *ANALYSIS_STOCK_TYPES),
        ).fetchall()
        return [dict(row) for row in rows]


def pending_intraday_events(date_from: str, date_to: str,
                            db_path: Path = DB_PATH
                            ) -> tuple[str, list[dict]]:
    """선택 범위의 가장 최근 상한가 거래일 중 진입시각 미수집 건."""
    with closing(connect(db_path)) as connection:
        latest = connection.execute(
            """SELECT MAX(e.trade_date)
               FROM limit_up_events e
               JOIN stocks s ON s.stock_code=e.stock_code
               WHERE e.trade_date BETWEEN ? AND ?
                 AND s.stock_type IN (?, ?, ?, ?, ?, ?)""",
            (date_from, date_to, *ANALYSIS_STOCK_TYPES),
        ).fetchone()[0]
        if not latest:
            return "", []
        rows = connection.execute(
            """SELECT e.trade_date, e.stock_code, s.stock_name,
                      p.close_price AS upper_price
               FROM limit_up_events e
               JOIN stocks s ON s.stock_code=e.stock_code
               JOIN daily_prices p ON p.trade_date=e.trade_date
                                  AND p.stock_code=e.stock_code
               WHERE e.trade_date=? AND e.last_entry_time IS NULL
                 AND s.stock_type IN (?, ?, ?, ?, ?, ?)
               ORDER BY e.stock_code""",
            (latest, *ANALYSIS_STOCK_TYPES),
        ).fetchall()
        return latest, [dict(row) for row in rows]


def save_last_entry_time(trade_date: str, stock_code: str, entry_time: str,
                         db_path: Path = DB_PATH) -> bool:
    if not entry_time:
        return False
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.execute(
            """UPDATE limit_up_events SET last_entry_time=?
               WHERE trade_date=? AND stock_code=?
                 AND last_entry_time IS NULL""",
            (entry_time, trade_date, stock_code),
        )
        return cursor.rowcount > 0


def pending_disclosure_stocks(date_from: str, date_to: str,
                              db_path: Path = DB_PATH
                              ) -> tuple[list[dict], int]:
    """이미 조회한 기간을 제외한 DART 수집 대상 종목을 반환한다.

    수집 이력 테이블 도입 전에 저장된 공시가 해당 기간에 하나라도 있는 종목은
    기존 1회 수집을 완료한 것으로 간주하고 현재 조회 범위를 이력에 등록한다.
    """
    stocks = limit_up_stocks(date_from, date_to, db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    pending = []
    skipped = 0
    with closing(connect(db_path)) as connection, connection:
        for stock in stocks:
            stock_code = stock["stock_code"]
            covered = connection.execute(
                """SELECT 1 FROM disclosure_collection_ranges
                   WHERE stock_code=? AND date_from<=? AND date_to>=?
                   LIMIT 1""",
                (stock_code, date_from, date_to),
            ).fetchone()
            if not covered:
                legacy = connection.execute(
                    """SELECT 1 FROM disclosures
                       WHERE stock_code=? AND receipt_date BETWEEN ? AND ?
                       LIMIT 1""",
                    (stock_code, date_from, date_to),
                ).fetchone()
                if legacy:
                    connection.execute(
                        """INSERT OR IGNORE INTO disclosure_collection_ranges(
                               stock_code, date_from, date_to, collected_at)
                           VALUES (?, ?, ?, ?)""",
                        (stock_code, date_from, date_to, now),
                    )
                    covered = True
            if covered:
                skipped += 1
            else:
                pending.append(stock)
    return pending, skipped


def mark_disclosure_range_collected(stock_code: str, date_from: str,
                                    date_to: str,
                                    db_path: Path = DB_PATH):
    """공시가 0건인 경우도 포함해 성공적으로 조회한 범위를 기록한다."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """INSERT INTO disclosure_collection_ranges(
                   stock_code, date_from, date_to, collected_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(stock_code, date_from, date_to) DO UPDATE SET
                   collected_at=excluded.collected_at""",
            (stock_code, date_from, date_to, now),
        )


def disclosure_rows(stock_code: str, date_from: str, date_to: str,
                    db_path: Path = DB_PATH) -> list[dict]:
    """종목 하나의 저장된 공시를 최신순으로 반환한다."""
    if not db_path.exists():
        return []
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT receipt_no, receipt_date, report_name, disclosure_type,
                      submitter, correction, source_url
               FROM disclosures
               WHERE stock_code=? AND receipt_date BETWEEN ? AND ?
               ORDER BY receipt_date DESC, receipt_no DESC""",
            (stock_code, date_from, date_to),
        ).fetchall()
        return [dict(row) for row in rows]


def disclosure_list_rows(date_from: str, date_to: str, keyword: str = "",
                         limit: int = 5000,
                         db_path: Path = DB_PATH) -> tuple[list[dict], int]:
    """공시 탭용 전체 목록과 검색 결과 총건수."""
    if not db_path.exists():
        return [], 0
    keyword = keyword.strip()
    where = "d.receipt_date BETWEEN ? AND ?"
    params: list = [date_from, date_to]
    if keyword:
        where += """ AND (
            s.stock_code LIKE ? OR s.stock_name LIKE ?
            OR d.report_name LIKE ? OR d.submitter LIKE ?)"""
        term = f"%{keyword}%"
        params.extend((term, term, term, term))
    with closing(connect(db_path)) as connection:
        total = connection.execute(
            f"""SELECT COUNT(*)
                FROM disclosures d
                LEFT JOIN stocks s ON s.stock_code=d.stock_code
                WHERE {where}""",
            params,
        ).fetchone()[0]
        rows = connection.execute(
            f"""SELECT d.receipt_no, d.receipt_date, d.stock_code,
                       COALESCE(s.stock_name, '') AS stock_name,
                       d.report_name, d.disclosure_type, d.submitter,
                       d.correction, d.source_url
                FROM disclosures d
                LEFT JOIN stocks s ON s.stock_code=d.stock_code
                WHERE {where}
                ORDER BY d.receipt_date DESC, d.receipt_no DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows], total


def save_dart_corp_codes(mapping: dict[str, str],
                         db_path: Path = DB_PATH) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.executemany(
            """UPDATE stocks SET dart_corp_code=?, updated_at=?
               WHERE stock_code=?""",
            ((corp_code, now, stock_code)
             for stock_code, corp_code in mapping.items()),
        )
        return cursor.rowcount


def save_disclosures(stock_code: str, corp_code: str, rows: list[dict],
                     db_path: Path = DB_PATH) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    values = [(
        row.get("rcept_no", ""), row.get("rcept_dt", ""), stock_code, corp_code,
        row.get("report_nm", ""), row.get("pblntf_ty", ""),
        row.get("flr_nm", ""), int("정정" in row.get("report_nm", "")),
        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row.get('rcept_no', '')}",
        now,
    ) for row in rows if row.get("rcept_no")]
    with closing(connect(db_path)) as connection, connection:
        before = connection.total_changes
        connection.executemany(
            """INSERT INTO disclosures(
                   receipt_no, receipt_date, stock_code, dart_corp_code,
                   report_name, disclosure_type, submitter, correction,
                   source_url, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(receipt_no) DO NOTHING""",
            values,
        )
        return connection.total_changes - before

# -*- coding: utf-8 -*-
"""분석용 SQLite 저장소.

시세·상한가·공시·테마·백테스트 데이터를 한 DB에서 연결하되, 원천 데이터와
가공 결과를 분리한다. 모든 날짜는 YYYY-MM-DD, 시각은 ISO 8601 문자열로 저장한다.
"""
from __future__ import annotations

import sqlite3
import re
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "data" / "market_analysis.db"
SCHEMA_VERSION = 11
ANALYSIS_STOCK_TYPES = (
    "COMMON", "PREFERRED", "SPAC", "FOREIGN", "REIT", "INFRA",
)
DEFAULT_RELATION_GROUPS = (
    # 최대주주·계열 관계는 일반 테마보다 신뢰도가 높아 조건검색에서 같은
    # 묶음으로 보존한다. 이후 우선주·보통주, 지주사 관계도 여기에 추가한다.
    ("셀바스 그룹", "PARENT_SUBSIDIARY", 100, ("108860", "208370")),
)
PREFERRED_SUFFIX_RE = re.compile(r"(?:\d+)?우(?:[A-Z])?$")


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

CREATE TABLE IF NOT EXISTS stock_relation_groups (
    relation_group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL UNIQUE,
    relation_type TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 100,
    source TEXT NOT NULL DEFAULT 'CURATED',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_relation_members (
    relation_group_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    PRIMARY KEY (relation_group_id, stock_code),
    FOREIGN KEY (relation_group_id)
        REFERENCES stock_relation_groups(relation_group_id)
);

CREATE TABLE IF NOT EXISTS dart_relation_checks (
    stock_code TEXT PRIMARY KEY,
    business_year TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS dart_relation_evidence (
    child_stock_code TEXT PRIMARY KEY,
    parent_stock_code TEXT NOT NULL,
    shareholder_name TEXT NOT NULL,
    share_ratio TEXT NOT NULL DEFAULT '',
    share_count TEXT NOT NULL DEFAULT '',
    business_year TEXT NOT NULL,
    receipt_no TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    FOREIGN KEY (child_stock_code) REFERENCES stocks(stock_code),
    FOREIGN KEY (parent_stock_code) REFERENCES stocks(stock_code)
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

CREATE TABLE IF NOT EXISTS theme_daily_stats (
    trade_date TEXT NOT NULL,
    theme_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    limit_up_count INTEGER NOT NULL DEFAULT 0,
    unique_stock_count INTEGER NOT NULL DEFAULT 0,
    trading_value INTEGER NOT NULL DEFAULT 0,
    leader_stock_code TEXT,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, theme_id, source),
    FOREIGN KEY (theme_id) REFERENCES themes(theme_id)
);

CREATE TABLE IF NOT EXISTS theme_rotation_signals (
    as_of_date TEXT NOT NULL,
    theme_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    rotation_score REAL NOT NULL DEFAULT 0,
    member_count INTEGER NOT NULL DEFAULT 0,
    events_5d INTEGER NOT NULL DEFAULT 0,
    events_20d INTEGER NOT NULL DEFAULT 0,
    events_60d INTEGER NOT NULL DEFAULT 0,
    stocks_20d INTEGER NOT NULL DEFAULT 0,
    active_days_20d INTEGER NOT NULL DEFAULT 0,
    days_since_last INTEGER,
    average_rate REAL,
    trading_value INTEGER,
    value_ratio REAL,
    overheat_penalty REAL NOT NULL DEFAULT 0,
    reason_text TEXT NOT NULL DEFAULT '',
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, theme_id, source),
    FOREIGN KEY (theme_id) REFERENCES themes(theme_id)
);

CREATE TABLE IF NOT EXISTS stock_leader_scores (
    as_of_date TEXT NOT NULL,
    theme_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    leader_score REAL NOT NULL DEFAULT 0,
    follower_score REAL NOT NULL DEFAULT 0,
    events_5d INTEGER NOT NULL DEFAULT 0,
    events_20d INTEGER NOT NULL DEFAULT 0,
    events_60d INTEGER NOT NULL DEFAULT 0,
    last_limit_date TEXT,
    change_rate REAL,
    trading_value INTEGER,
    value_ratio REAL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, theme_id, source, stock_code),
    FOREIGN KEY (theme_id) REFERENCES themes(theme_id),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS prediction_model_runs (
    model_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    train_from TEXT NOT NULL,
    train_to TEXT NOT NULL,
    test_from TEXT,
    test_to TEXT,
    train_samples INTEGER NOT NULL DEFAULT 0,
    train_positives INTEGER NOT NULL DEFAULT 0,
    test_samples INTEGER NOT NULL DEFAULT 0,
    test_positives INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    model_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_predictions (
    as_of_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    horizon_days INTEGER NOT NULL DEFAULT 1,
    model_run_id INTEGER NOT NULL,
    probability REAL NOT NULL,
    probability_rank INTEGER NOT NULL,
    feature_version TEXT NOT NULL,
    reason_text TEXT NOT NULL DEFAULT '',
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, stock_code, horizon_days, model_run_id),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code),
    FOREIGN KEY (model_run_id)
        REFERENCES prediction_model_runs(model_run_id)
);

CREATE TABLE IF NOT EXISTS content_sources (
    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT NOT NULL UNIQUE,
    content_kind TEXT NOT NULL DEFAULT '',
    access_mode TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    policy_url TEXT NOT NULL DEFAULT '',
    robots_checked_at TEXT,
    daily_request_limit INTEGER,
    retention_policy TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS realtime_watchlist (
    stock_code TEXT PRIMARY KEY,
    watch_scope TEXT NOT NULL DEFAULT 'ALWAYS',
    source_context TEXT NOT NULL DEFAULT 'MANUAL',
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_news_checked_at TEXT,
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS news_items (
    news_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    source_item_key TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    original_url TEXT NOT NULL DEFAULT '',
    naver_url TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    published_at_source TEXT,
    first_collected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    market_session TEXT NOT NULL DEFAULT '',
    current_title TEXT NOT NULL DEFAULT '',
    current_summary TEXT NOT NULL DEFAULT '',
    current_content_hash TEXT NOT NULL DEFAULT '',
    duplicate_cluster_key TEXT NOT NULL DEFAULT '',
    modified_count INTEGER NOT NULL DEFAULT 0,
    removal_status TEXT NOT NULL DEFAULT 'ACTIVE',
    missing_since TEXT,
    removed_at TEXT,
    UNIQUE (source_id, source_item_key),
    FOREIGN KEY (source_id) REFERENCES content_sources(source_id)
);

CREATE TABLE IF NOT EXISTS news_item_versions (
    news_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    change_type TEXT NOT NULL DEFAULT 'CREATED',
    PRIMARY KEY (news_id, version_no),
    FOREIGN KEY (news_id) REFERENCES news_items(news_id)
);

CREATE TABLE IF NOT EXISTS news_stock_maps (
    news_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    match_method TEXT NOT NULL DEFAULT 'QUERY_STOCK',
    matched_text TEXT NOT NULL DEFAULT '',
    confidence REAL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    mapped_at TEXT NOT NULL,
    PRIMARY KEY (news_id, stock_code),
    FOREIGN KEY (news_id) REFERENCES news_items(news_id),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS news_material_labels (
    news_id INTEGER NOT NULL,
    material_type TEXT NOT NULL,
    confidence REAL,
    classifier_version TEXT NOT NULL DEFAULT 'rules_v1',
    classified_at TEXT NOT NULL,
    PRIMARY KEY (news_id, material_type, classifier_version),
    FOREIGN KEY (news_id) REFERENCES news_items(news_id)
);

CREATE TABLE IF NOT EXISTS discussion_posts (
    post_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    source_post_key TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    published_at_source TEXT,
    first_collected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    author_key_hash TEXT NOT NULL DEFAULT '',
    current_text_hash TEXT NOT NULL DEFAULT '',
    normalized_text_hash TEXT NOT NULL DEFAULT '',
    simhash64 TEXT NOT NULL DEFAULT '',
    modified_count INTEGER NOT NULL DEFAULT 0,
    removal_status TEXT NOT NULL DEFAULT 'ACTIVE',
    missing_since TEXT,
    removed_at TEXT,
    UNIQUE (source_id, source_post_key),
    FOREIGN KEY (source_id) REFERENCES content_sources(source_id),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS discussion_post_versions (
    post_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    text_hash TEXT NOT NULL DEFAULT '',
    simhash64 TEXT NOT NULL DEFAULT '',
    change_type TEXT NOT NULL DEFAULT 'CREATED',
    PRIMARY KEY (post_id, version_no),
    FOREIGN KEY (post_id) REFERENCES discussion_posts(post_id)
);

CREATE TABLE IF NOT EXISTS discussion_metrics (
    bucket_at TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    window_minutes INTEGER NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 0,
    unique_author_count INTEGER NOT NULL DEFAULT 0,
    similar_repeat_count INTEGER NOT NULL DEFAULT 0,
    similar_repeat_rate REAL,
    edited_count INTEGER NOT NULL DEFAULT 0,
    removed_count INTEGER NOT NULL DEFAULT 0,
    mention_acceleration REAL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (bucket_at, stock_code, window_minutes),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS content_request_logs (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    query_text TEXT NOT NULL DEFAULT '',
    stock_code TEXT,
    http_status INTEGER,
    received_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER,
    quota_used INTEGER,
    quota_remaining INTEGER,
    error_text TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (source_id) REFERENCES content_sources(source_id),
    FOREIGN KEY (stock_code) REFERENCES stocks(stock_code)
);

CREATE TABLE IF NOT EXISTS market_daily_features (
    trade_date TEXT NOT NULL,
    market TEXT NOT NULL,
    stock_count INTEGER NOT NULL DEFAULT 0,
    rising_count INTEGER NOT NULL DEFAULT 0,
    falling_count INTEGER NOT NULL DEFAULT 0,
    rise_ratio REAL,
    average_rate REAL,
    trading_value INTEGER,
    limit_up_count INTEGER NOT NULL DEFAULT 0,
    preferred_limit_up_count INTEGER NOT NULL DEFAULT 0,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, market)
);

CREATE TABLE IF NOT EXISTS market_index_prices (
    trade_date TEXT NOT NULL,
    index_code TEXT NOT NULL,
    market TEXT NOT NULL,
    open_value REAL,
    high_value REAL,
    low_value REAL,
    close_value REAL,
    volume INTEGER,
    trading_value INTEGER,
    source TEXT NOT NULL DEFAULT 'KIWOOM',
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, index_code)
);

CREATE TABLE IF NOT EXISTS market_investor_flows (
    trade_date TEXT NOT NULL,
    market TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    industry_name TEXT NOT NULL DEFAULT '',
    change_rate REAL,
    volume INTEGER,
    securities_net_amount_million INTEGER,
    insurance_net_amount_million INTEGER,
    investment_trust_net_amount_million INTEGER,
    bank_net_amount_million INTEGER,
    merchant_bank_net_amount_million INTEGER,
    fund_net_amount_million INTEGER,
    other_corporation_net_amount_million INTEGER,
    individual_net_amount_million INTEGER,
    foreign_net_amount_million INTEGER,
    domestic_foreign_net_amount_million INTEGER,
    national_net_amount_million INTEGER,
    private_fund_net_amount_million INTEGER,
    institution_net_amount_million INTEGER,
    source TEXT NOT NULL DEFAULT 'KIWOOM',
    collected_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, market, industry_code)
);

CREATE TABLE IF NOT EXISTS external_market_ticks (
    indicator_code TEXT NOT NULL,
    indicator_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    value REAL NOT NULL,
    previous_close REAL,
    change_rate REAL,
    currency TEXT NOT NULL DEFAULT '',
    exchange_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (indicator_code, observed_at, source)
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

CREATE TABLE IF NOT EXISTS condition_snapshot_runs (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_seq TEXT NOT NULL,
    condition_name TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT 'KRX',
    captured_at TEXT NOT NULL,
    stock_count INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS condition_snapshot_members (
    snapshot_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, stock_code),
    FOREIGN KEY (snapshot_id) REFERENCES condition_snapshot_runs(snapshot_id)
        ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_condition_snapshots_seq_time
    ON condition_snapshot_runs(condition_seq, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_condition_snapshot_members_code
    ON condition_snapshot_members(stock_code, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_theme_daily_stats_source_date
    ON theme_daily_stats(source, trade_date);
CREATE INDEX IF NOT EXISTS idx_rotation_signals_source_date_score
    ON theme_rotation_signals(source, as_of_date, rotation_score);
CREATE INDEX IF NOT EXISTS idx_stock_leader_theme_date
    ON stock_leader_scores(theme_id, source, as_of_date);
CREATE INDEX IF NOT EXISTS idx_relation_members_stock
    ON stock_relation_members(stock_code, relation_group_id);
CREATE INDEX IF NOT EXISTS idx_stock_predictions_date_rank
    ON stock_predictions(as_of_date, horizon_days, probability_rank);
CREATE INDEX IF NOT EXISTS idx_news_items_published
    ON news_items(published_at_source DESC);
CREATE INDEX IF NOT EXISTS idx_news_stock_maps_code
    ON news_stock_maps(stock_code, news_id);
CREATE INDEX IF NOT EXISTS idx_news_duplicate_cluster
    ON news_items(duplicate_cluster_key);
CREATE INDEX IF NOT EXISTS idx_discussion_posts_code_published
    ON discussion_posts(stock_code, published_at_source DESC);
CREATE INDEX IF NOT EXISTS idx_discussion_metrics_code_bucket
    ON discussion_metrics(stock_code, bucket_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_request_logs_source_time
    ON content_request_logs(source_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_daily_features_market_date
    ON market_daily_features(market, trade_date);
CREATE INDEX IF NOT EXISTS idx_market_index_prices_market_date
    ON market_index_prices(market, trade_date);
CREATE INDEX IF NOT EXISTS idx_market_investor_flows_market_date
    ON market_investor_flows(market, trade_date);
CREATE INDEX IF NOT EXISTS idx_external_market_ticks_latest
    ON external_market_ticks(indicator_code, observed_at DESC);
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
            connection.executemany(
                """INSERT OR IGNORE INTO content_sources(
                       source_code, content_kind, access_mode, enabled,
                       policy_url, robots_checked_at, daily_request_limit,
                       retention_policy, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        "NAVER_SEARCH_NEWS", "NEWS", "OFFICIAL_API", 1,
                        "https://developers.naver.com/docs/serviceapi/"
                        "search/news/news.md",
                        now, 25000,
                        "API 약관 범위 내 메타데이터·파생값 보관", now,
                    ),
                    (
                        "NAVER_FINANCE_NEWS", "NEWS", "MANUAL_WEBVIEW", 0,
                        "https://finance.naver.com/robots.txt",
                        now, None, "자동수집 금지·사용자 웹뷰 열람 전용", now,
                    ),
                    (
                        "NAVER_FINANCE_BOARD", "DISCUSSION",
                        "MANUAL_WEBVIEW", 0,
                        "https://finance.naver.com/robots.txt",
                        now, None, "자동수집 금지·사용자 웹뷰 열람 전용", now,
                    ),
                ),
            )
            for group_name, relation_type, priority, stock_codes in DEFAULT_RELATION_GROUPS:
                connection.execute(
                    """INSERT OR IGNORE INTO stock_relation_groups(
                           group_name, relation_type, priority, source, updated_at)
                       VALUES (?, ?, ?, 'CURATED', ?)""",
                    (group_name, relation_type, priority, now),
                )
                group_id = connection.execute(
                    """SELECT relation_group_id FROM stock_relation_groups
                       WHERE group_name=?""", (group_name,)
                ).fetchone()[0]
                connection.executemany(
                    """INSERT OR IGNORE INTO stock_relation_members(
                           relation_group_id, stock_code)
                       VALUES (?, ?)""",
                    ((group_id, code) for code in stock_codes),
                )
    return db_path


def save_condition_snapshot(condition_seq: str, condition_name: str,
                            codes: list[str], captured_at: str | None = None,
                            market: str = "KRX", truncated: bool = False,
                            db_path: Path = DB_PATH) -> int:
    """화면 없이 조회한 조건검색 결과와 순서를 저장한다.

    종목 자체의 시세·테마는 별도 원천 테이블에서 관리하므로 이 단계에서는
    조건식 결과와 조회 시각만 보존한다. 최대 100종목 제한에 걸린 경우
    ``truncated``를 표시해 후속 구간분할 수집이 가능하도록 한다.
    """
    timestamp = captured_at or datetime.now().astimezone().isoformat(timespec="seconds")
    normalized = []
    seen = set()
    for code in codes:
        value = str(code or "").strip().upper().lstrip("A")
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    with closing(connect(db_path)) as connection:
        with connection:
            cursor = connection.execute(
                """INSERT INTO condition_snapshot_runs(
                       condition_seq, condition_name, market, captured_at,
                       stock_count, truncated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(condition_seq), str(condition_name or ""), str(market or "KRX"),
                 timestamp, len(normalized), int(bool(truncated))),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO condition_snapshot_members(
                       snapshot_id, stock_code, position)
                   VALUES (?, ?, ?)""",
                [(snapshot_id, code, position)
                 for position, code in enumerate(normalized, 1)],
            )
    return snapshot_id


def recent_condition_snapshots(condition_seq: str | None = None, limit: int = 20,
                               db_path: Path = DB_PATH) -> list[dict]:
    """최근 조건검색 일반조회 요약을 반환한다."""
    clauses = []
    params: list = []
    if condition_seq is not None:
        clauses.append("condition_seq = ?")
        params.append(str(condition_seq))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            f"""SELECT snapshot_id, condition_seq, condition_name, market,
                       captured_at, stock_count, truncated
                FROM condition_snapshot_runs{where}
                ORDER BY captured_at DESC, snapshot_id DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


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
            "market_index_prices": 0,
            "market_investor_flows": 0,
            "external_market_ticks": 0,
            "last_trade_date": "",
            "last_run": "",
        }
    initialize(db_path)
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
        result["market_index_prices"] = connection.execute(
            "SELECT COUNT(*) FROM market_index_prices"
        ).fetchone()[0]
        result["market_investor_flows"] = connection.execute(
            "SELECT COUNT(*) FROM market_investor_flows"
        ).fetchone()[0]
        result["external_market_ticks"] = connection.execute(
            "SELECT COUNT(*) FROM external_market_ticks"
        ).fetchone()[0]
        row = connection.execute(
            """SELECT data_type, status, started_at
               FROM collection_runs ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
        result["last_run"] = (
            f"{row['started_at']} / {row['data_type']} / {row['status']}"
            if row else ""
        )
        return result


def save_market_index_prices(rows: list[dict],
                             db_path: Path = DB_PATH) -> int:
    """키움 업종 일봉을 코스피·코스닥 지수 원천 데이터로 저장한다."""
    if not rows:
        return 0
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    values = [
        (
            str(row.get("date") or ""),
            str(row.get("index_code") or ""),
            str(row.get("market") or ""),
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            int(row.get("volume") or 0),
            int(row.get("trading_value") or 0),
            str(row.get("source") or "KIWOOM"),
            now,
        )
        for row in rows
        if row.get("date") and row.get("index_code")
    ]
    if not values:
        return 0
    with closing(connect(db_path)) as connection, connection:
        connection.executemany(
            """INSERT INTO market_index_prices(
                   trade_date, index_code, market, open_value, high_value,
                   low_value, close_value, volume, trading_value, source,
                   collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_date, index_code) DO UPDATE SET
                   market=excluded.market,
                   open_value=excluded.open_value,
                   high_value=excluded.high_value,
                   low_value=excluded.low_value,
                   close_value=excluded.close_value,
                   volume=excluded.volume,
                   trading_value=excluded.trading_value,
                   source=excluded.source,
                   collected_at=excluded.collected_at""",
            values,
        )
    return len(values)


def market_index_coverage(db_path: Path = DB_PATH) -> list[dict]:
    """저장된 시장 지수별 건수와 날짜 범위를 반환한다."""
    if not db_path.exists():
        return []
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT index_code, market, COUNT(*) AS row_count,
                      MIN(trade_date) AS date_from,
                      MAX(trade_date) AS date_to
                 FROM market_index_prices
                GROUP BY index_code, market
                ORDER BY index_code"""
        ).fetchall()
        return [dict(row) for row in rows]


def save_market_investor_flows(rows: list[dict],
                               db_path: Path = DB_PATH) -> int:
    """키움 시장·업종별 투자자 순매수를 백만원 단위로 저장한다."""
    if not rows:
        return 0
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    fields = (
        "securities", "insurance", "investment_trust", "bank",
        "merchant_bank", "fund", "other_corporation", "individual",
        "foreign", "domestic_foreign", "national", "private_fund",
        "institution",
    )
    values = []
    for row in rows:
        trade_date = str(row.get("date") or "")
        market = str(row.get("market") or "")
        industry_code = str(row.get("industry_code") or "")
        if not trade_date or not market or not industry_code:
            continue
        values.append((
            trade_date,
            market,
            industry_code,
            str(row.get("industry_name") or ""),
            row.get("change_rate"),
            int(row.get("volume") or 0),
            *(int(row.get(f"{field}_net_amount_million") or 0)
              for field in fields),
            str(row.get("source") or "KIWOOM"),
            now,
        ))
    if not values:
        return 0
    with closing(connect(db_path)) as connection, connection:
        connection.executemany(
            """INSERT INTO market_investor_flows(
                   trade_date, market, industry_code, industry_name,
                   change_rate, volume,
                   securities_net_amount_million,
                   insurance_net_amount_million,
                   investment_trust_net_amount_million,
                   bank_net_amount_million,
                   merchant_bank_net_amount_million,
                   fund_net_amount_million,
                   other_corporation_net_amount_million,
                   individual_net_amount_million,
                   foreign_net_amount_million,
                   domestic_foreign_net_amount_million,
                   national_net_amount_million,
                   private_fund_net_amount_million,
                   institution_net_amount_million,
                   source, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?)
               ON CONFLICT(trade_date, market, industry_code) DO UPDATE SET
                   industry_name=excluded.industry_name,
                   change_rate=excluded.change_rate,
                   volume=excluded.volume,
                   securities_net_amount_million=
                       excluded.securities_net_amount_million,
                   insurance_net_amount_million=
                       excluded.insurance_net_amount_million,
                   investment_trust_net_amount_million=
                       excluded.investment_trust_net_amount_million,
                   bank_net_amount_million=
                       excluded.bank_net_amount_million,
                   merchant_bank_net_amount_million=
                       excluded.merchant_bank_net_amount_million,
                   fund_net_amount_million=
                       excluded.fund_net_amount_million,
                   other_corporation_net_amount_million=
                       excluded.other_corporation_net_amount_million,
                   individual_net_amount_million=
                       excluded.individual_net_amount_million,
                   foreign_net_amount_million=
                       excluded.foreign_net_amount_million,
                   domestic_foreign_net_amount_million=
                       excluded.domestic_foreign_net_amount_million,
                   national_net_amount_million=
                       excluded.national_net_amount_million,
                   private_fund_net_amount_million=
                       excluded.private_fund_net_amount_million,
                   institution_net_amount_million=
                       excluded.institution_net_amount_million,
                   source=excluded.source,
                   collected_at=excluded.collected_at""",
            values,
        )
    return len(values)


def pending_market_investor_flow_requests(
    date_from: str, date_to: str, db_path: Path = DB_PATH,
) -> list[tuple[str, str]]:
    """거래일별로 아직 저장되지 않은 KOSPI·KOSDAQ 요청만 반환한다."""
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        trade_dates = [
            row[0] for row in connection.execute(
                """SELECT DISTINCT trade_date
                     FROM daily_prices
                    WHERE trade_date BETWEEN ? AND ?
                    ORDER BY trade_date""",
                (date_from, date_to),
            ).fetchall()
        ]
        completed = {
            (row["trade_date"], row["market"])
            for row in connection.execute(
                """SELECT trade_date, market
                     FROM market_investor_flows
                    WHERE trade_date BETWEEN ? AND ?
                    GROUP BY trade_date, market
                   HAVING COUNT(*)>0""",
                (date_from, date_to),
            ).fetchall()
        }
    return [
        (trade_date, market)
        for trade_date in trade_dates
        for market in ("KOSPI", "KOSDAQ")
        if (trade_date, market) not in completed
    ]


def market_investor_flow_coverage(db_path: Path = DB_PATH) -> list[dict]:
    """시장별 수급 저장 건수와 날짜 범위를 반환한다."""
    if not db_path.exists():
        return []
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT market, COUNT(*) AS row_count,
                      COUNT(DISTINCT trade_date) AS date_count,
                      MIN(trade_date) AS date_from,
                      MAX(trade_date) AS date_to
                 FROM market_investor_flows
                GROUP BY market
                ORDER BY market"""
        ).fetchall()
        return [dict(row) for row in rows]


def save_external_market_quotes(
    rows: list[dict], db_path: Path = DB_PATH,
) -> int:
    """해외지표 현재값을 원본 시각과 실제 수집시각을 분리해 저장한다."""
    if not rows:
        return 0
    initialize(db_path)
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    values = [
        (
            str(row.get("indicator_code") or ""),
            str(row.get("indicator_name") or ""),
            str(row.get("symbol") or ""),
            str(row.get("observed_at") or ""),
            float(row.get("value")),
            (
                float(row["previous_close"])
                if row.get("previous_close") is not None else None
            ),
            (
                float(row["change_rate"])
                if row.get("change_rate") is not None else None
            ),
            str(row.get("currency") or ""),
            str(row.get("exchange") or ""),
            str(row.get("source") or "YAHOO_CHART"),
            collected_at,
        )
        for row in rows
        if row.get("indicator_code")
        and row.get("observed_at")
        and row.get("value") is not None
    ]
    if not values:
        return 0
    with closing(connect(db_path)) as connection, connection:
        connection.executemany(
            """INSERT INTO external_market_ticks(
                   indicator_code, indicator_name, symbol, observed_at,
                   value, previous_close, change_rate, currency,
                   exchange_name, source, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(indicator_code, observed_at, source) DO UPDATE SET
                   indicator_name=excluded.indicator_name,
                   symbol=excluded.symbol,
                   value=excluded.value,
                   previous_close=excluded.previous_close,
                   change_rate=excluded.change_rate,
                   currency=excluded.currency,
                   exchange_name=excluded.exchange_name,
                   collected_at=excluded.collected_at""",
            values,
        )
    return len(values)


def latest_external_market_quotes(
    db_path: Path = DB_PATH,
) -> list[dict]:
    """지표별 원본 게시시각이 가장 최근인 한 행을 반환한다."""
    if not db_path.exists():
        return []
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """WITH ranked AS (
                   SELECT *,
                          ROW_NUMBER() OVER (
                              PARTITION BY indicator_code
                              ORDER BY observed_at DESC, collected_at DESC
                          ) AS rn
                     FROM external_market_ticks
               )
               SELECT indicator_code, indicator_name, symbol, observed_at,
                      value, previous_close, change_rate, currency,
                      exchange_name, source, collected_at
                 FROM ranked
                WHERE rn=1
                ORDER BY indicator_code"""
        ).fetchall()
        return [dict(row) for row in rows]


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
    """최신 종목 목록을 동기화해 신규 상장 종목도 분석 DB에 추가한다."""
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
            """INSERT INTO stocks(
                   stock_name, market, stock_type, sector_name, listed_date,
                   shares_outstanding, updated_at, stock_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(stock_code) DO UPDATE SET
                   stock_name=excluded.stock_name, market=excluded.market,
                   stock_type=excluded.stock_type,
                   sector_name=excluded.sector_name,
                   listed_date=excluded.listed_date,
                   shares_outstanding=excluded.shares_outstanding,
                   updated_at=excluded.updated_at""",
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


def active_theme_labels(db_path: Path = DB_PATH) -> dict[str, tuple[str, ...]]:
    """현재 유효한 종목별 테마명을 출처 우선순위대로 반환한다.

    조건검색 실시간 정렬은 네이버 테마를 기본 분류로 쓰되, 네이버에 없는
    종목은 키움 등 수집된 보조 출처의 테마로 묶는다. 한 종목의 복수 테마는
    모두 보존하며 화면에서 현재 가장 강한 묶음을 대표 테마로 선택한다.
    """
    if not db_path.exists():
        return {}
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT st.stock_code, t.theme_name, st.source
                 FROM stock_themes st
                 JOIN themes t ON t.theme_id=st.theme_id
                WHERE st.valid_to IS NULL
                  AND TRIM(t.theme_name)<>''
                ORDER BY st.stock_code,
                    CASE st.source
                        WHEN 'NAVER' THEN 0
                        WHEN 'KIWOOM' THEN 1
                        WHEN 'WICS' THEN 2
                        WHEN 'KRX' THEN 3
                        WHEN 'DART' THEN 4
                        ELSE 9
                    END,
                    t.theme_name""",
        ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        code = str(row["stock_code"] or "").removesuffix("_AL")
        theme = str(row["theme_name"] or "").strip()
        if code and theme and theme not in result.setdefault(code, []):
            result[code].append(theme)
    return {code: tuple(themes) for code, themes in result.items()}


def active_relation_groups(db_path: Path = DB_PATH) -> dict[str, tuple[str, ...]]:
    """조건검색에서 우선 묶을 명확한 관계 종목 그룹을 반환한다.

    수동 등록한 계열·지분 관계와 함께, 종목유형이 우선주인 종목을 이름으로
    본주에 자동 연결한다. 본주 종목명이 실제 카탈로그에 있을 때만 추가한다.
    """
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT g.group_name, m.stock_code
                 FROM stock_relation_groups g
                 JOIN stock_relation_members m
                   ON m.relation_group_id=g.relation_group_id
                ORDER BY g.priority, g.group_name, m.stock_code""",
        ).fetchall()
        stock_rows = connection.execute(
            """SELECT stock_code, stock_name, stock_type
                 FROM stocks
                WHERE stock_type IN ('COMMON', 'PREFERRED')""",
        ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        group = str(row["group_name"] or "").strip()
        code = str(row["stock_code"] or "").removesuffix("_AL")
        if group and code:
            result.setdefault(group, []).append(code)
    common_codes = {
        str(row["stock_name"] or "").strip(): str(row["stock_code"] or "")
        for row in stock_rows if row["stock_type"] == "COMMON"
    }
    preferred_groups: dict[str, list[str]] = {}
    for row in stock_rows:
        if row["stock_type"] != "PREFERRED":
            continue
        name = str(row["stock_name"] or "").strip()
        base_name = PREFERRED_SUFFIX_RE.sub("", name)
        common_code = common_codes.get(base_name)
        preferred_code = str(row["stock_code"] or "")
        if not base_name or not common_code or not preferred_code:
            continue
        preferred_groups.setdefault(base_name, [common_code]).append(preferred_code)
    for base_name, codes in preferred_groups.items():
        result.setdefault(f"{base_name} 우선주·본주", []).extend(codes)
    return {
        group: tuple(dict.fromkeys(codes)) for group, codes in result.items()
        if len(set(codes)) >= 2
    }


def _normalized_company_name(value: str) -> str:
    """DART 법인명과 거래소 종목명을 비교하기 위한 보수적 정규화."""
    value = str(value or "").casefold()
    value = value.replace("주식회사", "").replace("(주)", "").replace("㈜", "")
    value = value.replace("에이아이", "ai")
    return re.sub(r"[^0-9a-z가-힣]", "", value)


def save_dart_parent_relations(
    parent_evidence_by_child: dict[str, str | dict], db_path: Path = DB_PATH,
) -> tuple[int, int]:
    """DART 최대주주가 상장사인 경우에만 관계와 확인 근거를 저장한다."""
    if not parent_evidence_by_child:
        return 0, 0
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection, connection:
        names: dict[str, tuple[str, str]] = {}
        for row in connection.execute(
                "SELECT stock_code, stock_name FROM stocks").fetchall():
            normalized = _normalized_company_name(row["stock_name"])
            if normalized and normalized not in names:
                names[normalized] = (row["stock_code"], row["stock_name"])
        grouped: dict[tuple[str, str], set[str]] = {}
        matched_evidence: list[tuple[str, str, str, str, str, str, str]] = []
        for child_code, source in parent_evidence_by_child.items():
            evidence = source if isinstance(source, dict) else {"name": source}
            parent_name = str(evidence.get("name") or "").strip()
            parent = names.get(_normalized_company_name(parent_name))
            if not parent or parent[0] == child_code:
                continue
            grouped.setdefault(parent, set()).add(str(child_code))
            matched_evidence.append((
                str(child_code), parent[0], parent_name,
                str(evidence.get("share_ratio") or "").strip(),
                str(evidence.get("share_count") or "").strip(),
                str(evidence.get("business_year") or "").strip(),
                str(evidence.get("receipt_no") or "").strip(),
            ))

        groups = members = 0
        for (parent_code, parent_name), child_codes in grouped.items():
            group_name = f"{parent_name} 그룹"
            connection.execute(
                """INSERT INTO stock_relation_groups(
                       group_name, relation_type, priority, source, updated_at)
                   VALUES (?, 'PARENT_SUBSIDIARY', 90, 'DART', ?)
                   ON CONFLICT(group_name) DO UPDATE SET
                       relation_type=excluded.relation_type,
                       priority=excluded.priority, source=excluded.source,
                       updated_at=excluded.updated_at""",
                (group_name, now),
            )
            group_id = connection.execute(
                "SELECT relation_group_id FROM stock_relation_groups "
                "WHERE group_name=?", (group_name,)
            ).fetchone()[0]
            codes = {parent_code, *child_codes}
            connection.executemany(
                """INSERT OR IGNORE INTO stock_relation_members(
                       relation_group_id, stock_code) VALUES (?, ?)""",
                ((group_id, code) for code in codes),
            )
            groups += 1
            members += len(codes)
        if matched_evidence:
            connection.executemany(
                """INSERT INTO dart_relation_evidence(
                       child_stock_code, parent_stock_code, shareholder_name,
                       share_ratio, share_count, business_year, receipt_no, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(child_stock_code) DO UPDATE SET
                       parent_stock_code=excluded.parent_stock_code,
                       shareholder_name=excluded.shareholder_name,
                       share_ratio=excluded.share_ratio,
                       share_count=excluded.share_count,
                       business_year=excluded.business_year,
                       receipt_no=excluded.receipt_no,
                       checked_at=excluded.checked_at""",
                [(*row, now) for row in matched_evidence],
            )
    return groups, members


def dart_relation_evidence_labels(
    db_path: Path = DB_PATH,
) -> dict[str, tuple[str, ...]]:
    """종목명 도구설명에 표시할 DART 최대주주 관계의 확인 근거를 반환한다."""
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT child_stock_code, shareholder_name, share_ratio,
                      share_count, business_year, receipt_no
                 FROM dart_relation_evidence
                ORDER BY child_stock_code""",
        ).fetchall()
    labels: dict[str, list[str]] = {}
    for row in rows:
        code = str(row["child_stock_code"] or "")
        shareholder = str(row["shareholder_name"] or "").strip()
        if not code or not shareholder:
            continue
        parts = [f"{shareholder} 최대주주"]
        ratio = str(row["share_ratio"] or "").strip()
        if ratio:
            parts.append(f"지분율 {ratio}%")
        shares = str(row["share_count"] or "").strip()
        if shares:
            parts.append(f"보유주식 {shares}주")
        year = str(row["business_year"] or "").strip()
        if year:
            parts.append(f"{year} 사업보고서")
        receipt = str(row["receipt_no"] or "").strip()
        if receipt:
            parts.append(f"접수번호 {receipt}")
        labels.setdefault(code, []).append(" · ".join(parts))
    return {code: tuple(values) for code, values in labels.items()}


def pending_dart_relation_checks(
    stock_codes: list[str] | tuple[str, ...] | set[str], business_year: str,
    db_path: Path = DB_PATH,
) -> list[str]:
    """해당 사업연도에 최대주주 관계를 아직 확인하지 않은 종목만 반환한다."""
    codes = list(dict.fromkeys(
        str(code or "").removesuffix("_AL").strip() for code in stock_codes
        if str(code or "").strip()))
    if not codes:
        return []
    initialize(db_path)
    placeholders = ",".join("?" for _ in codes)
    with closing(connect(db_path)) as connection:
        checked = {
            str(row["stock_code"])
            for row in connection.execute(
                f"""SELECT stock_code FROM dart_relation_checks
                      WHERE business_year=? AND stock_code IN ({placeholders})""",
                (str(business_year), *codes),
            ).fetchall()
        }
    return [code for code in codes if code not in checked]


def save_dart_relation_checks(
    results: dict[str, str], business_year: str, db_path: Path = DB_PATH,
) -> int:
    """DART 최대주주 조회 성공 여부를 저장해 자동수집 중복을 막는다."""
    if not results:
        return 0
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    values = [
        (str(code).removesuffix("_AL"), str(business_year), str(result), now)
        for code, result in results.items() if str(code or "").strip()
    ]
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.executemany(
            """INSERT INTO dart_relation_checks(
                   stock_code, business_year, result, checked_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(stock_code) DO UPDATE SET
                   business_year=excluded.business_year,
                   result=excluded.result, checked_at=excluded.checked_at""",
            values,
        )
        return cursor.rowcount


def rebuild_rotation_analysis(as_of_date: str = "", source: str = "NAVER",
                              db_path: Path = DB_PATH) -> dict:
    """현재 테마 분류와 저장된 일봉으로 순환매 후보 스냅샷을 계산한다.

    과거 시점의 실제 테마 구성원이 아니라 현재 유효한 구성원을 과거 일봉에
    연결하는 1차 분석이다. 결과를 원천 테이블과 분리해 반복 검증할 수 있게 한다.
    """
    initialize(db_path)
    source = str(source or "NAVER").upper()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection, connection:
        if as_of_date:
            row = connection.execute(
                "SELECT MAX(trade_date) FROM daily_prices WHERE trade_date<=?",
                (as_of_date,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT MAX(trade_date) FROM daily_prices"
            ).fetchone()
        as_of_date = (row[0] if row else "") or ""
        if not as_of_date:
            return {"as_of_date": "", "source": source, "themes": 0,
                    "stocks": 0}

        dates = [
            row[0] for row in connection.execute(
                """SELECT DISTINCT trade_date FROM daily_prices
                   WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 61""",
                (as_of_date,),
            ).fetchall()
        ]
        if not dates:
            return {"as_of_date": "", "source": source, "themes": 0,
                    "stocks": 0}

        def cutoff(days: int) -> str:
            return dates[min(days - 1, len(dates) - 1)]

        cutoff_5 = cutoff(5)
        cutoff_20 = cutoff(20)
        cutoff_60 = cutoff(60)
        recent_price_dates = dates[:min(6, len(dates))]
        recent_price_cutoff = recent_price_dates[-1]
        date_position = {date: index for index, date in enumerate(dates)}

        membership_rows = connection.execute(
            """SELECT DISTINCT st.theme_id, st.stock_code, t.theme_name
                 FROM stock_themes st
                 JOIN themes t ON t.theme_id=st.theme_id
                WHERE st.source=? AND st.valid_to IS NULL
                ORDER BY st.theme_id, st.stock_code""",
            (source,),
        ).fetchall()
        members: dict[int, set[str]] = {}
        theme_names: dict[int, str] = {}
        all_codes: set[str] = set()
        for member in membership_rows:
            theme_id = int(member["theme_id"])
            code = member["stock_code"]
            members.setdefault(theme_id, set()).add(code)
            theme_names[theme_id] = member["theme_name"]
            all_codes.add(code)
        if not members:
            return {"as_of_date": as_of_date, "source": source, "themes": 0,
                    "stocks": 0}

        event_rows = connection.execute(
            """WITH membership AS (
                   SELECT DISTINCT theme_id, stock_code
                     FROM stock_themes
                    WHERE source=? AND valid_to IS NULL
               )
               SELECT m.theme_id, e.trade_date, e.stock_code,
                      COALESCE(p.trading_value, 0) AS trading_value
                 FROM membership m
                 JOIN limit_up_events e ON e.stock_code=m.stock_code
                 JOIN daily_prices p
                   ON p.trade_date=e.trade_date AND p.stock_code=e.stock_code
                WHERE e.trade_date<=?
                ORDER BY e.trade_date, m.theme_id, e.stock_code""",
            (source, as_of_date),
        ).fetchall()
        events_by_theme: dict[int, list[dict]] = {}
        events_by_stock: dict[tuple[int, str], list[dict]] = {}
        daily_groups: dict[tuple[str, int], list[dict]] = {}
        for event_row in event_rows:
            event = dict(event_row)
            theme_id = int(event["theme_id"])
            events_by_theme.setdefault(theme_id, []).append(event)
            events_by_stock.setdefault(
                (theme_id, event["stock_code"]), []).append(event)
            daily_groups.setdefault(
                (event["trade_date"], theme_id), []).append(event)

        stock_price_rows = connection.execute(
            """WITH member_codes AS (
                   SELECT DISTINCT stock_code FROM stock_themes
                    WHERE source=? AND valid_to IS NULL
               )
               SELECT p.trade_date, p.stock_code, p.change_rate,
                      COALESCE(p.trading_value, 0) AS trading_value
                 FROM daily_prices p
                 JOIN member_codes m ON m.stock_code=p.stock_code
                WHERE p.trade_date BETWEEN ? AND ?""",
            (source, recent_price_cutoff, as_of_date),
        ).fetchall()
        prices: dict[str, dict[str, dict]] = {}
        for price_row in stock_price_rows:
            prices.setdefault(price_row["stock_code"], {})[
                price_row["trade_date"]] = dict(price_row)

        connection.execute(
            "DELETE FROM theme_daily_stats WHERE source=?",
            (source,),
        )
        daily_values = []
        for (trade_date, theme_id), grouped_events in daily_groups.items():
            leader = max(
                grouped_events,
                key=lambda item: int(item["trading_value"] or 0),
            )
            daily_values.append((
                trade_date, theme_id, source, len(grouped_events),
                len({item["stock_code"] for item in grouped_events}),
                sum(int(item["trading_value"] or 0)
                    for item in grouped_events),
                leader["stock_code"], now,
            ))
        connection.executemany(
            """INSERT INTO theme_daily_stats(
                   trade_date, theme_id, source, limit_up_count,
                   unique_stock_count, trading_value, leader_stock_code,
                   calculated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            daily_values,
        )

        def bounded(value: float, low: float, high: float) -> float:
            return max(low, min(high, value))

        signal_values = []
        signal_scores: dict[int, float] = {}
        signal_phases: dict[int, str] = {}
        for theme_id, theme_members in members.items():
            theme_events = events_by_theme.get(theme_id, [])
            recent_60 = [
                event for event in theme_events
                if event["trade_date"] >= cutoff_60
            ]
            recent_20 = [
                event for event in recent_60
                if event["trade_date"] >= cutoff_20
            ]
            recent_5 = [
                event for event in recent_20
                if event["trade_date"] >= cutoff_5
            ]
            previous_15 = [
                event for event in recent_20
                if event["trade_date"] < cutoff_5
            ]
            older_40 = [
                event for event in recent_60
                if event["trade_date"] < cutoff_20
            ]
            stocks_5 = {event["stock_code"] for event in recent_5}
            stocks_20 = {event["stock_code"] for event in recent_20}
            active_days_20 = {
                event["trade_date"] for event in recent_20
            }
            last_date = (
                max((event["trade_date"] for event in theme_events),
                    default="")
            )
            days_since_last = (
                date_position.get(last_date, 999) if last_date else 999
            )

            latest_rates = []
            latest_value = 0
            previous_totals = {date: 0 for date in recent_price_dates[1:]}
            for code in theme_members:
                code_prices = prices.get(code, {})
                latest = code_prices.get(as_of_date)
                if latest:
                    latest_rates.append(float(latest["change_rate"] or 0))
                    latest_value += int(latest["trading_value"] or 0)
                for date in previous_totals:
                    previous = code_prices.get(date)
                    if previous:
                        previous_totals[date] += int(
                            previous["trading_value"] or 0)
            average_rate = (
                sum(latest_rates) / len(latest_rates)
                if latest_rates else 0.0
            )
            previous_average_value = (
                sum(previous_totals.values()) / len(previous_totals)
                if previous_totals else 0
            )
            value_ratio = (
                latest_value / previous_average_value
                if previous_average_value else 0.0
            )

            breadth_5 = (
                len(stocks_5) / len(theme_members) if theme_members else 0
            )
            recent_rate = len(recent_5) / 5
            previous_rate = len(previous_15) / 15
            acceleration = (
                recent_rate / previous_rate if previous_rate > 0
                else (2.0 if recent_5 else 0.0)
            )
            recency_points = (
                max(0.0, 20.0 - days_since_last * 4.0)
                if days_since_last < 999 else 0.0
            )
            activity_points = min(20.0, len(recent_5) * 4.0)
            breadth_points = min(15.0, breadth_5 * 75.0)
            acceleration_points = (
                min(15.0, 5.0 + max(0.0, acceleration - 1.0) * 5.0)
                if recent_5 else 0.0
            )
            momentum_points = bounded(average_rate, 0.0, 5.0) * 2.0
            value_points = bounded((value_ratio - 1.0) * 7.5, 0.0, 10.0)
            persistence_points = min(10.0, len(active_days_20) * 2.0)
            overheat_penalty = 0.0
            if breadth_5 >= 0.20:
                overheat_penalty += min(10.0, breadth_5 * 25.0)
            if average_rate >= 10:
                overheat_penalty += min(10.0, (average_rate - 8.0) / 2.0)
            score = bounded(
                recency_points + activity_points + breadth_points
                + acceleration_points + momentum_points + value_points
                + persistence_points - overheat_penalty,
                0.0, 100.0,
            )

            if recent_5 and previous_15 == [] and older_40:
                phase = "재점화"
            elif (len(recent_5) >= 5 and breadth_5 >= 0.12) \
                    or average_rate >= 10:
                phase = "과열"
            elif recent_5 and not previous_15:
                phase = "초기"
            elif len(recent_5) >= 2 and acceleration >= 1.2:
                phase = "확산"
            elif not recent_5 and recent_20:
                phase = "소멸"
            elif not recent_20 and (average_rate > 1.0 or value_ratio >= 1.5):
                phase = "대기"
            else:
                phase = "관찰"

            reason_parts = []
            if recent_5:
                reason_parts.append(
                    f"5일 상한가 {len(recent_5)}건/"
                    f"{len(stocks_5)}종목")
            if acceleration >= 1.2:
                reason_parts.append(f"확산속도 {acceleration:.1f}배")
            if value_ratio >= 1.2:
                reason_parts.append(f"거래대금 {value_ratio:.1f}배")
            if average_rate > 0:
                reason_parts.append(f"당일 평균 {average_rate:+.1f}%")
            if not reason_parts:
                reason_parts.append("뚜렷한 초기 신호 없음")
            if overheat_penalty:
                reason_parts.append(f"과열감점 {overheat_penalty:.0f}")

            signal_scores[theme_id] = score
            signal_phases[theme_id] = phase
            signal_values.append((
                as_of_date, theme_id, source, phase, round(score, 2),
                len(theme_members), len(recent_5), len(recent_20),
                len(recent_60), len(stocks_20), len(active_days_20),
                None if days_since_last >= 999 else days_since_last,
                round(average_rate, 4), latest_value,
                round(value_ratio, 4), round(overheat_penalty, 2),
                " · ".join(reason_parts), now,
            ))

        connection.execute(
            """DELETE FROM theme_rotation_signals
               WHERE as_of_date=? AND source=?""",
            (as_of_date, source),
        )
        connection.executemany(
            """INSERT INTO theme_rotation_signals(
                   as_of_date, theme_id, source, phase, rotation_score,
                   member_count, events_5d, events_20d, events_60d,
                   stocks_20d, active_days_20d, days_since_last,
                   average_rate, trading_value, value_ratio,
                   overheat_penalty, reason_text, calculated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            signal_values,
        )

        stock_values = []
        for theme_id, theme_members in members.items():
            scored = []
            for code in theme_members:
                stock_events = events_by_stock.get((theme_id, code), [])
                recent_60 = [
                    event for event in stock_events
                    if event["trade_date"] >= cutoff_60
                ]
                recent_20 = [
                    event for event in recent_60
                    if event["trade_date"] >= cutoff_20
                ]
                recent_5 = [
                    event for event in recent_20
                    if event["trade_date"] >= cutoff_5
                ]
                last_date = max(
                    (event["trade_date"] for event in stock_events),
                    default="",
                )
                code_prices = prices.get(code, {})
                latest = code_prices.get(as_of_date, {})
                change_rate = float(latest.get("change_rate") or 0)
                trading_value = int(latest.get("trading_value") or 0)
                previous_values = [
                    int(code_prices.get(date, {}).get("trading_value") or 0)
                    for date in recent_price_dates[1:]
                    if code_prices.get(date)
                ]
                previous_average = (
                    sum(previous_values) / len(previous_values)
                    if previous_values else 0
                )
                stock_value_ratio = (
                    trading_value / previous_average
                    if previous_average else 0.0
                )
                stock_days_since = (
                    date_position.get(last_date, 999)
                    if last_date else 999
                )
                leader_score = bounded(
                    len(recent_5) * 25.0 + len(recent_20) * 7.0
                    + len(recent_60) * 1.5
                    + (max(0.0, 20.0 - stock_days_since * 3.0)
                       if stock_days_since < 999 else 0.0)
                    + bounded(change_rate, 0.0, 10.0)
                    + bounded((stock_value_ratio - 1.0) * 5.0, 0.0, 10.0),
                    0.0, 100.0,
                )
                follower_score = bounded(
                    signal_scores.get(theme_id, 0.0) * (
                        0.55 if not recent_20 else 0.35)
                    + bounded(change_rate, 0.0, 8.0) * 2.0
                    + bounded((stock_value_ratio - 1.0) * 10.0, 0.0, 25.0)
                    + (12.0 if not recent_20 and
                       signal_phases.get(theme_id) in
                       ("초기", "확산", "재점화") else 0.0)
                    + (8.0 if not recent_5 and recent_20 else 0.0),
                    0.0, 100.0,
                )
                scored.append({
                    "code": code,
                    "events_5": len(recent_5),
                    "events_20": len(recent_20),
                    "events_60": len(recent_60),
                    "last_date": last_date,
                    "change_rate": change_rate,
                    "trading_value": trading_value,
                    "value_ratio": stock_value_ratio,
                    "leader_score": leader_score,
                    "follower_score": follower_score,
                })
            leader_code = ""
            active_scored = [
                item for item in scored if item["events_20"] > 0
            ]
            if active_scored:
                leader_code = max(
                    active_scored,
                    key=lambda item: (
                        item["leader_score"], item["trading_value"]),
                )["code"]
            for item in scored:
                if item["code"] == leader_code:
                    role = "대장주"
                elif item["events_5"] > 0:
                    role = "선도주"
                elif item["events_20"] == 0 and (
                        item["change_rate"] > 0
                        or item["value_ratio"] >= 1.2):
                    role = "후발 후보"
                elif item["events_20"] == 0:
                    role = "미발동"
                else:
                    role = "재점화 관찰"
                stock_values.append((
                    as_of_date, theme_id, source, item["code"], role,
                    round(item["leader_score"], 2),
                    round(item["follower_score"], 2),
                    item["events_5"], item["events_20"],
                    item["events_60"], item["last_date"] or None,
                    round(item["change_rate"], 4),
                    item["trading_value"],
                    round(item["value_ratio"], 4), now,
                ))

        connection.execute(
            """DELETE FROM stock_leader_scores
               WHERE as_of_date=? AND source=?""",
            (as_of_date, source),
        )
        connection.executemany(
            """INSERT INTO stock_leader_scores(
                   as_of_date, theme_id, source, stock_code, role,
                   leader_score, follower_score, events_5d, events_20d,
                   events_60d, last_limit_date, change_rate, trading_value,
                   value_ratio, calculated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            stock_values,
        )
        return {
            "as_of_date": as_of_date,
            "source": source,
            "themes": len(signal_values),
            "stocks": len(stock_values),
        }


def rotation_signal_rows(as_of_date: str, source: str = "NAVER",
                         db_path: Path = DB_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT r.*, t.theme_name
                 FROM theme_rotation_signals r
                 JOIN themes t ON t.theme_id=r.theme_id
                WHERE r.as_of_date=? AND r.source=?
                  AND (r.rotation_score>0 OR r.events_20d>0)
                ORDER BY r.rotation_score DESC, r.events_5d DESC,
                         r.trading_value DESC""",
            (as_of_date, source.upper()),
        ).fetchall()
        return [dict(row) for row in rows]


def rotation_theme_daily_rows(theme_id: int, source: str, as_of_date: str,
                              trading_days: int = 120,
                              db_path: Path = DB_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    with closing(connect(db_path)) as connection:
        cutoff_row = connection.execute(
            """SELECT MIN(trade_date) FROM (
                   SELECT DISTINCT trade_date FROM daily_prices
                    WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?)""",
            (as_of_date, int(trading_days)),
        ).fetchone()
        cutoff_date = (cutoff_row[0] if cutoff_row else "") or "00000000"
        rows = connection.execute(
            """SELECT d.trade_date, d.limit_up_count, d.unique_stock_count,
                      d.trading_value, d.leader_stock_code,
                      COALESCE(s.stock_name, '') AS leader_stock_name,
                      COALESCE((
                          SELECT GROUP_CONCAT(stock_name, ', ')
                            FROM (
                                SELECT DISTINCT sx.stock_name AS stock_name
                                  FROM stock_themes stx
                                  JOIN limit_up_events ex
                                    ON ex.stock_code=stx.stock_code
                                  JOIN stocks sx
                                    ON sx.stock_code=ex.stock_code
                                 WHERE stx.theme_id=d.theme_id
                                   AND stx.source=d.source
                                   AND stx.valid_to IS NULL
                                   AND ex.trade_date=d.trade_date
                                 ORDER BY sx.stock_name
                            )
                      ), '') AS event_stocks
                 FROM theme_daily_stats d
                 LEFT JOIN stocks s ON s.stock_code=d.leader_stock_code
                WHERE d.theme_id=? AND d.source=?
                  AND d.trade_date BETWEEN ? AND ?
                ORDER BY d.trade_date DESC""",
            (int(theme_id), source.upper(), cutoff_date, as_of_date),
        ).fetchall()
        return [dict(row) for row in rows]


def rotation_stock_rows(as_of_date: str, theme_id: int,
                        source: str = "NAVER",
                        db_path: Path = DB_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT r.*, s.stock_name, s.market
                 FROM stock_leader_scores r
                 JOIN stocks s ON s.stock_code=r.stock_code
                WHERE r.as_of_date=? AND r.theme_id=? AND r.source=?
                ORDER BY CASE r.role WHEN '대장주' THEN 1
                                     WHEN '선도주' THEN 2
                                     WHEN '후발 후보' THEN 3
                                     WHEN '재점화 관찰' THEN 4 ELSE 5 END,
                         CASE WHEN r.role IN ('대장주', '선도주')
                              THEN r.leader_score ELSE r.follower_score END DESC,
                         r.trading_value DESC""",
            (as_of_date, int(theme_id), source.upper()),
        ).fetchall()
        return [dict(row) for row in rows]


def market_dashboard(db_path: Path = DB_PATH) -> dict:
    """최근 거래일 기준 시장 요약·테마·주도주·상한가·수급을 반환한다."""
    empty = {
        "trade_date": "", "markets": [], "themes": [],
        "leaders": [], "limit_ups": [], "flows": [],
        "indices": [], "market_flows": [], "external": [],
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
        indices = connection.execute(
            """WITH ranked AS (
                   SELECT trade_date, index_code, market, close_value,
                          trading_value, collected_at,
                          ROW_NUMBER() OVER (
                              PARTITION BY index_code
                              ORDER BY trade_date DESC
                          ) AS rn
                     FROM market_index_prices
                    WHERE trade_date<=?
               )
               SELECT index_code, market,
                      MAX(CASE WHEN rn=1 THEN trade_date END) AS trade_date,
                      MAX(CASE WHEN rn=1 THEN close_value END) AS close_value,
                      MAX(CASE WHEN rn=2 THEN close_value END) AS previous_close,
                      MAX(CASE WHEN rn=1 THEN trading_value END)
                          AS trading_value,
                      MAX(CASE WHEN rn=1 THEN collected_at END) AS collected_at
                 FROM ranked
                WHERE rn<=2
                GROUP BY index_code, market
                ORDER BY index_code""",
            (trade_date,),
        ).fetchall()
        index_rows = []
        for row in indices:
            item = dict(row)
            close_value = float(item["close_value"] or 0)
            previous_close = float(item["previous_close"] or 0)
            item["change_rate"] = (
                round((close_value - previous_close) * 100 / previous_close, 2)
                if previous_close else None
            )
            index_rows.append(item)
        market_flows = connection.execute(
            """WITH aggregate_flows AS (
                   SELECT trade_date, market, industry_name,
                          foreign_net_amount_million AS foreign_net,
                          institution_net_amount_million AS institution_net,
                          individual_net_amount_million AS individual_net,
                          national_net_amount_million AS national_net,
                          collected_at
                     FROM market_investor_flows
                    WHERE trade_date<=?
                      AND ((market='KOSPI' AND industry_code='001')
                        OR (market='KOSDAQ' AND industry_code='101'))
               ),
               ranked AS (
                   SELECT *,
                          ROW_NUMBER() OVER (
                              PARTITION BY market ORDER BY trade_date DESC
                          ) AS rn
                     FROM aggregate_flows
               )
               SELECT market,
                      MAX(CASE WHEN rn=1 THEN trade_date END) AS trade_date,
                      MAX(CASE WHEN rn=1 THEN industry_name END)
                          AS industry_name,
                      MAX(CASE WHEN rn=1 THEN foreign_net END) AS foreign_net,
                      MAX(CASE WHEN rn=1 THEN institution_net END)
                          AS institution_net,
                      MAX(CASE WHEN rn=1 THEN individual_net END)
                          AS individual_net,
                      MAX(CASE WHEN rn=1 THEN national_net END)
                          AS national_net,
                      SUM(CASE WHEN rn<=5 THEN foreign_net ELSE 0 END)
                          AS foreign_5d,
                      SUM(CASE WHEN rn<=20 THEN foreign_net ELSE 0 END)
                          AS foreign_20d,
                      MAX(CASE WHEN rn=1 THEN collected_at END) AS collected_at
                 FROM ranked
                WHERE rn<=20
                GROUP BY market
                ORDER BY market""",
            (trade_date,),
        ).fetchall()
        return {
            "trade_date": trade_date,
            "flow_date": flow_date or "",
            "markets": [dict(row) for row in markets],
            "themes": [dict(row) for row in themes],
            "leaders": [dict(row) for row in leaders],
            "limit_ups": [dict(row) for row in limit_ups],
            "flows": [dict(row) for row in flows],
            "indices": index_rows,
            "market_flows": [dict(row) for row in market_flows],
            "external": latest_external_market_quotes(db_path),
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


def resolve_analysis_stock(query: str,
                           db_path: Path = DB_PATH) -> dict | None:
    """종목코드 또는 종목명으로 감시목록에 추가할 한 종목을 찾는다."""
    query = str(query or "").strip()
    if not query or not db_path.exists():
        return None
    with closing(connect(db_path)) as connection:
        row = connection.execute(
            """SELECT stock_code, stock_name, market, stock_type, dart_corp_code
               FROM stocks
               WHERE stock_code=? OR stock_name=?
               ORDER BY CASE WHEN stock_code=? THEN 0 ELSE 1 END
               LIMIT 1""",
            (query, query, query),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """SELECT stock_code, stock_name, market, stock_type, dart_corp_code
                   FROM stocks
                   WHERE stock_code LIKE ? OR stock_name LIKE ?
                   ORDER BY CASE
                       WHEN stock_code LIKE ? THEN 0
                       WHEN stock_name LIKE ? THEN 1 ELSE 2 END,
                       stock_name
                   LIMIT 1""",
                (f"{query}%", f"%{query}%", f"{query}%", f"{query}%"),
            ).fetchone()
        return dict(row) if row else None


def realtime_watch_codes(db_path: Path = DB_PATH) -> set[str]:
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        return {
            str(row[0]) for row in connection.execute(
                "SELECT stock_code FROM realtime_watchlist"
            ).fetchall()
        }


def set_realtime_watch(stock_code: str, enabled: bool,
                       source_context: str = "MANUAL",
                       stock_name: str = "",
                       db_path: Path = DB_PATH) -> bool:
    """영구 실시간 감시 종목을 추가하거나 제거한다."""
    initialize(db_path)
    stock_code = str(stock_code or "").strip()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection, connection:
        if not enabled:
            connection.execute(
                "DELETE FROM realtime_watchlist WHERE stock_code=?",
                (stock_code,),
            )
            return False
        exists = connection.execute(
            "SELECT 1 FROM stocks WHERE stock_code=?", (stock_code,)
        ).fetchone()
        if not exists:
            # 조건검색에는 막 상장된 종목도 즉시 나타날 수 있다. 화면에서 받은
            # 종목명으로 최소 카탈로그 행을 만들어 감시를 막지 않고, 이후 전체
            # 종목목록 동기화가 시장·유형 등 상세 정보를 채운다.
            stock_name = str(stock_name or "").strip()
            if not stock_name:
                raise ValueError(f"등록되지 않은 종목코드입니다: {stock_code}")
            connection.execute(
                """INSERT INTO stocks(stock_code, stock_name, updated_at)
                   VALUES (?, ?, ?)""",
                (stock_code, stock_name, now),
            )
        current_count = int(connection.execute(
            "SELECT COUNT(*) FROM realtime_watchlist"
        ).fetchone()[0])
        already = connection.execute(
            "SELECT 1 FROM realtime_watchlist WHERE stock_code=?",
            (stock_code,),
        ).fetchone()
        if current_count >= 80 and not already:
            raise ValueError("실시간 감시 종목은 최대 80개까지 등록할 수 있습니다.")
        connection.execute(
            """INSERT INTO realtime_watchlist(
                   stock_code, watch_scope, source_context, note,
                   added_at, updated_at)
               VALUES (?, 'ALWAYS', ?, '', ?, ?)
               ON CONFLICT(stock_code) DO UPDATE SET
                   source_context=excluded.source_context,
                   updated_at=excluded.updated_at""",
            (stock_code, str(source_context or "MANUAL"), now, now),
        )
        return True


def realtime_watch_rows(db_path: Path = DB_PATH) -> list[dict]:
    initialize(db_path)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT w.stock_code, s.stock_name, s.market, s.stock_type,
                      w.watch_scope, w.source_context, w.added_at,
                      w.updated_at, w.last_news_checked_at,
                      COUNT(m.news_id) AS news_count,
                      MAX(n.published_at_source) AS latest_news_at
               FROM realtime_watchlist w
               JOIN stocks s ON s.stock_code=w.stock_code
               LEFT JOIN news_stock_maps m ON m.stock_code=w.stock_code
               LEFT JOIN news_items n ON n.news_id=m.news_id
                    AND (
                        m.match_method<>'QUERY_STOCK'
                        OR INSTR(
                            LOWER(n.current_title || ' ' ||
                                  n.current_summary),
                            LOWER(m.matched_text)
                        )>0
                    )
               GROUP BY w.stock_code
               ORDER BY w.added_at ASC, s.stock_name""",
        ).fetchall()
        return [dict(row) for row in rows]


def _market_session(published_at: str) -> str:
    try:
        value = datetime.fromisoformat(str(published_at or ""))
        hour_minute = value.hour * 60 + value.minute
    except ValueError:
        return ""
    if hour_minute < 9 * 60:
        return "장전"
    if hour_minute <= 15 * 60 + 30:
        return "장중"
    return "장후"


def save_news_items(stock_code: str, stock_name: str, rows: list[dict],
                    db_path: Path = DB_PATH) -> dict:
    """공식 뉴스 검색 결과와 변경 버전·종목매핑·재료분류를 저장한다."""
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    result = {
        "received": len(rows), "new": 0, "updated": 0,
        "new_ids": [],
    }
    with closing(connect(db_path)) as connection, connection:
        source_row = connection.execute(
            """SELECT source_id FROM content_sources
               WHERE source_code='NAVER_SEARCH_NEWS'"""
        ).fetchone()
        if source_row is None:
            raise RuntimeError("네이버 뉴스 원천 정보가 없습니다.")
        source_id = int(source_row["source_id"])
        for item in rows:
            source_key = str(item.get("source_item_key") or "")
            if not source_key:
                continue
            existing = connection.execute(
                """SELECT news_id, current_content_hash, modified_count
                   FROM news_items
                   WHERE source_id=? AND source_item_key=?""",
                (source_id, source_key),
            ).fetchone()
            published = str(item.get("published_at_source") or "")
            values = (
                str(item.get("canonical_url") or ""),
                str(item.get("original_url") or ""),
                str(item.get("naver_url") or ""),
                str(item.get("publisher") or ""),
                published or None,
                now,
                _market_session(published),
                str(item.get("title") or ""),
                str(item.get("summary") or ""),
                str(item.get("current_hash") or ""),
                str(item.get("duplicate_key") or ""),
            )
            if existing is None:
                cursor = connection.execute(
                    """INSERT INTO news_items(
                           source_id, source_item_key, canonical_url,
                           original_url, naver_url, publisher,
                           published_at_source, first_collected_at,
                           last_seen_at, market_session, current_title,
                           current_summary, current_content_hash,
                           duplicate_cluster_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_id, source_key, *values[:6], now,
                        *values[6:],
                    ),
                )
                news_id = int(cursor.lastrowid)
                connection.execute(
                    """INSERT INTO news_item_versions(
                           news_id, version_no, observed_at, title, summary,
                           content_hash, change_type)
                       VALUES (?, 1, ?, ?, ?, ?, 'CREATED')""",
                    (
                        news_id, now, item.get("title") or "",
                        item.get("summary") or "",
                        item.get("current_hash") or "",
                    ),
                )
                result["new"] += 1
                result["new_ids"].append(news_id)
            else:
                news_id = int(existing["news_id"])
                changed = (
                    str(existing["current_content_hash"] or "")
                    != str(item.get("current_hash") or ""))
                connection.execute(
                    """UPDATE news_items SET
                           canonical_url=?, original_url=?, naver_url=?,
                           publisher=?, published_at_source=?,
                           last_seen_at=?, market_session=?, current_title=?,
                           current_summary=?, current_content_hash=?,
                           duplicate_cluster_key=?, removal_status='ACTIVE',
                           missing_since=NULL, removed_at=NULL,
                           modified_count=modified_count+?
                       WHERE news_id=?""",
                    (*values, int(changed), news_id),
                )
                if changed:
                    version_no = int(connection.execute(
                        """SELECT COALESCE(MAX(version_no), 0)+1
                           FROM news_item_versions WHERE news_id=?""",
                        (news_id,),
                    ).fetchone()[0])
                    connection.execute(
                        """INSERT INTO news_item_versions(
                               news_id, version_no, observed_at, title,
                               summary, content_hash, change_type)
                           VALUES (?, ?, ?, ?, ?, ?, 'EDITED')""",
                        (
                            news_id, version_no, now,
                            item.get("title") or "",
                            item.get("summary") or "",
                            item.get("current_hash") or "",
                        ),
                    )
                    result["updated"] += 1
            text = f"{item.get('title') or ''} {item.get('summary') or ''}"
            confidence = 0.9 if stock_name and stock_name in text else 0.5
            connection.execute(
                """INSERT INTO news_stock_maps(
                       news_id, stock_code, match_method, matched_text,
                       confidence, is_primary, mapped_at)
                   VALUES (?, ?, 'QUERY_STOCK', ?, ?, 1, ?)
                   ON CONFLICT(news_id, stock_code) DO UPDATE SET
                       matched_text=excluded.matched_text,
                       confidence=MAX(news_stock_maps.confidence,
                                      excluded.confidence),
                       mapped_at=excluded.mapped_at""",
                (news_id, stock_code, stock_name, confidence, now),
            )
            connection.execute(
                """INSERT INTO news_material_labels(
                       news_id, material_type, confidence,
                       classifier_version, classified_at)
                   VALUES (?, ?, ?, 'rules_v1', ?)
                   ON CONFLICT(news_id, material_type, classifier_version)
                   DO UPDATE SET confidence=excluded.confidence,
                                 classified_at=excluded.classified_at""",
                (
                    news_id, item.get("material_type") or "기타",
                    float(item.get("material_confidence") or 0), now,
                ),
            )
        connection.execute(
            """UPDATE realtime_watchlist
               SET last_news_checked_at=?, updated_at=updated_at
               WHERE stock_code=?""",
            (now, stock_code),
        )
    return result


def reconcile_news_search_results(
        stock_code: str, rows: list[dict],
        db_path: Path = DB_PATH) -> dict:
    """이번 공식 검색 범위 안에서 이전 기사가 사라졌는지 추적한다.

    검색 API 상위 100건 밖으로 밀린 기사를 오판하지 않도록 이번 응답의
    가장 오래된 게시시각 이후 기사만 비교한다. 검색에서 빠진 상태는 실제
    원문 삭제가 확정된 것이 아니므로 MISSING으로 보존한다.
    """
    if not rows:
        return {"active": 0, "missing": 0}
    returned_keys = {
        str(row.get("source_item_key") or "")
        for row in rows if row.get("source_item_key")
    }
    published_values = sorted(
        str(row.get("published_at_source") or "")
        for row in rows if row.get("published_at_source")
    )
    if not returned_keys or not published_values:
        return {"active": 0, "missing": 0}
    oldest_published = published_values[0]
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    initialize(db_path)
    with closing(connect(db_path)) as connection, connection:
        source_row = connection.execute(
            """SELECT source_id FROM content_sources
               WHERE source_code='NAVER_SEARCH_NEWS'"""
        ).fetchone()
        if source_row is None:
            return {"active": 0, "missing": 0}
        candidates = connection.execute(
            """SELECT DISTINCT n.news_id, n.source_item_key
                 FROM news_items n
                 JOIN news_stock_maps m ON m.news_id=n.news_id
                WHERE n.source_id=? AND m.stock_code=?
                  AND n.published_at_source>=?
                  AND (
                      SELECT COUNT(*) FROM news_stock_maps all_maps
                      WHERE all_maps.news_id=n.news_id
                  )=1""",
            (int(source_row["source_id"]), str(stock_code), oldest_published),
        ).fetchall()
        active_ids = [
            int(row["news_id"]) for row in candidates
            if str(row["source_item_key"]) in returned_keys
        ]
        missing_ids = [
            int(row["news_id"]) for row in candidates
            if str(row["source_item_key"]) not in returned_keys
        ]
        if active_ids:
            placeholders = ",".join("?" for _ in active_ids)
            connection.execute(
                f"""UPDATE news_items
                       SET removal_status='ACTIVE', missing_since=NULL,
                           removed_at=NULL
                     WHERE news_id IN ({placeholders})""",
                active_ids,
            )
        if missing_ids:
            placeholders = ",".join("?" for _ in missing_ids)
            connection.execute(
                f"""UPDATE news_items
                       SET removal_status='MISSING',
                           missing_since=COALESCE(missing_since, ?)
                     WHERE news_id IN ({placeholders})
                       AND removal_status<>'REMOVED'""",
                (now, *missing_ids),
            )
        return {
            "active": len(active_ids),
            "missing": len(missing_ids),
        }


def news_rows(stock_code: str = "", limit: int = 300,
              db_path: Path = DB_PATH) -> list[dict]:
    initialize(db_path)
    stock_code = str(stock_code or "").strip()
    relevance = """(
        m.match_method<>'QUERY_STOCK'
        OR INSTR(
            LOWER(n.current_title || ' ' || n.current_summary),
            LOWER(m.matched_text)
        )>0
    )"""
    where = (
        f"WHERE m.stock_code=? AND {relevance}"
        if stock_code else f"WHERE {relevance}"
    )
    params = (stock_code, int(limit)) if stock_code else (int(limit),)
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            f"""SELECT n.news_id, m.stock_code, s.stock_name,
                       n.published_at_source, n.first_collected_at,
                       n.last_seen_at, n.market_session, n.publisher,
                       n.current_title, n.current_summary, n.original_url,
                       n.naver_url, n.modified_count, n.removal_status,
                       COALESCE((
                           SELECT material_type
                           FROM news_material_labels l
                           WHERE l.news_id=n.news_id
                           ORDER BY l.confidence DESC LIMIT 1
                       ), '기타') AS material_type,
                       (
                           SELECT COUNT(*) FROM news_items d
                           WHERE d.duplicate_cluster_key=
                                 n.duplicate_cluster_key
                       ) AS duplicate_count,
                       m.confidence AS mapping_confidence
                FROM news_stock_maps m
                JOIN news_items n ON n.news_id=m.news_id
                JOIN stocks s ON s.stock_code=m.stock_code
                {where}
                ORDER BY n.published_at_source DESC, n.news_id DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def log_content_request(stock_code: str, query_text: str, http_status: int,
                        received_count: int, new_count: int,
                        elapsed_ms: int, error_text: str = "",
                        db_path: Path = DB_PATH):
    initialize(db_path)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(connect(db_path)) as connection, connection:
        source_id = connection.execute(
            """SELECT source_id FROM content_sources
               WHERE source_code='NAVER_SEARCH_NEWS'"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO content_request_logs(
                   source_id, requested_at, query_text, stock_code,
                   http_status, received_count, new_count, elapsed_ms,
                   error_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id, now, query_text, stock_code,
                int(http_status), int(received_count), int(new_count),
                int(elapsed_ms), str(error_text or ""),
            ),
        )


def news_request_count_today(db_path: Path = DB_PATH) -> int:
    initialize(db_path)
    today = datetime.now().astimezone().date().isoformat()
    with closing(connect(db_path)) as connection:
        return int(connection.execute(
            """SELECT COUNT(*) FROM content_request_logs l
               JOIN content_sources s ON s.source_id=l.source_id
               WHERE s.source_code='NAVER_SEARCH_NEWS'
                 AND SUBSTR(l.requested_at, 1, 10)=?""",
            (today,),
        ).fetchone()[0])

# -*- coding: utf-8 -*-
"""다음 거래일 상한가 확률의 시간순 기준 모델.

현재 테마를 과거에 소급하는 특징은 의도적으로 제외한다. 일봉 마감 시점까지
알 수 있었던 가격·거래량·시장 폭·실제 시장지수·시장 종합 수급만 사용하고,
다음 거래일 상한가 여부를 정답으로 삼는다. 뉴스·글로벌 지수는 후속 feature
version에서 추가한다.
"""
from __future__ import annotations

import json
import math
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)

from analysis_db import (
    ANALYSIS_STOCK_TYPES, DB_PATH, connect, initialize,
)


FEATURE_VERSION = "price_market_flow_v4"
MODEL_NAME = "next_limit_hist_gradient_boosting"
MODEL_DIR = Path(__file__).resolve().parent / "data" / "models"
FEATURE_NAMES = (
    "return_1d", "return_3d", "return_5d", "return_10d", "return_20d",
    "gap_rate", "intraday_rate", "day_range",
    "distance_high_20d", "distance_high_60d",
    "volume_ratio_5d", "volume_ratio_20d",
    "value_ratio_5d", "value_ratio_20d",
    "volatility_5d", "volatility_20d",
    "turnover_rate", "log_close", "log_trading_value",
    "limit_count_5d", "limit_count_20d", "limit_count_60d",
    "limit_today", "up_streak",
    "same_market_rise_ratio", "same_market_average_rate",
    "same_market_value_ratio_5d",
    "kospi_rise_ratio", "kosdaq_rise_ratio",
    "same_market_rise_ratio_5d", "same_market_rise_ratio_20d",
    "same_market_average_rate_5d", "same_market_average_rate_20d",
    "same_market_rate_volatility_5d",
    "same_market_rate_volatility_20d",
    "same_market_value_ratio_20d",
    "same_market_limit_count", "same_market_limit_average_5d",
    "same_market_limit_average_20d",
    "same_market_preferred_limit_ratio",
    "kospi_average_rate", "kosdaq_average_rate",
    "kospi_average_rate_5d", "kosdaq_average_rate_5d",
    "market_breadth_spread",
    "own_index_return_1d", "own_index_return_5d",
    "own_index_return_20d", "own_index_intraday_rate",
    "own_index_day_range", "own_index_volatility_5d",
    "own_index_volatility_20d", "own_index_value_ratio_20d",
    "kospi_index_return_1d", "kosdaq_index_return_1d",
    "kospi_index_return_5d", "kosdaq_index_return_5d",
    "index_return_spread_1d", "index_return_spread_5d",
    "own_foreign_flow_1d", "own_foreign_flow_5d",
    "own_foreign_flow_20d",
    "own_institution_flow_1d", "own_institution_flow_5d",
    "own_institution_flow_20d",
    "own_fund_flow_1d", "own_fund_flow_5d", "own_fund_flow_20d",
    "own_smart_flow_1d", "own_smart_flow_5d", "own_smart_flow_20d",
    "own_foreign_positive_days_5d", "own_smart_positive_days_5d",
    "is_kosdaq", "is_preferred", "is_spac",
)


@dataclass
class Dataset:
    x: np.ndarray
    y: np.ndarray
    weights: np.ndarray
    dates: list[str]
    codes: list[str]


class _Collector:
    def __init__(self, feature_count: int, initial_capacity: int = 4096):
        self._feature_count = feature_count
        self._capacity = max(1, initial_capacity)
        self._x = np.empty(
            (self._capacity, feature_count), dtype=np.float32)
        self._y = np.empty(self._capacity, dtype=np.uint8)
        self._weights = np.empty(self._capacity, dtype=np.float32)
        self.dates: list[str] = []
        self.codes: list[str] = []
        self.count = 0

    def append(self, features, label: int, weight: float,
               date: str, code: str):
        if self.count >= self._capacity:
            new_capacity = self._capacity * 2
            resized = np.empty(
                (new_capacity, self._feature_count), dtype=np.float32)
            resized[:self.count] = self._x[:self.count]
            self._x = resized
            labels = np.empty(new_capacity, dtype=np.uint8)
            labels[:self.count] = self._y[:self.count]
            self._y = labels
            weights = np.empty(new_capacity, dtype=np.float32)
            weights[:self.count] = self._weights[:self.count]
            self._weights = weights
            self._capacity = new_capacity
        self._x[self.count] = features
        self._y[self.count] = label
        self._weights[self.count] = weight
        self.dates.append(date)
        self.codes.append(code)
        self.count += 1

    def dataset(self) -> Dataset:
        return Dataset(
            self._x[:self.count].copy(),
            self._y[:self.count].copy(),
            self._weights[:self.count].copy(),
            self.dates,
            self.codes,
        )


def _safe_ratio(value: float, base: float, default: float = 0.0) -> float:
    return value / base if base else default


def _bounded(value: float, low: float = -20.0,
             high: float = 20.0) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(low, min(high, value))


def _market_context(connection) -> dict[tuple[str, str], dict]:
    rows = connection.execute(
        """SELECT p.trade_date, s.market,
                  COUNT(*) AS stock_count,
                  SUM(CASE WHEN p.change_rate>0 THEN 1 ELSE 0 END)
                      AS rising_count,
                  SUM(CASE WHEN p.change_rate<0 THEN 1 ELSE 0 END)
                      AS falling_count,
                  1.0 * SUM(CASE WHEN p.change_rate>0 THEN 1 ELSE 0 END)
                      / COUNT(*) AS rise_ratio,
                  AVG(COALESCE(p.change_rate, 0)) AS average_rate,
                  SUM(COALESCE(p.trading_value, 0)) AS trading_value,
                  COUNT(e.stock_code) AS limit_up_count,
                  SUM(CASE WHEN e.stock_code IS NOT NULL
                                AND s.stock_type='PREFERRED'
                           THEN 1 ELSE 0 END) AS preferred_limit_up_count
             FROM daily_prices p
             JOIN stocks s ON s.stock_code=p.stock_code
             LEFT JOIN limit_up_events e
               ON e.trade_date=p.trade_date AND e.stock_code=p.stock_code
            WHERE s.stock_type IN (?, ?, ?, ?, ?, ?)
              AND s.market IN ('KOSPI', 'KOSDAQ')
            GROUP BY p.trade_date, s.market
            ORDER BY s.market, p.trade_date""",
        ANALYSIS_STOCK_TYPES,
    ).fetchall()
    by_market: dict[str, list[dict]] = {"KOSPI": [], "KOSDAQ": []}
    for row in rows:
        by_market[row["market"]].append(dict(row))
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    connection.executemany(
        """INSERT INTO market_daily_features(
               trade_date, market, stock_count, rising_count, falling_count,
               rise_ratio, average_rate, trading_value, limit_up_count,
               preferred_limit_up_count, calculated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(trade_date, market) DO UPDATE SET
               stock_count=excluded.stock_count,
               rising_count=excluded.rising_count,
               falling_count=excluded.falling_count,
               rise_ratio=excluded.rise_ratio,
               average_rate=excluded.average_rate,
               trading_value=excluded.trading_value,
               limit_up_count=excluded.limit_up_count,
               preferred_limit_up_count=excluded.preferred_limit_up_count,
               calculated_at=excluded.calculated_at""",
        ((
            row["trade_date"], row["market"],
            int(row["stock_count"] or 0),
            int(row["rising_count"] or 0),
            int(row["falling_count"] or 0),
            float(row["rise_ratio"] or 0),
            float(row["average_rate"] or 0),
            int(row["trading_value"] or 0),
            int(row["limit_up_count"] or 0),
            int(row["preferred_limit_up_count"] or 0),
            now,
        ) for row in rows),
    )
    result: dict[tuple[str, str], dict] = {}
    for market, market_rows in by_market.items():
        for index, row in enumerate(market_rows):
            previous_5 = market_rows[max(0, index - 5):index]
            previous_20 = market_rows[max(0, index - 20):index]
            history_5 = market_rows[max(0, index - 4):index + 1]
            history_20 = market_rows[max(0, index - 19):index + 1]
            previous_average_5 = (
                sum(float(item["trading_value"] or 0)
                    for item in previous_5) / len(previous_5)
                if previous_5 else 0
            )
            previous_average_20 = (
                sum(float(item["trading_value"] or 0)
                    for item in previous_20) / len(previous_20)
                if previous_20 else 0
            )
            rates_5 = [
                float(item["average_rate"] or 0) for item in history_5]
            rates_20 = [
                float(item["average_rate"] or 0) for item in history_20]
            limit_count = int(row["limit_up_count"] or 0)
            result[(row["trade_date"], market)] = {
                "rise_ratio": float(row["rise_ratio"] or 0),
                "average_rate": float(row["average_rate"] or 0),
                "value_ratio_5": _safe_ratio(
                    float(row["trading_value"] or 0), previous_average_5),
                "value_ratio_20": _safe_ratio(
                    float(row["trading_value"] or 0), previous_average_20),
                "rise_ratio_5": float(np.mean([
                    float(item["rise_ratio"] or 0)
                    for item in history_5])),
                "rise_ratio_20": float(np.mean([
                    float(item["rise_ratio"] or 0)
                    for item in history_20])),
                "average_rate_5": float(np.mean(rates_5)),
                "average_rate_20": float(np.mean(rates_20)),
                "rate_volatility_5": float(np.std(rates_5)),
                "rate_volatility_20": float(np.std(rates_20)),
                "limit_count": limit_count,
                "limit_average_5": float(np.mean([
                    int(item["limit_up_count"] or 0)
                    for item in history_5])),
                "limit_average_20": float(np.mean([
                    int(item["limit_up_count"] or 0)
                    for item in history_20])),
                "preferred_limit_ratio": _safe_ratio(
                    int(row["preferred_limit_up_count"] or 0),
                    limit_count),
            }
    return result


def _index_context(connection) -> dict[tuple[str, str], dict]:
    """실제 코스피·코스닥 지수 일봉에서 당일 사용 가능한 특징을 만든다."""
    rows = connection.execute(
        """SELECT trade_date, market, open_value, high_value, low_value,
                  close_value, trading_value
             FROM market_index_prices
            WHERE index_code IN ('001', '101')
              AND market IN ('KOSPI', 'KOSDAQ')
            ORDER BY market, trade_date"""
    ).fetchall()
    by_market: dict[str, list[dict]] = {"KOSPI": [], "KOSDAQ": []}
    for row in rows:
        by_market[row["market"]].append(dict(row))
    result: dict[tuple[str, str], dict] = {}
    for market, market_rows in by_market.items():
        closes = np.asarray([
            float(row["close_value"] or 0) for row in market_rows
        ], dtype=np.float64)
        values = np.asarray([
            float(row["trading_value"] or 0) for row in market_rows
        ], dtype=np.float64)
        returns = np.zeros(len(market_rows), dtype=np.float64)
        if len(closes) > 1:
            valid = closes[:-1] != 0
            ratios = np.ones(len(closes) - 1, dtype=np.float64)
            np.divide(
                closes[1:], closes[:-1], out=ratios, where=valid)
            returns[1:] = (ratios - 1.0) * 100.0
        for index, row in enumerate(market_rows):
            close = closes[index]

            def period_return(days: int) -> float:
                if index < days:
                    return 0.0
                return _bounded(
                    (_safe_ratio(close, closes[index - days], 1.0) - 1.0)
                    * 100.0
                )

            history_5 = returns[max(0, index - 4):index + 1]
            history_20 = returns[max(0, index - 19):index + 1]
            previous_values = values[max(0, index - 20):index]
            average_value_20 = (
                float(np.mean(previous_values))
                if len(previous_values) else 0.0
            )
            open_value = float(row["open_value"] or 0)
            high_value = float(row["high_value"] or 0)
            low_value = float(row["low_value"] or 0)
            result[(row["trade_date"], market)] = {
                "return_1": float(returns[index]),
                "return_5": period_return(5),
                "return_20": period_return(20),
                "intraday_rate": _bounded(
                    (_safe_ratio(close, open_value, 1.0) - 1.0) * 100.0),
                "day_range": _bounded(
                    (_safe_ratio(high_value, low_value, 1.0) - 1.0)
                    * 100.0),
                "volatility_5": float(np.std(history_5)),
                "volatility_20": float(np.std(history_20)),
                "value_ratio_20": _safe_ratio(
                    values[index], average_value_20),
            }
    return result


def _flow_context(connection) -> dict[tuple[str, str], dict]:
    """시장 종합 투자자 순매수를 동기간 거래대금 대비 비율로 정규화한다."""
    rows = connection.execute(
        """SELECT f.trade_date, f.market,
                  f.foreign_net_amount_million AS foreign_net,
                  f.institution_net_amount_million AS institution_net,
                  f.fund_net_amount_million AS fund_net,
                  m.trading_value
             FROM market_investor_flows f
             JOIN market_daily_features m
               ON m.trade_date=f.trade_date AND m.market=f.market
            WHERE (f.market='KOSPI' AND f.industry_code='001')
               OR (f.market='KOSDAQ' AND f.industry_code='101')
            ORDER BY f.market, f.trade_date"""
    ).fetchall()
    by_market: dict[str, list[dict]] = {"KOSPI": [], "KOSDAQ": []}
    for row in rows:
        by_market[row["market"]].append(dict(row))
    result: dict[tuple[str, str], dict] = {}
    for market, market_rows in by_market.items():
        foreign = np.asarray([
            float(row["foreign_net"] or 0) * 1_000_000
            for row in market_rows
        ], dtype=np.float64)
        institution = np.asarray([
            float(row["institution_net"] or 0) * 1_000_000
            for row in market_rows
        ], dtype=np.float64)
        fund = np.asarray([
            float(row["fund_net"] or 0) * 1_000_000
            for row in market_rows
        ], dtype=np.float64)
        trading_values = np.asarray([
            float(row["trading_value"] or 0)
            for row in market_rows
        ], dtype=np.float64)
        smart = foreign + institution

        def ratio(values: np.ndarray, start: int, end: int) -> float:
            denominator = float(np.sum(trading_values[start:end]))
            return _bounded(
                _safe_ratio(float(np.sum(values[start:end])),
                            denominator) * 100.0,
                -30.0, 30.0,
            )

        for index, row in enumerate(market_rows):
            start_5 = max(0, index - 4)
            start_20 = max(0, index - 19)
            result[(row["trade_date"], market)] = {
                "foreign_1": ratio(foreign, index, index + 1),
                "foreign_5": ratio(foreign, start_5, index + 1),
                "foreign_20": ratio(foreign, start_20, index + 1),
                "institution_1": ratio(institution, index, index + 1),
                "institution_5": ratio(
                    institution, start_5, index + 1),
                "institution_20": ratio(
                    institution, start_20, index + 1),
                "fund_1": ratio(fund, index, index + 1),
                "fund_5": ratio(fund, start_5, index + 1),
                "fund_20": ratio(fund, start_20, index + 1),
                "smart_1": ratio(smart, index, index + 1),
                "smart_5": ratio(smart, start_5, index + 1),
                "smart_20": ratio(smart, start_20, index + 1),
                "foreign_positive_days_5": float(np.sum(
                    foreign[start_5:index + 1] > 0)),
                "smart_positive_days_5": float(np.sum(
                    smart[start_5:index + 1] > 0)),
            }
    return result


def _feature_row(rows: list[dict], index: int, limit_flags: np.ndarray,
                 market_context: dict, index_context: dict, flow_context: dict,
                 series: dict[str, np.ndarray]
                 ) -> np.ndarray:
    row = rows[index]
    closes = series["closes"]
    highs = series["highs"]
    volumes = series["volumes"]
    values = series["values"]
    rates = series["rates"]
    close = closes[index]

    def period_return(days: int) -> float:
        return _bounded(
            (_safe_ratio(close, closes[index - days], 1.0) - 1.0) * 100)

    previous_5_vol = float(np.mean(volumes[index - 5:index]))
    previous_20_vol = float(np.mean(volumes[index - 20:index]))
    previous_5_value = float(np.mean(values[index - 5:index]))
    previous_20_value = float(np.mean(values[index - 20:index]))
    open_price = float(row["open_price"] or 0)
    high_price = float(row["high_price"] or 0)
    low_price = float(row["low_price"] or 0)
    prev_close = float(row["prev_close"] or closes[index - 1] or 0)
    shares = float(row["shares_outstanding"] or 0)
    same_market = market_context.get(
        (row["trade_date"], row["market"]), {})
    kospi = market_context.get((row["trade_date"], "KOSPI"), {})
    kosdaq = market_context.get((row["trade_date"], "KOSDAQ"), {})
    own_index = index_context.get(
        (row["trade_date"], row["market"]), {})
    kospi_index = index_context.get(
        (row["trade_date"], "KOSPI"), {})
    kosdaq_index = index_context.get(
        (row["trade_date"], "KOSDAQ"), {})
    own_flow = flow_context.get(
        (row["trade_date"], row["market"]), {})

    up_streak = 0
    for rate in rates[:index + 1][::-1]:
        if rate > 0:
            up_streak += 1
        else:
            break

    features = (
        float(row["change_rate"] or 0),
        period_return(3), period_return(5),
        period_return(10), period_return(20),
        _bounded((_safe_ratio(open_price, prev_close, 1.0) - 1.0) * 100),
        _bounded((_safe_ratio(close, open_price, 1.0) - 1.0) * 100),
        _bounded((_safe_ratio(high_price, low_price, 1.0) - 1.0) * 100),
        _bounded((_safe_ratio(
            close, float(np.max(highs[index - 19:index + 1])),
            1.0) - 1.0) * 100),
        _bounded((_safe_ratio(
            close, float(np.max(highs[index - 59:index + 1])),
            1.0) - 1.0) * 100),
        _bounded(_safe_ratio(volumes[index], previous_5_vol), 0, 30),
        _bounded(_safe_ratio(volumes[index], previous_20_vol), 0, 30),
        _bounded(_safe_ratio(values[index], previous_5_value), 0, 30),
        _bounded(_safe_ratio(values[index], previous_20_value), 0, 30),
        _bounded(float(np.std(rates[index - 4:index + 1])), 0, 20),
        _bounded(float(np.std(rates[index - 19:index + 1])), 0, 20),
        _bounded(_safe_ratio(volumes[index], shares) * 100, 0, 100),
        math.log1p(max(0.0, close)),
        math.log1p(max(0.0, values[index])),
        float(np.sum(limit_flags[index - 4:index + 1])),
        float(np.sum(limit_flags[index - 19:index + 1])),
        float(np.sum(limit_flags[index - 59:index + 1])),
        float(limit_flags[index]),
        float(min(up_streak, 20)),
        float(same_market.get("rise_ratio", 0)),
        float(same_market.get("average_rate", 0)),
        _bounded(float(same_market.get("value_ratio_5", 0)), 0, 10),
        float(kospi.get("rise_ratio", 0)),
        float(kosdaq.get("rise_ratio", 0)),
        float(same_market.get("rise_ratio_5", 0)),
        float(same_market.get("rise_ratio_20", 0)),
        float(same_market.get("average_rate_5", 0)),
        float(same_market.get("average_rate_20", 0)),
        _bounded(float(same_market.get("rate_volatility_5", 0)), 0, 20),
        _bounded(float(same_market.get("rate_volatility_20", 0)), 0, 20),
        _bounded(float(same_market.get("value_ratio_20", 0)), 0, 10),
        float(same_market.get("limit_count", 0)),
        float(same_market.get("limit_average_5", 0)),
        float(same_market.get("limit_average_20", 0)),
        float(same_market.get("preferred_limit_ratio", 0)),
        float(kospi.get("average_rate", 0)),
        float(kosdaq.get("average_rate", 0)),
        float(kospi.get("average_rate_5", 0)),
        float(kosdaq.get("average_rate_5", 0)),
        float(kosdaq.get("rise_ratio", 0))
            - float(kospi.get("rise_ratio", 0)),
        float(own_index.get("return_1", 0)),
        float(own_index.get("return_5", 0)),
        float(own_index.get("return_20", 0)),
        float(own_index.get("intraday_rate", 0)),
        float(own_index.get("day_range", 0)),
        _bounded(float(own_index.get("volatility_5", 0)), 0, 20),
        _bounded(float(own_index.get("volatility_20", 0)), 0, 20),
        _bounded(float(own_index.get("value_ratio_20", 0)), 0, 10),
        float(kospi_index.get("return_1", 0)),
        float(kosdaq_index.get("return_1", 0)),
        float(kospi_index.get("return_5", 0)),
        float(kosdaq_index.get("return_5", 0)),
        float(kosdaq_index.get("return_1", 0))
            - float(kospi_index.get("return_1", 0)),
        float(kosdaq_index.get("return_5", 0))
            - float(kospi_index.get("return_5", 0)),
        float(own_flow.get("foreign_1", 0)),
        float(own_flow.get("foreign_5", 0)),
        float(own_flow.get("foreign_20", 0)),
        float(own_flow.get("institution_1", 0)),
        float(own_flow.get("institution_5", 0)),
        float(own_flow.get("institution_20", 0)),
        float(own_flow.get("fund_1", 0)),
        float(own_flow.get("fund_5", 0)),
        float(own_flow.get("fund_20", 0)),
        float(own_flow.get("smart_1", 0)),
        float(own_flow.get("smart_5", 0)),
        float(own_flow.get("smart_20", 0)),
        float(own_flow.get("foreign_positive_days_5", 0)),
        float(own_flow.get("smart_positive_days_5", 0)),
        float(row["market"] == "KOSDAQ"),
        float(row["stock_type"] == "PREFERRED"),
        float(row["stock_type"] == "SPAC"),
    )
    return np.asarray(features, dtype=np.float32)


def build_datasets(db_path: Path = DB_PATH, negative_sample_rate: float = 0.05,
                   calibration_days: int = 40,
                   test_days: int = 80) -> tuple[Dataset, Dataset, Dataset,
                                                  Dataset, dict]:
    """시간순 train/calibration/test와 최신 예측용 데이터를 한 번 생성한다."""
    initialize(db_path)
    with connect(db_path) as connection:
        trade_dates = [
            row[0] for row in connection.execute(
                "SELECT DISTINCT trade_date FROM daily_prices "
                "ORDER BY trade_date"
            ).fetchall()
        ]
        if len(trade_dates) < calibration_days + test_days + 120:
            raise RuntimeError("시간순 학습에 필요한 거래일 데이터가 부족합니다.")
        latest_date = trade_dates[-1]
        labeled_dates = trade_dates[:-1]
        test_date_list = labeled_dates[-test_days:]
        calibration_date_list = labeled_dates[
            -(test_days + calibration_days):-test_days]
        train_date_list = labeled_dates[
            :-(test_days + calibration_days)]
        train_to = train_date_list[-1]
        calibration_dates = set(calibration_date_list)
        test_dates = set(test_date_list)
        next_date = {
            date: trade_dates[index + 1]
            for index, date in enumerate(trade_dates[:-1])
        }
        limit_events = {
            (row["trade_date"], row["stock_code"])
            for row in connection.execute(
                "SELECT trade_date, stock_code FROM limit_up_events"
            ).fetchall()
        }
        market_context = _market_context(connection)
        index_context = _index_context(connection)
        flow_context = _flow_context(connection)
        missing_index_dates = [
            date for date in trade_dates
            if (date, "KOSPI") not in index_context
            or (date, "KOSDAQ") not in index_context
        ]
        if missing_index_dates:
            sample = ", ".join(missing_index_dates[:5])
            raise RuntimeError(
                "실제 코스피·코스닥 지수 데이터가 부족합니다. "
                f"누락 거래일 {len(missing_index_dates):,}개"
                + (f" ({sample})" if sample else "")
            )
        missing_flow_dates = [
            date for date in trade_dates
            if (date, "KOSPI") not in flow_context
            or (date, "KOSDAQ") not in flow_context
        ]
        if missing_flow_dates:
            sample = ", ".join(missing_flow_dates[:5])
            raise RuntimeError(
                "코스피·코스닥 시장 종합 수급 데이터가 부족합니다. "
                f"누락 거래일 {len(missing_flow_dates):,}개"
                + (f" ({sample})" if sample else "")
            )

        train_collector = _Collector(len(FEATURE_NAMES), 131072)
        calibration_collector = _Collector(len(FEATURE_NAMES), 131072)
        test_collector = _Collector(len(FEATURE_NAMES), 262144)
        latest_collector = _Collector(len(FEATURE_NAMES), 4096)

        query = """SELECT p.trade_date, p.stock_code, p.open_price,
                          p.high_price, p.low_price, p.close_price,
                          p.prev_close, p.volume, p.trading_value,
                          p.change_rate, s.market, s.stock_type,
                          s.shares_outstanding
                     FROM daily_prices p
                     JOIN stocks s ON s.stock_code=p.stock_code
                    WHERE s.stock_type IN (?, ?, ?, ?, ?, ?)
                      AND s.market IN ('KOSPI', 'KOSDAQ')
                    ORDER BY p.stock_code, p.trade_date"""
        cursor = connection.execute(query, ANALYSIS_STOCK_TYPES)
        current_code = ""
        stock_rows: list[dict] = []

        def process_stock(code: str, rows: list[dict]):
            if not code or len(rows) < 61:
                return
            row_dates = [row["trade_date"] for row in rows]
            date_to_index = {
                date: index for index, date in enumerate(row_dates)}
            limit_flags = np.asarray([
                1 if (row["trade_date"], code) in limit_events else 0
                for row in rows
            ], dtype=np.uint8)
            series = {
                "closes": np.asarray(
                    [float(row["close_price"] or 0) for row in rows],
                    dtype=np.float64),
                "highs": np.asarray(
                    [float(row["high_price"] or 0) for row in rows],
                    dtype=np.float64),
                "volumes": np.asarray(
                    [float(row["volume"] or 0) for row in rows],
                    dtype=np.float64),
                "values": np.asarray(
                    [float(row["trading_value"] or 0) for row in rows],
                    dtype=np.float64),
                "rates": np.asarray(
                    [float(row["change_rate"] or 0) for row in rows],
                    dtype=np.float64),
            }
            for index in range(59, len(rows)):
                date = rows[index]["trade_date"]
                features = _feature_row(
                    rows, index, limit_flags, market_context,
                    index_context, flow_context, series)
                if date == latest_date:
                    latest_collector.append(
                        features, 0, 1.0, date, code)
                target_date = next_date.get(date)
                if not target_date or target_date not in date_to_index:
                    continue
                label = int((target_date, code) in limit_events)
                if date <= train_to:
                    sample_key = zlib.crc32(
                        f"{date}:{code}".encode("ascii"))
                    take_negative = (
                        sample_key / 0xFFFFFFFF < negative_sample_rate)
                    if label or take_negative:
                        weight = (
                            1.0 if label else 1.0 / negative_sample_rate)
                        train_collector.append(
                            features, label, weight, date, code)
                elif date in calibration_dates:
                    calibration_collector.append(
                        features, label, 1.0, date, code)
                elif date in test_dates:
                    test_collector.append(
                        features, label, 1.0, date, code)

        for row in cursor:
            code = row["stock_code"]
            if current_code and code != current_code:
                process_stock(current_code, stock_rows)
                stock_rows = []
            current_code = code
            stock_rows.append(dict(row))
        process_stock(current_code, stock_rows)

    metadata = {
        "latest_date": latest_date,
        "train_from": train_date_list[59],
        "train_to": train_to,
        "calibration_from": calibration_date_list[0],
        "calibration_to": calibration_date_list[-1],
        "test_from": test_date_list[0],
        "test_to": test_date_list[-1],
        "negative_sample_rate": negative_sample_rate,
    }
    return (
        train_collector.dataset(),
        calibration_collector.dataset(),
        test_collector.dataset(),
        latest_collector.dataset(),
        metadata,
    )


def _new_estimator() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=0.06,
        max_iter=180,
        max_leaf_nodes=31,
        min_samples_leaf=40,
        l2_regularization=1.5,
        early_stopping=True,
        validation_fraction=0.1,
        random_state=42,
    )


def _top_k_metrics(dataset: Dataset, probabilities: np.ndarray,
                   sizes=(5, 10, 20)) -> dict:
    grouped: dict[str, list[int]] = {}
    for index, date in enumerate(dataset.dates):
        grouped.setdefault(date, []).append(index)
    metrics = {}
    total_positives = int(np.sum(dataset.y))
    for size in sizes:
        hits = 0
        day_hits = 0
        evaluated_days = 0
        for indices in grouped.values():
            if not any(dataset.y[index] for index in indices):
                continue
            evaluated_days += 1
            selected = sorted(
                indices, key=lambda index: probabilities[index],
                reverse=True)[:size]
            selected_hits = sum(int(dataset.y[index]) for index in selected)
            hits += selected_hits
            day_hits += int(selected_hits > 0)
        metrics[f"top_{size}_event_recall"] = (
            hits / total_positives if total_positives else 0)
        metrics[f"top_{size}_day_hit_rate"] = (
            day_hits / evaluated_days if evaluated_days else 0)
        metrics[f"top_{size}_precision"] = (
            hits / (evaluated_days * size) if evaluated_days else 0)
    return metrics


def _evaluate(dataset: Dataset, probabilities: np.ndarray) -> dict:
    result = {
        "prevalence": float(np.mean(dataset.y)),
        "roc_auc": float(roc_auc_score(dataset.y, probabilities)),
        "average_precision": float(
            average_precision_score(dataset.y, probabilities)),
        "brier_score": float(brier_score_loss(dataset.y, probabilities)),
        "log_loss": float(log_loss(dataset.y, probabilities)),
    }
    result.update(_top_k_metrics(dataset, probabilities))
    limit_today_index = FEATURE_NAMES.index("limit_today")
    for prefix, mask in (
            ("continuation", dataset.x[:, limit_today_index] >= 0.5),
            ("fresh", dataset.x[:, limit_today_index] < 0.5)):
        if int(np.sum(mask)) == 0 or len(np.unique(dataset.y[mask])) < 2:
            continue
        subset = Dataset(
            dataset.x[mask],
            dataset.y[mask],
            dataset.weights[mask],
            [date for date, take in zip(dataset.dates, mask) if take],
            [code for code, take in zip(dataset.codes, mask) if take],
        )
        subset_probabilities = probabilities[mask]
        result[f"{prefix}_samples"] = len(subset.y)
        result[f"{prefix}_positives"] = int(np.sum(subset.y))
        result[f"{prefix}_prevalence"] = float(np.mean(subset.y))
        result[f"{prefix}_roc_auc"] = float(
            roc_auc_score(subset.y, subset_probabilities))
        result[f"{prefix}_average_precision"] = float(
            average_precision_score(subset.y, subset_probabilities))
        for key, value in _top_k_metrics(
                subset, subset_probabilities, sizes=(3, 5, 10)).items():
            result[f"{prefix}_{key}"] = value
    return result


def _prediction_reason(features: np.ndarray) -> str:
    values = dict(zip(FEATURE_NAMES, features))
    reasons = []
    if values["limit_today"] >= 1:
        reasons.append("당일 상한가")
    if values["limit_count_20d"] >= 2:
        reasons.append(
            f"20일 상한가 {values['limit_count_20d']:.0f}회")
    if values["value_ratio_5d"] >= 1.5:
        reasons.append(
            f"거래대금 {values['value_ratio_5d']:.1f}배")
    if values["volume_ratio_5d"] >= 1.5:
        reasons.append(
            f"거래량 {values['volume_ratio_5d']:.1f}배")
    if values["return_20d"] >= 10:
        reasons.append(f"20일 수익률 {values['return_20d']:+.0f}%")
    if values["same_market_rise_ratio"] <= 0.3:
        reasons.append("시장 약세 속 상대강도")
    return " · ".join(reasons[:4]) or "가격·거래량 복합 신호"


def train_and_predict(db_path: Path = DB_PATH) -> dict:
    """워크포워드 홀드아웃 성과를 측정하고 최신 확률을 DB에 저장한다."""
    train, calibration, test, latest, metadata = build_datasets(db_path)
    if np.sum(train.y) < 100 or np.sum(test.y) < 20:
        raise RuntimeError("상한가 양성 표본이 부족합니다.")

    evaluation_base = _new_estimator()
    evaluation_base.fit(train.x, train.y, sample_weight=train.weights)
    evaluation_model = CalibratedClassifierCV(
        FrozenEstimator(evaluation_base), method="sigmoid")
    evaluation_model.fit(calibration.x, calibration.y)
    test_probabilities = evaluation_model.predict_proba(test.x)[:, 1]
    metrics = _evaluate(test, test_probabilities)

    production_train_x = np.vstack((train.x, calibration.x))
    production_train_y = np.concatenate((train.y, calibration.y))
    production_weights = np.concatenate(
        (train.weights, calibration.weights))
    production_base = _new_estimator()
    production_base.fit(
        production_train_x, production_train_y,
        sample_weight=production_weights)
    production_model = CalibratedClassifierCV(
        FrozenEstimator(production_base), method="sigmoid")
    production_model.fit(test.x, test.y)
    latest_probabilities = production_model.predict_proba(latest.x)[:, 1]

    ranked_indices = np.argsort(-latest_probabilities)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    initialize(db_path)
    with connect(db_path) as connection, connection:
        cursor = connection.execute(
            """INSERT INTO prediction_model_runs(
                   model_name, feature_version, train_from, train_to,
                   test_from, test_to, train_samples, train_positives,
                   test_samples, test_positives, metrics_json,
                   model_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)""",
            (
                MODEL_NAME, FEATURE_VERSION,
                metadata["train_from"], metadata["calibration_to"],
                metadata["test_from"], metadata["test_to"],
                len(production_train_y), int(np.sum(production_train_y)),
                len(test.y), int(np.sum(test.y)),
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        model_run_id = int(cursor.lastrowid)
        model_path = MODEL_DIR / (
            f"next_limit_{FEATURE_VERSION}_{model_run_id}.joblib")
        payload = {
            "model": production_model,
            "feature_names": FEATURE_NAMES,
            "feature_version": FEATURE_VERSION,
            "metadata": metadata,
            "metrics": metrics,
        }
        joblib.dump(payload, model_path, compress=3)
        connection.execute(
            """UPDATE prediction_model_runs SET model_path=?
               WHERE model_run_id=?""",
            (str(model_path), model_run_id),
        )
        prediction_rows = []
        for rank, index in enumerate(ranked_indices[:300], 1):
            prediction_rows.append((
                metadata["latest_date"], latest.codes[index], 1,
                model_run_id, float(latest_probabilities[index]), rank,
                FEATURE_VERSION, _prediction_reason(latest.x[index]), now,
            ))
        connection.executemany(
            """INSERT INTO stock_predictions(
                   as_of_date, stock_code, horizon_days, model_run_id,
                   probability, probability_rank, feature_version,
                   reason_text, calculated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            prediction_rows,
        )

    return {
        "model_run_id": model_run_id,
        "model_path": str(model_path),
        "metadata": metadata,
        "metrics": metrics,
        "train_samples": len(production_train_y),
        "train_positives": int(np.sum(production_train_y)),
        "test_samples": len(test.y),
        "test_positives": int(np.sum(test.y)),
        "latest_candidates": len(latest.y),
    }


if __name__ == "__main__":
    print(json.dumps(
        train_and_predict(), ensure_ascii=False, indent=2, sort_keys=True))

# -*- coding: utf-8 -*-
"""v1 대 v4 고정 설정 다구간 워크포워드 검증.

초기 기본학습 250거래일, 보정 40거래일을 확보한 뒤 남은 기간을 서로
겹치지 않는 60~80거래일 테스트 구간으로 나눈다. 각 구간은 해당 시점보다
과거인 데이터만 사용해 모델을 처음부터 다시 학습한다.

paired bootstrap은 같은 거래일의 일별 AP 또는 Top-K 적중 여부 차이를 한
쌍으로 묶어 재표본화한다. 구간 선택 뒤 특징이나 하이퍼파라미터를 변경하지
않는다.
"""
from __future__ import annotations

import json
import math
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import average_precision_score

from analysis_db import ANALYSIS_STOCK_TYPES, DB_PATH, connect, initialize
from prediction_model import (
    FEATURE_NAMES,
    _feature_row,
    _flow_context,
    _index_context,
    _market_context,
    _new_estimator,
)


INITIAL_TRAIN_DAYS = 250
CALIBRATION_DAYS = 40
TARGET_TEST_DAYS = 70
MIN_TEST_DAYS = 60
MAX_TEST_DAYS = 80
NEGATIVE_SAMPLE_RATE = 0.05
BOOTSTRAP_ITERATIONS = 2000
RANDOM_SEED = 42
REPORT_JSON = Path(__file__).resolve().parent / "data" / (
    "walkforward_v1_v4_report.json")
REPORT_MD = Path(__file__).resolve().parent / "data" / (
    "walkforward_v1_v4_report.md")

V1_FEATURE_NAMES = (
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
    "is_kosdaq", "is_preferred", "is_spac",
)
V4_FEATURE_NAMES = FEATURE_NAMES


@dataclass
class MatrixData:
    x: np.ndarray
    y: np.ndarray
    date_position: np.ndarray
    limit_today: np.ndarray
    weights: np.ndarray | None = None


@dataclass(frozen=True)
class Fold:
    fold: int
    train_end: int
    calibration_start: int
    test_start: int
    test_end: int


class _PreallocatedCollector:
    def __init__(self, capacity: int, feature_count: int,
                 with_weights: bool):
        self.x = np.empty(
            (max(1, capacity), feature_count), dtype=np.float32)
        self.y = np.empty(max(1, capacity), dtype=np.uint8)
        self.date_position = np.empty(max(1, capacity), dtype=np.int16)
        self.limit_today = np.empty(max(1, capacity), dtype=np.uint8)
        self.weights = (
            np.empty(max(1, capacity), dtype=np.float32)
            if with_weights else None
        )
        self.count = 0

    def append(self, features: np.ndarray, label: int, date_position: int,
               limit_today: int, weight: float = 1.0):
        if self.count >= len(self.y):
            raise MemoryError(
                f"워크포워드 배열 예상 용량 초과: {len(self.y):,}행")
        self.x[self.count] = features
        self.y[self.count] = label
        self.date_position[self.count] = date_position
        self.limit_today[self.count] = limit_today
        if self.weights is not None:
            self.weights[self.count] = weight
        self.count += 1

    def data(self) -> MatrixData:
        return MatrixData(
            self.x[:self.count],
            self.y[:self.count],
            self.date_position[:self.count],
            self.limit_today[:self.count],
            self.weights[:self.count] if self.weights is not None else None,
        )


def _folds(usable_dates: list[str]) -> list[Fold]:
    first_test = INITIAL_TRAIN_DAYS + CALIBRATION_DAYS
    remaining = len(usable_dates) - first_test
    if remaining < MIN_TEST_DAYS:
        raise RuntimeError("워크포워드 테스트 기간이 60거래일보다 짧습니다.")
    fold_count = max(1, remaining // TARGET_TEST_DAYS)
    while math.ceil(remaining / fold_count) > MAX_TEST_DAYS:
        fold_count += 1
    while fold_count > 1 and remaining // fold_count < MIN_TEST_DAYS:
        fold_count -= 1
    base, extra = divmod(remaining, fold_count)
    sizes = [base + (1 if index < extra else 0)
             for index in range(fold_count)]
    if not all(MIN_TEST_DAYS <= size <= MAX_TEST_DAYS for size in sizes):
        raise RuntimeError(f"허용 범위를 벗어난 테스트 구간: {sizes}")
    result = []
    start = first_test
    for index, size in enumerate(sizes, 1):
        result.append(Fold(
            fold=index,
            train_end=start - CALIBRATION_DAYS,
            calibration_start=start - CALIBRATION_DAYS,
            test_start=start,
            test_end=start + size,
        ))
        start += size
    return result


def _build_all_data(db_path: Path = DB_PATH
                    ) -> tuple[MatrixData, MatrixData, list[str], list[Fold]]:
    initialize(db_path)
    with connect(db_path) as connection:
        trade_dates = [
            row[0] for row in connection.execute(
                "SELECT DISTINCT trade_date FROM daily_prices "
                "ORDER BY trade_date"
            ).fetchall()
        ]
        if len(trade_dates) < 60 + INITIAL_TRAIN_DAYS + CALIBRATION_DAYS:
            raise RuntimeError("워크포워드 검증에 필요한 거래일이 부족합니다.")
        labeled_dates = trade_dates[:-1]
        usable_dates = labeled_dates[59:]
        folds = _folds(usable_dates)
        usable_position = {
            date: index for index, date in enumerate(usable_dates)}
        next_date = {
            date: trade_dates[index + 1]
            for index, date in enumerate(trade_dates[:-1])
        }
        full_from = usable_dates[INITIAL_TRAIN_DAYS]
        full_capacity = int(connection.execute(
            """SELECT COUNT(*) FROM daily_prices
                WHERE trade_date BETWEEN ? AND ?""",
            (full_from, usable_dates[-1]),
        ).fetchone()[0]) + 4096
        labeled_capacity = int(connection.execute(
            """SELECT COUNT(*) FROM daily_prices
                WHERE trade_date BETWEEN ? AND ?""",
            (usable_dates[0], usable_dates[-1]),
        ).fetchone()[0])
        sampled_capacity = int(
            labeled_capacity * (NEGATIVE_SAMPLE_RATE + 0.02)) + 16384

        market_context = _market_context(connection)
        index_context = _index_context(connection)
        flow_context = _flow_context(connection)
        for context_name, context in (
            ("시장지수", index_context), ("시장수급", flow_context),
        ):
            missing = [
                date for date in trade_dates
                if (date, "KOSPI") not in context
                or (date, "KOSDAQ") not in context
            ]
            if missing:
                raise RuntimeError(
                    f"{context_name} 누락 거래일 {len(missing):,}개: "
                    + ", ".join(missing[:5]))

        limit_events = {
            (row["trade_date"], row["stock_code"])
            for row in connection.execute(
                "SELECT trade_date, stock_code FROM limit_up_events"
            ).fetchall()
        }
        sampled_collector = _PreallocatedCollector(
            sampled_capacity, len(FEATURE_NAMES), True)
        full_collector = _PreallocatedCollector(
            full_capacity, len(FEATURE_NAMES), False)

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
            for row_index in range(59, len(rows)):
                date = rows[row_index]["trade_date"]
                date_position = usable_position.get(date)
                if date_position is None:
                    continue
                target_date = next_date.get(date)
                if not target_date or target_date not in date_to_index:
                    continue
                label = int((target_date, code) in limit_events)
                features = _feature_row(
                    rows, row_index, limit_flags, market_context,
                    index_context, flow_context, series)
                limit_today = int(limit_flags[row_index])
                sample_key = zlib.crc32(
                    f"{date}:{code}".encode("ascii"))
                take_negative = (
                    sample_key / 0xFFFFFFFF < NEGATIVE_SAMPLE_RATE)
                if label or take_negative:
                    sampled_collector.append(
                        features, label, date_position, limit_today,
                        1.0 if label else 1.0 / NEGATIVE_SAMPLE_RATE,
                    )
                if date_position >= INITIAL_TRAIN_DAYS:
                    full_collector.append(
                        features, label, date_position, limit_today)

        for row in cursor:
            code = row["stock_code"]
            if current_code and code != current_code:
                process_stock(current_code, stock_rows)
                stock_rows = []
            current_code = code
            stock_rows.append(dict(row))
        process_stock(current_code, stock_rows)

    return (
        sampled_collector.data(), full_collector.data(),
        usable_dates, folds,
    )


def _average_precision(y: np.ndarray, probabilities: np.ndarray) -> float:
    if len(y) == 0 or len(np.unique(y)) < 2:
        return float("nan")
    return float(average_precision_score(y, probabilities))


def _daily_ap_map(y: np.ndarray, probabilities: np.ndarray,
                  dates: np.ndarray, mask: np.ndarray) -> dict[int, float]:
    result = {}
    for date in np.unique(dates[mask]):
        date_mask = mask & (dates == date)
        date_y = y[date_mask]
        if len(date_y) and np.any(date_y) and len(np.unique(date_y)) == 2:
            result[int(date)] = _average_precision(
                date_y, probabilities[date_mask])
    return result


def _daily_top_hits(y: np.ndarray, probabilities: np.ndarray,
                    dates: np.ndarray, fresh_mask: np.ndarray,
                    size: int) -> dict[int, int]:
    result = {}
    for date in np.unique(dates[fresh_mask]):
        indices = np.flatnonzero(fresh_mask & (dates == date))
        if not len(indices) or not np.any(y[indices]):
            continue
        order = indices[np.argsort(-probabilities[indices], kind="stable")]
        result[int(date)] = int(np.any(y[order[:size]]))
    return result


def _metrics(y: np.ndarray, probabilities: np.ndarray,
             dates: np.ndarray, limit_today: np.ndarray) -> tuple[dict, dict]:
    all_mask = np.ones(len(y), dtype=bool)
    fresh_mask = limit_today == 0
    continuation_mask = limit_today == 1
    result = {
        "overall_ap": _average_precision(y, probabilities),
        "fresh_ap": _average_precision(
            y[fresh_mask], probabilities[fresh_mask]),
        "continuation_ap": _average_precision(
            y[continuation_mask], probabilities[continuation_mask]),
    }
    daily = {
        "overall_ap": _daily_ap_map(
            y, probabilities, dates, all_mask),
        "fresh_ap": _daily_ap_map(
            y, probabilities, dates, fresh_mask),
        "continuation_ap": _daily_ap_map(
            y, probabilities, dates, continuation_mask),
    }
    for size in (3, 5, 10):
        hits = _daily_top_hits(
            y, probabilities, dates, fresh_mask, size)
        hit_days = int(sum(hits.values()))
        eligible_days = len(hits)
        key = f"fresh_top_{size}"
        result[f"{key}_hit_days"] = hit_days
        result[f"{key}_eligible_days"] = eligible_days
        result[f"{key}_day_hit_rate"] = (
            hit_days / eligible_days if eligible_days else float("nan")
        )
        daily[key] = hits
    return result, daily


def _feature_indices(names: tuple[str, ...]) -> np.ndarray:
    available = {name: index for index, name in enumerate(FEATURE_NAMES)}
    missing = [name for name in names if name not in available]
    if missing:
        raise RuntimeError(f"특징 이름 누락: {missing}")
    return np.asarray([available[name] for name in names], dtype=np.int32)


def _fit_predict(sampled: MatrixData, full: MatrixData, fold: Fold,
                 feature_indices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    train_rows = np.flatnonzero(
        sampled.date_position < fold.train_end)
    calibration_rows = np.flatnonzero(
        (full.date_position >= fold.calibration_start)
        & (full.date_position < fold.test_start))
    test_rows = np.flatnonzero(
        (full.date_position >= fold.test_start)
        & (full.date_position < fold.test_end))
    train_x = sampled.x[np.ix_(train_rows, feature_indices)]
    calibration_x = full.x[np.ix_(
        calibration_rows, feature_indices)]
    test_x = full.x[np.ix_(test_rows, feature_indices)]
    base = _new_estimator()
    base.fit(
        train_x, sampled.y[train_rows],
        sample_weight=sampled.weights[train_rows],
    )
    model = CalibratedClassifierCV(
        FrozenEstimator(base), method="sigmoid")
    model.fit(calibration_x, full.y[calibration_rows])
    probabilities = model.predict_proba(test_x)[:, 1]
    return test_rows, probabilities


def _winner(v1: float, v4: float, tolerance: float = 1e-12) -> str:
    if not math.isfinite(v1) or not math.isfinite(v4):
        return "DRAW"
    if v4 > v1 + tolerance:
        return "V4"
    if v1 > v4 + tolerance:
        return "V1"
    return "DRAW"


def _paired_bootstrap(
    v1_values: dict[int, float], v4_values: dict[int, float],
    rng: np.random.Generator,
) -> dict:
    dates = sorted(set(v1_values) & set(v4_values))
    differences = np.asarray([
        float(v4_values[date]) - float(v1_values[date])
        for date in dates
    ], dtype=np.float64)
    if not len(differences):
        return {
            "paired_days": 0, "mean_difference": None,
            "ci_low": None, "ci_high": None,
        }
    sampled = rng.integers(
        0, len(differences),
        size=(BOOTSTRAP_ITERATIONS, len(differences)),
    )
    means = np.mean(differences[sampled], axis=1)
    return {
        "paired_days": len(differences),
        "mean_difference": float(np.mean(differences)),
        "ci_low": float(np.percentile(means, 2.5)),
        "ci_high": float(np.percentile(means, 97.5)),
        "iterations": BOOTSTRAP_ITERATIONS,
        "unit": "date_macro_v4_minus_v1",
    }


def _finite(value):
    return None if not math.isfinite(float(value)) else float(value)


def run_walkforward(db_path: Path = DB_PATH) -> dict:
    print(json.dumps({
        "event": "start",
        "initial_train_days": INITIAL_TRAIN_DAYS,
        "calibration_days": CALIBRATION_DAYS,
        "target_test_days": TARGET_TEST_DAYS,
        "negative_sample_rate": NEGATIVE_SAMPLE_RATE,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
    }, ensure_ascii=False), flush=True)
    sampled, full, dates, folds = _build_all_data(db_path)
    v1_indices = _feature_indices(V1_FEATURE_NAMES)
    v4_indices = _feature_indices(V4_FEATURE_NAMES)
    print(json.dumps({
        "event": "dataset_ready",
        "sampled_rows": len(sampled.y),
        "full_rows": len(full.y),
        "usable_dates": len(dates),
        "folds": len(folds),
        "fold_sizes": [fold.test_end - fold.test_start for fold in folds],
        "v1_features": len(v1_indices),
        "v4_features": len(v4_indices),
    }, ensure_ascii=False), flush=True)

    fold_reports = []
    oof = {
        "y": [], "dates": [], "limit_today": [],
        "v1_probabilities": [], "v4_probabilities": [],
    }
    metric_keys = (
        "overall_ap", "fresh_ap", "continuation_ap",
        "fresh_top_3_day_hit_rate", "fresh_top_5_day_hit_rate",
        "fresh_top_10_day_hit_rate",
    )
    wins = {
        key: {"V1": 0, "V4": 0, "DRAW": 0}
        for key in metric_keys
    }

    for fold in folds:
        print(json.dumps({
            "event": "fold_start", "fold": fold.fold,
            "train_from": dates[0],
            "train_to": dates[fold.train_end - 1],
            "calibration_from": dates[fold.calibration_start],
            "calibration_to": dates[fold.test_start - 1],
            "test_from": dates[fold.test_start],
            "test_to": dates[fold.test_end - 1],
        }, ensure_ascii=False), flush=True)
        v1_test_rows, v1_probabilities = _fit_predict(
            sampled, full, fold, v1_indices)
        v4_test_rows, v4_probabilities = _fit_predict(
            sampled, full, fold, v4_indices)
        if not np.array_equal(v1_test_rows, v4_test_rows):
            raise RuntimeError("v1·v4 테스트 표본이 서로 다릅니다.")
        test_y = full.y[v1_test_rows]
        test_dates = full.date_position[v1_test_rows]
        test_limit = full.limit_today[v1_test_rows]
        v1_metrics, v1_daily = _metrics(
            test_y, v1_probabilities, test_dates, test_limit)
        v4_metrics, v4_daily = _metrics(
            test_y, v4_probabilities, test_dates, test_limit)
        fold_winners = {}
        for key in metric_keys:
            winner = _winner(v1_metrics[key], v4_metrics[key])
            fold_winners[key] = winner
            wins[key][winner] += 1
        fold_report = {
            "fold": fold.fold,
            "train_from": dates[0],
            "train_to": dates[fold.train_end - 1],
            "train_days": fold.train_end,
            "calibration_from": dates[fold.calibration_start],
            "calibration_to": dates[fold.test_start - 1],
            "calibration_days": CALIBRATION_DAYS,
            "test_from": dates[fold.test_start],
            "test_to": dates[fold.test_end - 1],
            "test_days": fold.test_end - fold.test_start,
            "test_samples": len(test_y),
            "test_positives": int(np.sum(test_y)),
            "v1": {key: _finite(value)
                   for key, value in v1_metrics.items()},
            "v4": {key: _finite(value)
                   for key, value in v4_metrics.items()},
            "winners": fold_winners,
            "primary_fresh_ap_winner": fold_winners["fresh_ap"],
            "paired_bootstrap": {
                key: _paired_bootstrap(
                    v1_daily[key], v4_daily[key],
                    np.random.default_rng(RANDOM_SEED + fold.fold),
                )
                for key in (
                    "overall_ap", "fresh_ap", "continuation_ap",
                    "fresh_top_3", "fresh_top_5", "fresh_top_10",
                )
            },
        }
        fold_reports.append(fold_report)
        oof["y"].append(test_y)
        oof["dates"].append(test_dates)
        oof["limit_today"].append(test_limit)
        oof["v1_probabilities"].append(v1_probabilities)
        oof["v4_probabilities"].append(v4_probabilities)
        print(json.dumps({
            "event": "fold_complete",
            "fold": fold.fold,
            "test_from": fold_report["test_from"],
            "test_to": fold_report["test_to"],
            "v1": fold_report["v1"],
            "v4": fold_report["v4"],
            "winners": fold_winners,
        }, ensure_ascii=False), flush=True)

    all_y = np.concatenate(oof["y"])
    all_dates = np.concatenate(oof["dates"])
    all_limit = np.concatenate(oof["limit_today"])
    all_v1 = np.concatenate(oof["v1_probabilities"])
    all_v4 = np.concatenate(oof["v4_probabilities"])
    v1_aggregate, v1_daily = _metrics(
        all_y, all_v1, all_dates, all_limit)
    v4_aggregate, v4_daily = _metrics(
        all_y, all_v4, all_dates, all_limit)
    rng = np.random.default_rng(RANDOM_SEED)
    bootstrap = {
        key: _paired_bootstrap(v1_daily[key], v4_daily[key], rng)
        for key in (
            "overall_ap", "fresh_ap", "continuation_ap",
            "fresh_top_3", "fresh_top_5", "fresh_top_10",
        )
    }
    aggregate_winners = {
        key: _winner(v1_aggregate[key], v4_aggregate[key])
        for key in metric_keys
    }
    report = {
        "created_at": datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "rules": {
            "initial_train_days": INITIAL_TRAIN_DAYS,
            "calibration_days": CALIBRATION_DAYS,
            "test_windows_disjoint": True,
            "test_window_min": MIN_TEST_DAYS,
            "test_window_max": MAX_TEST_DAYS,
            "test_window_sizes": [
                fold.test_end - fold.test_start for fold in folds],
            "expanding_training_window": True,
            "past_only": True,
            "negative_sample_rate": NEGATIVE_SAMPLE_RATE,
            "negative_weight": 1.0 / NEGATIVE_SAMPLE_RATE,
            "hyperparameters_fixed": True,
            "features_fixed_per_model": True,
            "bootstrap_unit": "trade_date",
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "bootstrap_seed": RANDOM_SEED,
        },
        "features": {
            "v1_count": len(V1_FEATURE_NAMES),
            "v1_names": V1_FEATURE_NAMES,
            "v4_count": len(V4_FEATURE_NAMES),
            "v4_names": V4_FEATURE_NAMES,
        },
        "dataset": {
            "usable_from": dates[0],
            "usable_to": dates[-1],
            "usable_days": len(dates),
            "sampled_training_pool_rows": len(sampled.y),
            "full_calibration_test_pool_rows": len(full.y),
            "fold_count": len(folds),
        },
        "folds": fold_reports,
        "fold_wins": wins,
        "aggregate": {
            "test_from": dates[folds[0].test_start],
            "test_to": dates[folds[-1].test_end - 1],
            "test_days": len(np.unique(all_dates)),
            "test_samples": len(all_y),
            "test_positives": int(np.sum(all_y)),
            "v1": {key: _finite(value)
                   for key, value in v1_aggregate.items()},
            "v4": {key: _finite(value)
                   for key, value in v4_aggregate.items()},
            "winners": aggregate_winners,
            "paired_bootstrap": bootstrap,
        },
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "event": "complete",
        "report_json": str(REPORT_JSON),
        "report_md": str(REPORT_MD),
        "fold_wins": wins,
        "aggregate": report["aggregate"],
    }, ensure_ascii=False), flush=True)
    return report


def _percent(value) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.4f}%"


def _markdown(report: dict) -> str:
    lines = [
        "# v1 대 v4 다구간 워크포워드 검증",
        "",
        "초기 기본학습 250거래일, 확률보정 40거래일 뒤 서로 겹치지 "
        "않는 테스트 구간으로 순차 검증했습니다. 모든 구간에서 특징과 "
        "하이퍼파라미터는 고정했습니다.",
        "",
        "## 구간별 결과",
        "",
        "|구간|테스트 기간|일수|v1 전체 AP|v4 전체 AP|v1 신규 AP|"
        "v4 신규 AP|v1 연속 AP|v4 연속 AP|신규 AP 승자|",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for fold in report["folds"]:
        lines.append(
            f"|{fold['fold']}|{fold['test_from']}~{fold['test_to']}|"
            f"{fold['test_days']}|{_percent(fold['v1']['overall_ap'])}|"
            f"{_percent(fold['v4']['overall_ap'])}|"
            f"{_percent(fold['v1']['fresh_ap'])}|"
            f"{_percent(fold['v4']['fresh_ap'])}|"
            f"{_percent(fold['v1']['continuation_ap'])}|"
            f"{_percent(fold['v4']['continuation_ap'])}|"
            f"{fold['primary_fresh_ap_winner']}|"
        )
    lines += [
        "",
        "## 신규 Top-K 실제 적중 일수",
        "",
        "|구간|대상 일수|v1 Top3|v4 Top3|v1 Top5|v4 Top5|"
        "v1 Top10|v4 Top10|",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for fold in report["folds"]:
        v1, v4 = fold["v1"], fold["v4"]
        lines.append(
            f"|{fold['fold']}|{v1['fresh_top_3_eligible_days']}|"
            f"{v1['fresh_top_3_hit_days']}|"
            f"{v4['fresh_top_3_hit_days']}|"
            f"{v1['fresh_top_5_hit_days']}|"
            f"{v4['fresh_top_5_hit_days']}|"
            f"{v1['fresh_top_10_hit_days']}|"
            f"{v4['fresh_top_10_hit_days']}|"
        )
    aggregate = report["aggregate"]
    v1, v4 = aggregate["v1"], aggregate["v4"]
    lines += [
        "",
        "## 전체 합산",
        "",
        "|지표|v1|v4|승자|",
        "|---|---:|---:|---|",
        f"|전체 AP|{_percent(v1['overall_ap'])}|"
        f"{_percent(v4['overall_ap'])}|"
        f"{aggregate['winners']['overall_ap']}|",
        f"|신규 AP|{_percent(v1['fresh_ap'])}|"
        f"{_percent(v4['fresh_ap'])}|"
        f"{aggregate['winners']['fresh_ap']}|",
        f"|연속 AP|{_percent(v1['continuation_ap'])}|"
        f"{_percent(v4['continuation_ap'])}|"
        f"{aggregate['winners']['continuation_ap']}|",
    ]
    for size in (3, 5, 10):
        key = f"fresh_top_{size}"
        lines.append(
            f"|신규 Top{size} 일자 적중|"
            f"{v1[f'{key}_hit_days']}/{v1[f'{key}_eligible_days']} "
            f"({_percent(v1[f'{key}_day_hit_rate'])})|"
            f"{v4[f'{key}_hit_days']}/{v4[f'{key}_eligible_days']} "
            f"({_percent(v4[f'{key}_day_hit_rate'])})|"
            f"{aggregate['winners'][f'{key}_day_hit_rate']}|"
        )
    lines += [
        "",
        "## 평가 방식 차이와 Top-K 제외 사유",
        "",
        "- 전체 합산 신규 AP는 518일의 신규 표본을 모두 이어 붙인 뒤 한 번 "
        "계산한 pooled AP입니다. 표본과 양성이 많은 날짜의 영향이 커지고, "
        "서로 다른 워크포워드 구간의 확률도 하나의 순위로 함께 비교됩니다.",
        "- bootstrap의 신규 AP 평균 차이는 신규 양성이 존재하는 날짜마다 "
        "일별 AP를 계산한 뒤, 같은 날짜의 `v4 - v1` 차이에 동일 가중치를 "
        "준 date-macro 평균입니다. AP는 비선형 순위 지표이므로 pooled AP "
        "차이와 일별 AP 차이의 평균은 일치할 필요가 없으며 부호도 달라질 "
        "수 있습니다.",
        "- 테스트 518일 중 2024-07-23은 다음 거래일인 2024-07-24의 신규 "
        "상한가 양성이 0건이어서 신규 Top-K 및 신규 일별 AP 대상에서 "
        "제외했습니다. 다음 날의 유일한 상한가 양성인 셀리드(299660)는 "
        "2024-07-23에도 상한가였으므로 신규가 아니라 연속 양성입니다. "
        "따라서 신규 Top-K 대상일은 517일입니다.",
        "",
        "## 날짜 paired bootstrap 95% 신뢰구간",
        "",
        "차이는 `v4 - v1`이며, AP 신뢰구간은 같은 날짜의 일별 AP "
        "차이를 짝지어 재표본화한 date-macro 결과입니다.",
        "",
        "|지표|짝지은 날짜|평균 차이|95% CI|",
        "|---|---:|---:|---:|",
    ]
    labels = {
        "overall_ap": "전체 AP",
        "fresh_ap": "신규 AP",
        "continuation_ap": "연속 AP",
        "fresh_top_3": "신규 Top3 적중",
        "fresh_top_5": "신규 Top5 적중",
        "fresh_top_10": "신규 Top10 적중",
    }
    for key, label in labels.items():
        row = aggregate["paired_bootstrap"][key]
        lines.append(
            f"|{label}|{row['paired_days']}|"
            f"{_percent(row['mean_difference'])}|"
            f"[{_percent(row['ci_low'])}, {_percent(row['ci_high'])}]|"
        )
    lines += [
        "",
        "## 구간별 승패 수",
        "",
        "|지표|v1 승|v4 승|무승부|",
        "|---|---:|---:|---:|",
    ]
    for key, counts in report["fold_wins"].items():
        lines.append(
            f"|{key}|{counts['V1']}|{counts['V4']}|{counts['DRAW']}|")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    run_walkforward()

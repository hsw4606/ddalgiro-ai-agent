"""다중 출처의 공통 계산 경로. 운영 pipeline과 과거 자료 재생 backtest가 함께 쓴다.

최근 10일이 모두 확인된 출처만 고정해, 각 출처의 평균을 100으로 맞춘 뒤 같은 계열끼리 평균한다.
단위가 다른 RSS 건수·검색 비율·SNS 건수를 그대로 더하지 않는다. 두 계열 이상이 있어야 판정한다.
DB·네트워크·LLM을 부르지 않는다. 누락은 None, 실제 0건은 0으로 구분하며 미래 날짜는 무시한다.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from app.trend import scoring, sources

MIN_HISTORY_DAYS = scoring.MIN_POINTS
WINDOW_DAYS = 30
LONG_WINDOW_DAYS = 91
NOT_ENOUGH = "자료부족"
VERSION = "multi-source-v2"


def source_views(rows: list[dict], as_of: date, is_sample: bool = False) -> dict[str, dict]:
    """10일 연속 완료된 출처의 값·계열·증가율을 반환한다. 연습 자료는 글/검색을 가상 출처로 나눈다."""
    values: dict[str, dict] = {}
    for row in rows:
        pairs = [(row["source"], sources.family_of(row["source"]))]
        if is_sample and row["source"] == sources.SAMPLE:
            pairs = [("sample:글", sources.TEXT), ("sample:검색", sources.SEARCH)]
        for name, family in pairs:
            value = row.get("search_ratio") if family == sources.SEARCH else row.get("mention_count")
            if not family or not row["is_complete"] or value is None or row["metric_date"] > as_of:
                continue
            view = values.setdefault(name, {"family": family, "days": {}})
            view["days"][row["metric_date"]] = float(value)
    # 조회 기준일에 고른 출처 묶음을 10일 내내 고정한다. 새 출처가 생겨도 중간 날짜부터 덧붙이지 않는다.
    days = [as_of - timedelta(days=i) for i in range(MIN_HISTORY_DAYS - 1, -1, -1)]
    views = {}
    for name, view in values.items():
        if not all(day in view["days"] for day in days):
            continue
        series = [view["days"][day] for day in days]
        views[name] = {**view, "series": series, "growth_rate": round(scoring.growth_rate(series), 4),
                       "total_7d": round(sum(series[-7:]), 1)}
    return views


def family_series(views: dict[str, dict], days: list[date] | None = None) -> dict[str, list[float]]:
    """출처마다 평균 100으로 바꾼 뒤 계열별 평균을 낸다. days를 주면 그 날짜 전체가 있는 출처 묶음만 사용한다."""
    grouped: dict[str, list[list[float]]] = {}
    for view in views.values():
        if days is not None and not all(day in view["days"] for day in days):
            continue
        series = [view["days"][day] for day in days] if days is not None else view["series"]
        base = statistics.mean(series) or 1.0
        grouped.setdefault(view["family"], []).append([value * 100 / base for value in series])
    return {family: [statistics.mean(column) for column in zip(*series)] for family, series in grouped.items()}


def family_growth(views: dict[str, dict]) -> dict[str, float]:
    """정규화한 계열별 증가율. 같은 단위로 맞춘 뒤 계산하여 대형 출처 하나의 건수에 끌려가지 않는다."""
    return {family: round(scoring.growth_rate(series), 4) for family, series in family_series(views).items()}


def metric_series(rows: list[dict], as_of: date, is_sample: bool) -> list[dict]:
    """화면용 30일 실제 글 수·검색 비율. SNS는 별도 칸이며, 검색이 없을 때 SNS를 검색량으로 표시하지 않는다."""
    by_day: dict[date, dict[str, list[float]]] = {}
    for row in rows:
        if not row["is_complete"]:
            continue
        family = sources.family_of(row["source"])
        pairs = [(family, row.get("search_ratio") if family == sources.SEARCH else row.get("mention_count"))]
        if is_sample and row["source"] == sources.SAMPLE:
            pairs = [(sources.TEXT, row.get("mention_count")), (sources.SEARCH, row.get("search_ratio"))]
        for family, value in pairs:
            if family and value is not None:
                by_day.setdefault(row["metric_date"], {}).setdefault(family, []).append(float(value))
    series = []
    for offset in range(WINDOW_DAYS - 1, -1, -1):
        day = as_of - timedelta(days=offset)
        families = by_day.get(day, {})
        series.append({"metric_date": day, "mentions": sum(families[sources.TEXT]) if sources.TEXT in families else None,
                       "search": statistics.mean(families[sources.SEARCH]) if sources.SEARCH in families else None,
                       "social": sum(families[sources.SOCIAL]) if sources.SOCIAL in families else None,
                       "is_complete": len(families) >= 2})
    return series


def prepare(rows: list[dict], as_of: date, is_sample: bool = False) -> dict[str, Any]:
    """운영과 백테스트가 같은 출처·연속일·정규화 규칙으로 계산 재료를 만든다. 기준일 이후 값은 사용하지 않는다."""
    views = source_views(rows, as_of, is_sample)
    families = family_series(views)
    order = [family for family in (sources.TEXT, sources.SEARCH, sources.SOCIAL) if family in families]
    primary = order[0] if order else None
    secondary = order[1] if len(order) > 1 else None
    enough = secondary is not None
    history = 0
    # 출처 묶음 전체가 이어진 날수를 센다. 10일 미만인 새 출처는 이번 판정에 넣지 않는다.
    if enough:
        for offset in range(WINDOW_DAYS):
            day = as_of - timedelta(days=offset)
            if not all(day in view["days"] for view in views.values()):
                break
            history += 1
    # 긴 창은 선택한 한 계열의 같은 출처들이 모두 확인된 연속 날짜만 사용한다.
    long_family = sources.SEARCH if sources.SEARCH in families else primary
    long_views = {name: view for name, view in views.items() if view["family"] == long_family}
    long_days: list[date] = []
    if long_views:
        for offset in range(LONG_WINDOW_DAYS):
            day = as_of - timedelta(days=offset)
            if not all(day in view["days"] for view in long_views.values()):
                break
            long_days.insert(0, day)
    long_values = family_series(long_views, long_days).get(long_family, []) if long_days else []
    return {"as_of": as_of, "history_days": history, "mentions": families.get(primary, []),
            "search": families.get(secondary, []), "search_long": long_values, "by_source": views,
            "n_sources": (1 if is_sample else sum(view["total_7d"] > 0 for view in views.values())),
            "raw_mentions_7d": int(sum(v["total_7d"] for v in views.values() if v["family"] == sources.TEXT)),
            "basis": {"version": VERSION, "primary_family": primary, "secondary_family": secondary,
                      "long_family": long_family, "sources": sorted(views), "normalization": "source mean=100; family mean"}}


def compute(state: dict[str, Any]) -> dict[str, Any]:
    """prepare의 재료와 감성 비율로 점수·단계를 계산한다. 부족한 자료에는 판정과 브리핑을 만들지 않는다."""
    enough = state["history_days"] >= MIN_HISTORY_DAYS
    primary = [float(v) for v in state["mentions"][-MIN_HISTORY_DAYS:]] if enough else []
    secondary = [float(v) for v in state["search"][-MIN_HISTORY_DAYS:]] if enough else []
    views = state.get("by_source") or {}
    families = family_growth(views)
    basis = state.get("basis") or {"primary_family": sources.TEXT, "secondary_family": sources.SEARCH}
    # 실제 검색 계열이 없으면 search_growth는 비운다. 대체 계열의 가속은 basis와 함께 momentum에 둔다.
    return {
        "score": scoring.trend_score(primary, secondary, state.get("sentiment_pos"), state["n_sources"]) if enough else 0.0,
        "stage": scoring.stage(primary) if enough else NOT_ENOUGH,
        "growth_rate": round(scoring.growth_rate(primary), 4) if enough else 0.0,
        "momentum": round(scoring.momentum(secondary), 4) if enough else 0.0,
        "sentiment_pos": state.get("sentiment_pos"), "n_sources": state["n_sources"],
        "mentions_7d": state.get("raw_mentions_7d", int(sum(v for v in state["mentions"][-7:] if v is not None))),
        "search_growth": families.get(sources.SEARCH) if enough else None,
        "signal_check": scoring.agreement(families) if enough else None,
        "by_source": {"families": families, "basis": basis,
                      "sources": {name: {k: v[k] for k in ("family", "growth_rate", "total_7d")} for name, v in views.items()}} if enough else None,
        "windows": scoring.windows(primary, state.get("search_long") or []) if enough else None,
    }

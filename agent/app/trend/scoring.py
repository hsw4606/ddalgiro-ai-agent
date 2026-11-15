"""트렌드 점수와 확산 단계 계산 — LLM 을 쓰지 않는 순수 계산.

쓰는 곳: trend/pipeline.py 의 score 단계, trend/backtest.py 의 백테스팅.
출처:   기획 02 §5.3.2 와 ddalgiro-tips-task 가이드 09 의 "트렌드 스코어 v1" 을 그대로 옮겼다.
        연구개발계획서의 "트렌드 스코어 산출 플로우"(입력 신호 → 증가율·가속 → 가중 융합 → 단계 판정 → 다중 시간창)와 같은 순서다.
핵심:   "얼마나 많이 언급됐나"(양)가 아니라 "얼마나 빨리 늘고 있나"(증가율)와
        "그 증가가 더 빨라지고 있나"(가속)를 본다. 양이 커진 뒤에는 이미 정점이기 때문이다.
입력의 약속: 모든 시계열은 한 칸에 값 하나, 오래된 것 → 최근 것 순서의 목록이다. 한 칸은 하루일 수도, 한 주일 수도 있다.
조정값: 가중치(ScoreWeights)와 단계 기준(StageThresholds)은 고정된 답이 아니라 백테스팅으로 다시 맞추는 값이다.
        그래서 함수 인자로 바꿔 넣을 수 있게 했고, 운영 기본값은 DEFAULT_WEIGHTS·DEFAULT_THRESHOLDS 다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

# 증가율(7칸)과 가속(3칸 전 시점의 증가율)을 함께 계산하는 데 필요한 최소 칸 수
MIN_POINTS = 10
DAYS_PER_WEEK = 7


@dataclass(frozen=True)
class ScoreWeights:
    """트렌드 점수의 가중치(합 100). 백테스팅 결과를 보고 조정한다."""

    mention_growth: float = 40.0   # 언급 증가율
    search_momentum: float = 30.0  # 검색 관심도의 가속
    sentiment: float = 20.0        # 긍정 비율(호감도)
    sources: float = 10.0          # 출처 다양성


@dataclass(frozen=True)
class StageThresholds:
    """확산 단계를 가르는 증가율 기준. 0.05 는 5% 다."""

    quiet: float = 0.05  # 증가율이 이 값 이하면 아직 잠잠하다(태동기)
    peak: float = 0.30   # 가속이 멈췄는데 증가율이 이 값을 넘으면 정점이다


DEFAULT_WEIGHTS = ScoreWeights()
DEFAULT_THRESHOLDS = StageThresholds()


def growth_rate(series: list[float]) -> float:
    """최근 3칸 평균이 그 직전 4칸 평균보다 얼마나 늘었는지의 비율.

    반환: 0.25 는 25% 증가, 음수는 감소. 값이 4개 미만이면 판단할 수 없어 0.0.
    """
    if len(series) < 4:
        return 0.0
    recent = statistics.mean(series[-3:])
    base = statistics.mean(series[-7:-3])
    # 직전에도 지금도 0 이면 달라진 것이 없다(0 을 아주 작은 수로 바꿔 나누면 -100% 로 잘못 나온다)
    if base == 0 and recent == 0:
        return 0.0
    # 직전 구간 평균만 0 이면 나눌 수 없어 아주 작은 수로 바꾼다(0 에서 새로 생긴 신호는 매우 큰 증가로 본다)
    base = base or 1e-9
    return (recent - base) / base


def momentum(series: list[float]) -> float:
    """증가율이 3칸 전보다 얼마나 커졌는지(가속).

    반환: 양수면 증가가 빨라지는 중, 음수면 둔해지는 중. 값이 8개 미만이면 0.0.
    """
    if len(series) < 8:
        return 0.0
    # 지금의 증가율에서, 최근 3칸을 떼어 낸 3칸 전 시점의 증가율을 뺀다
    return growth_rate(series) - growth_rate(series[:-3])


def _clip(value: float) -> float:
    """값을 0~1 사이로 자른다. 음수(감소)와 지나치게 큰 값이 점수를 흔들지 않게 한다."""
    return max(0.0, min(value, 1.0))


def trend_score(
    mentions: list[float],
    search: list[float],
    sentiment_pos: float | None,
    n_sources: int,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> float:
    """0~100 점의 트렌드 점수.

    입력: mentions 는 날짜별 언급 수, search 는 날짜별 검색 관심도,
          sentiment_pos 는 긍정 비율 0~1(모르면 None), n_sources 는 신호가 잡힌 출처 수,
          weights 는 가중치(기본: 언급 증가율 40 + 검색 가속 30 + 호감도 20 + 출처 다양성 10).
    """
    score = (
        weights.mention_growth * _clip(growth_rate(mentions))
        + weights.search_momentum * _clip(momentum(search))
        # 긍정 비율을 모르면 중간값 0.5 로 본다
        + weights.sentiment * (sentiment_pos if sentiment_pos is not None else 0.5)
        # 출처가 4곳 이상이면 만점. 한 곳에서만 튀는 신호를 덜 믿기 위한 항목이다
        + weights.sources * min(n_sources / 4, 1.0)
    )
    return round(min(score, 100.0), 1)


def stage(series: list[float], thresholds: StageThresholds = DEFAULT_THRESHOLDS) -> str:
    """시계열로 확산 단계를 판정한다.

    반환: '태동기'(아직 잠잠) / '성장기'(증가가 가속 중 — 추천할 때) / '정점'(크게 늘었지만 가속이 멈춤) / '쇠퇴기'.
    """
    growth = growth_rate(series)
    accel = momentum(series)
    if growth <= thresholds.quiet:
        return "태동기"
    if accel > 0:
        return "성장기"
    if growth > thresholds.peak:
        return "정점"
    return "쇠퇴기"


# ───────────────────────────── 교차검증 ─────────────────────────────

def agreement(family_growth: dict[str, float], thresholds: StageThresholds = DEFAULT_THRESHOLDS) -> str:
    """서로 다른 계열의 신호가 함께 오르는지 본다(신호 교차검증).

    입력: family_growth 는 {계열 이름: 증가율}. 계열은 글·검색·SNS 처럼 종류가 다른 신호 묶음이다(trend/sources.py).
    왜: 글이나 SNS 는 홍보로 한꺼번에 늘어날 수 있지만 검색은 실제 관심이 있어야 는다.
        둘 이상의 계열이 함께 오르면 믿을 만하고, 한 계열만 오르면 판단을 미룬다.
    반환: '일치'(두 계열 이상이 기준보다 크게 증가) / '불일치'(한 계열만 증가) / '약함'(모두 잠잠).
    """
    rising = sum(1 for growth in family_growth.values() if growth > thresholds.quiet)
    if rising >= 2:
        return "일치"
    return "불일치" if rising == 1 else "약함"


# ───────────────────────────── 다중 시간창 ─────────────────────────────

def weekly_means(daily: list[float]) -> list[float]:
    """하루 단위 시계열을 한 주 평균의 시계열로 바꾼다(오래된 주 → 최근 주).

    가장 최근 날부터 7일씩 묶는다. 맨 앞에 7일이 안 되는 자투리는 한 주로 볼 수 없어 버린다.
    """
    weeks = len(daily) // DAYS_PER_WEEK
    start = len(daily) - weeks * DAYS_PER_WEEK
    return [statistics.mean(daily[start + index * DAYS_PER_WEEK : start + (index + 1) * DAYS_PER_WEEK]) for index in range(weeks)]


def window_view(series: list[float], thresholds: StageThresholds = DEFAULT_THRESHOLDS) -> dict[str, Any] | None:
    """시계열 하나의 증가율·가속·단계를 묶어 돌려준다. 칸이 MIN_POINTS 개 미만이면 판단하지 않고 None."""
    if len(series) < MIN_POINTS:
        return None
    return {
        "growth_rate": round(growth_rate(series), 4),
        "momentum": round(momentum(series), 4),
        "stage": stage(series, thresholds),
    }


def windows(daily_short: list[float], daily_long: list[float], thresholds: StageThresholds = DEFAULT_THRESHOLDS) -> dict[str, Any]:
    """짧은 창과 긴 창을 나란히 판정한다 — "지금 뜨나" 와 "오래 가나" 를 따로 보기 위해서다.

    입력: daily_short 는 하루 단위 최근 값(7일 창의 재료), daily_long 은 하루 단위 약 3개월 값(주 단위로 묶어 본다).
    반환: {"7d": 판정 또는 None, "3m": 판정 또는 None}. 자료가 모자란 창은 None 이다.
    하루보다 짧은 창(24시간)은 하루에 여러 번 모으는 출처가 생기면 같은 방법으로 더한다.
    """
    return {
        "7d": window_view(daily_short, thresholds),
        "3m": window_view(weekly_means(daily_long), thresholds),
    }

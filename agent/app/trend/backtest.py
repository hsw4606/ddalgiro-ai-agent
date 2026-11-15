"""백테스팅 — 이미 흥망이 끝난 아이템의 과거 자료로 "정점보다 며칠 앞서 성장기를 잡았는가" 를 잰다. LLM 을 쓰지 않는다.

쓰는 곳: jobs/backtest.py. 운영 재생은 evaluate_metrics, 검색 한 계열만의 탐색은 evaluate_case를 쓴다.
출처:   ddalgiro-tips-task 가이드 09 §5~6 의 first_growth_signal·lead_time·baseline_signal 을 옮겼다.
        연구개발계획서의 "백테스팅 프로토콜"(미래 정보 누설을 막은 시뮬레이션, 리드타임 = 정점일 − 신호일)이 기준이다.
방법:   시계열을 한 칸씩 앞으로 가며 "그날까지의 값만" 으로 단계를 판정한다. 뒤의 값을 미리 보지 않는다.
왜 뒤의 값이 새지 않나: 데이터랩 값은 "기간 안 최댓값을 100 으로 본 상대값" 이라 값의 크기에는 뒤의 정보가 섞여 있다.
        하지만 증가율·가속은 "평균끼리의 비율" 이어서 전체에 같은 수를 곱해도 결과가 같다. 그래서 크기 기준을 쓰지 않는다.
용어:   리드타임 = 정점일 − 첫 신호일(양수면 정점보다 앞서 잡은 것).  헛신호 = 뜨지 않았는데 성장기로 판정한 것.
        비교 기준(베이스라인) = 직전 7칸 평균보다 50% 넘게 뛴 첫 시점. 우리 방법이 이보다 얼마나 앞서는지가 개선폭이다.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from app.trend import scoring, signals

# 시계열 한 칸: (그 칸의 날짜, 값). 날짜가 연속으로 이어진 하루 단위 자료만 판정한다.
Point = tuple[date, float]

BASELINE_WINDOW = 7       # 비교 기준이 평균을 내는 직전 칸 수
BASELINE_THRESHOLD = 0.5  # 비교 기준이 "급증" 으로 보는 증가 비율(50%)


def growth_signals(points: list[Point], thresholds: scoring.StageThresholds = scoring.DEFAULT_THRESHOLDS) -> list[date]:
    """한 칸씩 전진하며 성장기로 판정한 날짜를 모두 돌려준다(오래된 → 최근).

    달력상 연속 10일이 쌓인 뒤 최근 10일만 scoring.stage에 넘긴다. 뒤의 값과 주 단위 값은 쓰지 않는다.
    """
    seen: list[float] = []
    found: list[date] = []
    previous = None
    for day, value in sorted(points):
        if previous is not None and day != previous + timedelta(days=1):
            seen = []  # 달력상 빠진 날을 붙여 연속 자료처럼 판정하지 않는다
        previous = day
        seen.append(value)
        if len(seen) >= scoring.MIN_POINTS and scoring.stage(seen[-scoring.MIN_POINTS:], thresholds) == "성장기":
            found.append(day)
    return found


def first_growth_signal(points: list[Point], thresholds: scoring.StageThresholds = scoring.DEFAULT_THRESHOLDS) -> date | None:
    """처음으로 성장기로 판정한 날짜. 끝까지 판정이 없으면 None."""
    signals = growth_signals(points, thresholds)
    return signals[0] if signals else None


def baseline_signal(points: list[Point], window: int = BASELINE_WINDOW, threshold: float = BASELINE_THRESHOLD) -> date | None:
    """비교 기준: 직전 window 칸의 평균보다 threshold 이상 뛴 첫 날짜. 없으면 None.

    단순 이동평균 임계 방식이다. 값이 이미 크게 뛴 뒤에야 걸리므로 보통 우리 방법보다 늦다.
    """
    seen: list[float] = []
    previous = None
    for day, value in sorted(points):
        if previous is not None and day != previous + timedelta(days=1):
            seen = []
        previous = day
        seen.append(value)
        if len(seen) < max(scoring.MIN_POINTS, window + 1):
            continue
        average = statistics.mean(seen[-window - 1 : -1])
        # 직전 평균이 0 이면 비율을 낼 수 없어 넘어간다
        if average > 0 and (value - average) / average >= threshold:
            return day
    return None


def peak_point(points: list[Point]) -> date | None:
    """정점일 = 평가 기간 안에서 값이 가장 컸던 날짜. 같은 값이 여러 번이면 가장 이른 날. 빈 목록이면 None."""
    if not points:
        return None
    best = max(value for _day, value in points)
    return next(day for day, value in points if value == best)


def lead_time_days(signal: date | None, peak: date | None) -> int | None:
    """리드타임(일) = 정점일 − 신호일. 둘 중 하나라도 없으면 None. 음수면 정점이 지난 뒤에야 잡은 것이다."""
    if signal is None or peak is None:
        return None
    return (peak - signal).days


def evaluate_case(
    term: str,
    points: list[Point],
    peak_label: str | None,
    thresholds: scoring.StageThresholds = scoring.DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """아이템 하나를 평가해 결과표의 한 줄을 만든다.

    단일 검색 출처의 탐색용 함수이며 운영 재생은 evaluate_metrics를 쓴다.
    입력: peak_label 은 보도로 알려진 정점 달 'YYYY-MM'. 뚜렷한 정점 없이 꾸준히 팔리는 아이템(지속형)은 None.
    반환: 정점이 있는 아이템은 리드타임(우리·비교 기준)과 "실측 정점이 알려진 달과 맞는지",
          지속형은 성장기 판정 횟수(헛신호)를 담는다.
    """
    signals = growth_signals(points, thresholds)
    row: dict[str, Any] = {
        "term": term,
        "peak_label": peak_label,
        "signal": signals[0].isoformat() if signals else None,
        "signal_days": len(signals),
        "baseline_signal": None,
        "peak": None,
        "label_matches": None,
        "lead_days": None,
        "baseline_lead_days": None,
        "false_signal": None,
    }
    baseline = baseline_signal(points)
    row["baseline_signal"] = baseline.isoformat() if baseline else None
    if peak_label is None:
        # 지속형: 정점이 없으므로 성장기 판정이 한 번이라도 나오면 헛신호다
        row["false_signal"] = bool(signals)
        return row
    peak = peak_point(points)
    row["peak"] = peak.isoformat() if peak else None
    # 실측 정점이 알려진 달과 다르면 검색어 표기나 수집 기간을 다시 봐야 한다
    row["label_matches"] = bool(peak and peak.strftime("%Y-%m") == peak_label)
    row["lead_days"] = lead_time_days(signals[0] if signals else None, peak)
    row["baseline_lead_days"] = lead_time_days(baseline, peak)
    # 정점이 지난 뒤에야 처음 잡았으면 조기 포착에 실패한 것이다
    row["false_signal"] = row["lead_days"] is not None and row["lead_days"] < 0
    return row


def evaluate_metrics(term: str, metrics: list[dict], peak: date | None, *, is_sample: bool = False) -> dict[str, Any]:
    """다중 출처 원자료를 운영의 prepare/compute로 재생한다. peak는 사전에 검수한 정답 정점일이다.

    날짜는 date 객체다. 그날 이후 값은 넘기지 않으며 누락·출처 대체·정규화도 운영과 같다.
    매일 판정과 근거를 반환한다. 실제 관측 당시 자료의 보관과 정점 정답 검수는 입력 자료의 책임이다.
    """
    replay = []
    baseline = None
    for day in sorted({row["metric_date"] for row in metrics}):
        state = signals.prepare([row for row in metrics if row["metric_date"] <= day], day, is_sample)
        replay.append({"day": day.isoformat(), **signals.compute(state)})
        # 비교 기준도 같은 날 사용 가능한 주 계열의 동일 10일 창에서 계산한다.
        values = state["mentions"]
        if baseline is None and state["history_days"] >= scoring.MIN_POINTS:
            average = statistics.mean(values[-BASELINE_WINDOW - 1:-1])
            if average > 0 and (values[-1] - average) / average >= BASELINE_THRESHOLD:
                baseline = day
    found = [date.fromisoformat(row["day"]) for row in replay if row["stage"] == "성장기"]
    first = found[0] if found else None
    return {"term": term, "mode": "production-replay", "peak_label": peak.strftime("%Y-%m") if peak else None,
            "signal": first.isoformat() if first else None, "signal_days": len(found),
            "baseline_signal": baseline.isoformat() if baseline else None, "peak": peak.isoformat() if peak else None,
            "label_matches": True if peak else None, "lead_days": lead_time_days(first, peak),
            "baseline_lead_days": lead_time_days(baseline, peak),
            "false_signal": bool(found) if peak is None else bool(first and first > peak), "replay": replay}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """정점 정답이 일치하고 두 방법 모두 신호를 낸 같은 항목만 짝지어 개선 일수를 평균한다.

    한 방법이 놓친 항목·정답 불일치는 개선 평균에 숨겨 넣지 않고 별도 수로 보고한다.
    """
    labeled = [row for row in rows if row["peak_label"] and row.get("label_matches")]
    paired = [row for row in labeled if row["lead_days"] is not None and row["baseline_lead_days"] is not None]
    ours = statistics.mean(row["lead_days"] for row in paired) if paired else None
    baseline = statistics.mean(row["baseline_lead_days"] for row in paired) if paired else None
    return {"items": len(rows), "paired_items": len(paired),
            "avg_lead_days": round(ours, 1) if ours is not None else None,
            "avg_baseline_lead_days": round(baseline, 1) if baseline is not None else None,
            "improvement_days": round(statistics.mean(row["lead_days"] - row["baseline_lead_days"] for row in paired), 1) if paired else None,
            "label_mismatches": sum(bool(row["peak_label"] and not row.get("label_matches")) for row in rows),
            "missed": sum(row["signal"] is None for row in labeled),
            "baseline_missed": sum(row["baseline_signal"] is None for row in labeled),
            "false_signals": sum(bool(row["false_signal"]) for row in rows)}

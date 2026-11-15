"""백테스팅 실행 — 흥망이 끝난 과거 디저트 아이템으로 "정점보다 며칠 앞서 성장기를 잡았는가" 를 잰다 (안건 1).

실행: python -m app.jobs.backtest --input cases.json (다중 출처 원자료를 운영 계산기로 재생)
      python -m app.jobs.backtest                 (데이터랩 하루 단위 탐색; 운영 성능 평가가 아님)
      python -m app.jobs.backtest --no-save       (화면에만 출력)
하는 일: 검증셋(VALIDATION_SET)의 아이템마다 네이버 데이터랩에서 그 기간의 검색 관심도를 받아,
        우리 판정(trend/scoring.py 의 단계 판정)과 비교 기준(단순 이동평균 임계)의 첫 신호일·리드타임을 나란히 계산한다.
        계산은 trend/backtest.py 가 하고, 이 파일은 자료 받기·출력·보관만 맡는다.
전제: 네이버 Client ID·Secret 이 환경 변수에 있어야 한다. 없으면 아무것도 하지 않고 끝난다. 아이템 하나에 데이터랩 1회를 쓴다.
보관: 받은 시계열·설정·결과표를 backtest_runs 에 한 줄로 남긴다. 같은 자료로 다시 계산하거나 제3자에게 보일 수 있게 하기 위해서다.
출처: 검증셋과 절차는 ddalgiro-tips-task 가이드 09 §5 를 옮겼다. 정점 달은 보도 기준의 근삿값이라,
      결과표의 "실측 정점이 알려진 달과 맞는지"(label_matches)가 거짓이면 검색어 표기와 기간부터 다시 본다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import httpx

from app import db
from app.jobs import collect_naver
from app.settings import settings
from app.trend import backtest, scoring, signals

# (검색어, 받을 기간의 시작·끝, 알려진 정점 달). 기간은 떠오르기 전부터 식은 뒤까지를 담는다.
# peak 가 None 인 것은 뚜렷한 정점 없이 꾸준히 팔리는 지속형이다 — 성장기 신호가 나오면 헛신호로 센다.
# 데이터랩은 검색어의 띄어쓰기에 민감하다. 표기를 바꾸면 결과가 달라진다.
VALIDATION_SET = [
    {"term": "탕후루", "start": "2023-01-01", "end": "2024-03-31", "peak": "2023-08"},
    {"term": "두바이초콜릿", "start": "2024-01-01", "end": "2025-06-30", "peak": "2024-07"},
    {"term": "요아정", "start": "2024-01-01", "end": "2025-06-30", "peak": "2024-08"},
    {"term": "두바이쫀득쿠키", "start": "2025-09-01", "end": "2026-06-15", "peak": "2026-01"},
    {"term": "약과", "start": "2021-06-01", "end": "2023-12-31", "peak": "2023-03"},
    {"term": "런던베이글뮤지엄", "start": "2021-06-01", "end": "2023-12-31", "peak": None},
    {"term": "소금빵", "start": "2021-01-01", "end": "2024-06-30", "peak": None},
]


def run(client: httpx.Client, unit: str) -> tuple[list[dict], dict]:
    """검증셋 전체를 받아 평가한다.

    입력: unit은 하루 단위 'date'만 허용한다. 이 경로는 단일 출처 탐색용이다.
    반환: (아이템별 결과 목록, 요약). 결과 한 줄에는 받은 시계열(series)도 들어 있다.
    HTTP 오류(인증 실패 401, 한도 초과 429 등)는 예외로 올린다 — 일부만 받은 결과표는 비교에 쓸 수 없기 때문이다.
    """
    if unit != "date":
        raise ValueError("백테스트는 일 단위 자료만 받습니다.")
    rows = []
    for item in VALIDATION_SET:
        ratios = collect_naver.fetch_datalab(
            client, item["term"], date.fromisoformat(item["start"]), date.fromisoformat(item["end"]), time_unit=unit)
        points = sorted(ratios.items())   # 날짜 순(오래된 → 최근)
        row = backtest.evaluate_case(item["term"], points, item["peak"])
        row["mode"] = "single-source-exploration"
        row["series"] = [[day.isoformat(), value] for day, value in points]
        rows.append(row)
        time.sleep(collect_naver.PAUSE_SECONDS)
    return rows, backtest.summarize(rows)


def report(rows: list[dict], summary: dict) -> None:
    """결과표와 요약을 화면에 찍는다."""
    for row in rows:
        if row["peak_label"] is None:
            verdict = f"지속형 — 성장기 판정 {row['signal_days']}회" + (" (헛신호)" if row["false_signal"] else " (정상)")
        elif row["signal"] is None:
            verdict = "신호 없음(놓침)"
        else:
            verdict = (f"리드타임 {row['lead_days']}일 / 비교 기준 {row['baseline_lead_days']}일"
                       + ("" if row["label_matches"] else " ⚠ 실측 정점이 알려진 달과 다름"))
        print(f"[{row['term']}] 첫 신호 {row['signal']} · 실측 정점 {row['peak']} (알려진 달 {row['peak_label']}) → {verdict}")
    print(f"짝지은 {summary['paired_items']}개 평균 리드타임 — 우리: {summary['avg_lead_days']}일 / 비교 기준: {summary['avg_baseline_lead_days']}일 "
          f"(개선 {summary['improvement_days']}일), 정답 불일치 {summary['label_mismatches']}개, 놓침 {summary['missed']}건, 헛신호 {summary['false_signals']}건")


def main() -> int:
    """검증셋을 평가해 출력하고 보관한다. 반환값은 종료 코드: 정상 종료면 0, 사용법이 틀리면 2, 키·자료가 없거나 잘못되면 1."""
    parser = argparse.ArgumentParser(description="운영 다중 출처 재생 또는 단일 검색 출처 탐색")
    parser.add_argument("--input", type=Path, help="[{term,peak: YYYY-MM-DD 또는 null,metrics:[일별 DB 행]}] JSON")
    parser.add_argument("--unit", choices=["date"], default="date")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    unit = args.unit
    params = {"unit": unit, "calculator": signals.VERSION, "minimum_days": scoring.MIN_POINTS,
              "thresholds": asdict(scoring.DEFAULT_THRESHOLDS),
              "baseline": {"window": backtest.BASELINE_WINDOW, "threshold": backtest.BASELINE_THRESHOLD}}
    try:
        if args.input:
            cases = json.loads(args.input.read_text())
            rows = []
            for case in cases:
                metrics = [{**row, "metric_date": date.fromisoformat(row["metric_date"])} for row in case["metrics"]]
                row = backtest.evaluate_metrics(case["term"], metrics, date.fromisoformat(case["peak"]) if case["peak"] else None)
                row["metrics"] = case["metrics"]
                rows.append(row)
            summary = backtest.summarize(rows)
            params["mode"] = "production-replay"
        else:
            if not (settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET):
                print("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 없습니다. 다중 출처 재생은 --input을 사용하세요.")
                return 1
            with httpx.Client(timeout=20) as client:
                rows, summary = run(client, unit)
            params.update(mode="single-source-exploration", validation_set=VALIDATION_SET)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"평가 실패: {type(error).__name__}")
        return 1
    print(f"평가 모드: {params['mode']} / 계산 버전: {signals.VERSION}")
    report(rows, summary)
    if not args.no_save:
        db.wait_for_db()
        db.apply_schema()
        with db.conn() as c:
            run_id = c.execute(
                "INSERT INTO backtest_runs (params, rows, summary) VALUES (%s, %s, %s) RETURNING id",
                (json.dumps(params, ensure_ascii=False), json.dumps(rows, ensure_ascii=False), json.dumps(summary, ensure_ascii=False)),
            ).fetchone()["id"]
        print(f"결과를 backtest_runs {run_id}번으로 보관했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""모든 키워드의 트렌드 분석(감성 → 점수 → 브리핑)을 갱신한다.

실행: python -m app.jobs.run_trend        (쿠버네티스에서는 CronJob 이 매일, 수집 작업 뒤에 실행)
한 키워드가 실패해도 나머지를 계속하고, 실패가 있었으면 종료 코드 1 로 끝나 CronJob 기록에 실패로 남는다.
"""
from __future__ import annotations

import sys

from app import db
from app.trend import pipeline


def main() -> int:
    """모든 키워드를 차례로 분석하고 한 줄씩 결과를 찍는다. 반환값은 종료 코드: 실패한 키워드가 있으면 1."""
    db.wait_for_db()
    db.apply_schema()
    with db.conn() as c:
        keywords = c.execute("SELECT id, term FROM keywords ORDER BY id").fetchall()
    failed = 0
    for keyword in keywords:
        try:
            state = pipeline.run(keyword["id"])
        except Exception as error:  # noqa: BLE001 - 어떤 실패든 다음 키워드로 넘어간다
            failed += 1
            print(f"분석 실패: {keyword['term']} — {error}")
            continue
        if "score" not in state:
            print(f"지표 없음(건너뜀): {keyword['term']}")
            continue
        made = state["insight"]["generated_by"] if state.get("insight") else "없음"
        print(f"분석 완료: {keyword['term']} — {state['score']['score']}점 {state['score']['stage']} (브리핑: {made})")
    print(f"키워드 {len(keywords)}개 중 실패 {failed}개")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

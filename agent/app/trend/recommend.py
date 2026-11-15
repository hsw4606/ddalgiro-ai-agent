"""입점업체 맞춤 트렌드 추천 (안건 1 · 기획 02 §6) — 뜨는 아이템 가운데 그 업체에 맞는 것을 고른다.

쓰는 곳: API 의 GET /v1/vendors/recommendations?vendor=업체명 (main.py).
방법:   저장해 둔 최신 트렌드 점수에 "업체 적합도" 와 "진입 시기" 를 곱해 순서를 정한다. 모두 코드 계산이다.
        요청할 때 LLM 을 부르지 않으므로 응답이 빠르다(브리핑은 매일 분석 작업이 만들어 둔 것을 붙인다).
업체 구분: 딸기로 시스템에는 업체 계정이 없어 상품명 앞 [업체명] 의 업체명을 그대로 키로 쓴다.
"""
from __future__ import annotations

from typing import Any

from app import db

# 확산 단계별 "지금 들어가기 좋은 정도" 와 업체에 주는 권고 문구.
# 성장기 진입 직후가 추천할 때이고, 정점은 이미 늦었다는 기획 02 §5.3.2 의 판단을 숫자로 옮긴 것이다.
STAGE_TIMING: dict[str, tuple[float, str]] = {
    "성장기": (1.0, "증가가 빨라지는 중입니다. 시험 판매나 한정 수량 출시를 지금 검토할 만합니다."),
    "태동기": (0.5, "아직 신호가 약합니다. 재료·레시피를 알아보며 지켜보는 단계입니다."),
    "정점": (0.4, "이미 크게 오른 뒤 증가가 멈췄습니다. 새로 시작한다면 재고를 최소로 잡으세요."),
    "쇠퇴기": (0.1, "관심이 줄고 있습니다. 새 투자는 권하지 않습니다."),
    # 최근 10일의 수집이 온전하지 않은 키워드(pipeline.NOT_ENOUGH). 점수가 0 이라 순위 맨 아래에 온다
    "자료부족": (0.0, "최근 10일의 수집이 완료되지 않아 판단할 자료가 부족합니다."),
}
# 성장기로 보이지만 글·검색·SNS 신호 가운데 한 가지만 늘 때(교차검증 '불일치')의 시기 점수와 권고.
# 홍보로 글이나 SNS 만 늘어난 것일 수 있어, 지켜보는 단계(태동기)와 같은 무게로 낮춘다.
HOLD_TIMING: tuple[float, str] = (0.5, "글·검색·SNS 신호 가운데 한 가지만 늘고 있습니다. 다른 신호도 함께 오르는지 며칠 더 지켜본 뒤 정하세요.")
FIT_MATCH = 1.0       # 업체가 만드는 분류와 같은 아이템
FIT_OTHER = 0.4       # 다른 분류의 아이템 (만들 수는 있겠지만 우선순위를 낮춘다)
FIT_NO_PROFILE = 0.7  # 업체 프로필이 없어 적합도를 알 수 없을 때


def recommend(vendor_key: str, top_n: int = 5) -> dict[str, Any]:
    """업체 하나에 대한 추천 목록을 만든다.

    입력: vendor_key 는 업체명, top_n 은 돌려줄 개수.
    반환: {"vendor_key", "profile"(없으면 None), "items": [...]}.
          items 의 각 항목에는 점수 내역(트렌드 점수·적합도·시기)이 그대로 들어 있어 순위의 이유를 확인할 수 있다.
    """
    with db.conn() as c:
        profile = c.execute(
            "SELECT vendor_key, categories, region, note FROM vendor_profiles WHERE vendor_key = %s", (vendor_key,)
        ).fetchone()
        # 키워드마다 가장 최근 날짜의 점수 한 줄씩 (DISTINCT ON 은 묶음마다 첫 줄만 남긴다)
        rows = c.execute(
            """
            SELECT DISTINCT ON (s.keyword_id)
                   k.id AS keyword_id, k.term, k.category, k.is_sample,
                   s.score_date, s.score, s.stage, s.growth_rate, s.mentions_7d, s.signal_check,
                   i.briefing
            FROM trend_scores s
            JOIN keywords k ON k.id = s.keyword_id
            LEFT JOIN trend_insights i ON i.keyword_id = s.keyword_id AND i.score_date = s.score_date
            ORDER BY s.keyword_id, s.score_date DESC
            """
        ).fetchall()

    categories = set(profile["categories"]) if profile else set()
    items = []
    for row in rows:
        # 적합도: 프로필이 없으면 중간값, 있으면 분류가 같은지로 가른다
        if not categories:
            fit = FIT_NO_PROFILE
        else:
            fit = FIT_MATCH if row["category"] in categories else FIT_OTHER
        timing, advice = STAGE_TIMING[row["stage"]]
        if row["stage"] == "성장기" and row["signal_check"] == "불일치":
            timing, advice = HOLD_TIMING
        items.append(
            {
                "keyword_id": row["keyword_id"],
                "term": row["term"],
                "category": row["category"],
                "is_sample": row["is_sample"],
                "score_date": row["score_date"].isoformat(),
                "stage": row["stage"],
                "trend_score": row["score"],
                "growth_percent": round(row["growth_rate"] * 100),
                "mentions_7d": row["mentions_7d"],
                "signal_check": row["signal_check"],
                "fit": fit,
                "timing": timing,
                # 최종 순위 점수 = 트렌드 점수 × 적합도 × 시기
                "final_score": round(row["score"] * fit * timing, 1),
                "advice": advice,
                "briefing": row["briefing"],
            }
        )
    items.sort(key=lambda item: item["final_score"], reverse=True)
    return {
        "vendor_key": vendor_key,
        "profile": dict(profile) if profile else None,
        "items": items[:top_n],
    }

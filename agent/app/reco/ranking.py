"""후보의 순위를 정한다 — 여러 신호를 가중해 더하는 순수 계산 (안건 2 · 기획 03 §6). LLM 을 쓰지 않는다.

쓰는 곳: reco/graph.py 의 rank 단계.
점수(0~1) = 적합도×w + 품질×w + 지역·배송×w + 공급 능력×w + 트렌드×w (+ 입점사 가산, 상한 있음)
원칙:
  - 1순위 기준은 "문의에 얼마나 잘 맞는가(적합도)" 다. 적합도를 먼저 정렬하고, 같은 적합도 안에서 다른 신호를 비교한다.
  - 입점사 가산은 별도 항목으로 더하고 상한(PARTNER_BOOST_CAP)을 둔다. 같은 적합도 안에서만 순서를 바꾸며, 상위 결과를 보존하지 못하면 가산을 철회한다.
    가산 때문에 상위 결과가 얼마나 바뀌었는지(보존율)를 요청마다 재서 남긴다(boost_audit).
  - 신호별 점수를 결과에 그대로 남긴다. 순위가 왜 그렇게 나왔는지 나중에 검증할 수 있어야 한다.
가중치는 처음 정한 규칙값이다. 선택·주문 기록(quote_outcomes)이 쌓이면 그 자료로 다시 맞춘다. 그때도 이 파일의 rank 만 바꾸면 된다.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app import dates
from app.reco import capacity
from app.reco.slots import Slots, is_group_order, order_quantity
from app.settings import settings

# 용도별 가중치 묶음(합은 1). 선물은 품질·평판을, 단체주문은 지역·배송과 공급 능력을 더 본다.
WEIGHTS: dict[str, dict[str, float]] = {
    "기본": {"fit": 0.50, "quality": 0.25, "region": 0.10, "supply": 0.05, "trend": 0.10},
    "선물": {"fit": 0.45, "quality": 0.35, "region": 0.10, "supply": 0.05, "trend": 0.05},
    "단체주문": {"fit": 0.40, "quality": 0.15, "region": 0.15, "supply": 0.20, "trend": 0.10},
}
# 후기가 적은 업체의 평점을 믿는 정도를 낮추기 위한 기준값: 평균 3.5점짜리 후기 10건을 미리 가진 것으로 친다
PRIOR_RATING = 3.5
PRIOR_REVIEWS = 10
# 문의에 메뉴·업체 이름의 낱말이 그대로 나왔을 때 적합도에 더하는 값. 낱말 하나에 0.1, 세 낱말까지만 센다
KEYWORD_BONUS = 0.1
KEYWORD_BONUS_MAX_HITS = 3
# 지역이 맞은 방식별 점수: 업체가 그 지역에 있음 / 그 지역으로 배송함 / 전국 배송뿐
REGION_SCORES = {"local": 1.0, "delivery": 0.8, "nationwide": 0.6}
# 공급 조건이 오래됐거나 확인 전이면 공급 능력 점수를 이만큼만 믿는다
STALE_SUPPLY_FACTOR = 0.5


def fit_signal(candidate: dict[str, Any]) -> float:
    """적합도 0~1 = 의미 유사도 + 이름이 문의에 그대로 나온 만큼의 가산.

    유사도는 드물게 음수가 나올 수 있어 0~1 로 자른다. 가산은 최대 KEYWORD_BONUS × KEYWORD_BONUS_MAX_HITS 다.
    """
    similarity = max(0.0, min(float(candidate["similarity"]), 1.0))
    hits = min(int(candidate.get("keyword_hits") or 0), KEYWORD_BONUS_MAX_HITS)
    return min(1.0, similarity + KEYWORD_BONUS * hits)


def quality_signal(rating: float | None, review_count: int) -> float:
    """품질·평판 점수 0~1. 후기 수가 적을수록 기준 평점(3.5) 쪽으로 당겨진다.

    예: 후기 2건에 5.0점인 업체가 후기 300건에 4.7점인 업체보다 위로 가지 않게 한다.
    """
    if rating is None:
        return PRIOR_RATING / 5
    adjusted = (rating * review_count + PRIOR_RATING * PRIOR_REVIEWS) / (review_count + PRIOR_REVIEWS)
    return max(0.0, min(adjusted / 5, 1.0))


def region_signal(candidate: dict[str, Any]) -> float:
    """지역·배송 점수 0~1. 검색이 붙여 준 region_match(지역이 맞은 방식)를 점수로 바꾼다. 표시가 없으면 같은 지역으로 본다."""
    return REGION_SCORES.get(candidate.get("region_match") or "local", REGION_SCORES["local"])


def supply_signal(candidate: dict[str, Any], slots: Slots, today: date | None = None) -> float:
    """공급 능력 점수 0~1. 단체주문에서만 차이가 난다.

    요청 수량이 하루 최대 생산량에서 차지하는 비율이 작을수록(여유가 클수록) 점수가 높다.
    공급 조건이 오래됐거나 확인 전이면 그 여유를 다 믿을 수 없어 STALE_SUPPLY_FACTOR 를 곱한다.
    단체주문이 아니면 공급 조건을 따지지 않으므로 모든 후보가 똑같이 1.0 이다.
    """
    quantity = order_quantity(slots)
    if not is_group_order(slots) or not quantity or not candidate.get("max_qty_per_day"):
        return 1.0
    headroom = max(0.0, 1.0 - quantity / candidate["max_qty_per_day"])
    fresh = capacity.is_fresh(candidate, today or dates.today())
    return headroom if fresh else headroom * STALE_SUPPLY_FACTOR


def trend_signal(candidate: dict[str, Any], trend_scores: dict[str, float]) -> float:
    """트렌드 점수 0~1. 메뉴 이름·설명에 들어 있는 트렌드 키워드 가운데 가장 높은 점수를 쓴다(안건 1 의 점수)."""
    text = f"{candidate['menu_name']} {candidate.get('description') or ''}"
    matched = [score for term, score in trend_scores.items() if term in text]
    return max(matched) / 100 if matched else 0.0


def profile(slots: Slots) -> str:
    """가중치 묶음의 이름을 고른다. 대량 선물·행사도 공급 능력을 비교해야 하므로 검색과 같은 단체주문 판단을 쓴다."""
    purpose = "단체주문" if is_group_order(slots) else slots.occasion or "기본"
    return purpose if purpose in WEIGHTS else "기본"


def rank(candidates: list[dict[str, Any]], slots: Slots, trend_scores: dict[str, float],
         today: date | None = None) -> list[dict[str, Any]]:
    """후보에 점수를 매겨 높은 순으로 돌려준다. 업체마다 가장 점수가 높은 메뉴 하나만 남긴다.

    입력: candidates 는 search.search 의 결과, trend_scores 는 {키워드: 트렌드 점수 0~100}, today 는 공급 조건의 유효기간을 볼 기준일.
    반환: 후보 사전에 "score"(최종 점수)와 "score_detail"(가중치 묶음·신호별 점수·가중치·입점사 가산)을 더한 목록.
    """
    name = profile(slots)
    weights = WEIGHTS[name]
    scored = []
    for candidate in candidates:
        signals = {
            "fit": fit_signal(candidate),
            "quality": quality_signal(candidate.get("rating"), candidate.get("review_count") or 0),
            "region": region_signal(candidate),
            "supply": supply_signal(candidate, slots, today),
            "trend": trend_signal(candidate, trend_scores),
        }
        organic = sum(weights[key] * value for key, value in signals.items())
        boost = max(0.0, min(settings.PARTNER_BOOST_CAP, 0.03)) if candidate.get("is_partner") else 0.0
        scored.append(
            {
                **candidate,
                "score": round(min(1.0, organic + boost), 4),
                "score_detail": {
                    "profile": name,
                    "signals": {key: round(value, 4) for key, value in signals.items()},
                    "weights": weights,
                    "organic": round(organic, 4),   # 가산 전 점수
                    "partner_boost": boost,
                },
            }
        )
    # 적합도 우선 순서를 가산으로 뒤집지 않는다. 같은 적합도 안에서만 가중 점수를 비교한다.
    scored.sort(key=lambda item: _sort_key(item, boosted=True), reverse=True)
    best_per_shop: dict[int, dict[str, Any]] = {}
    for item in scored:
        # 이미 높은 순으로 정렬돼 있으므로 업체별로 처음 만난 메뉴가 그 업체의 최고 점수 메뉴다
        best_per_shop.setdefault(item["shop_id"], item)
    ranked = list(best_per_shop.values())
    # 표시할 상위 결과의 95% 이상을 지키지 못하면 이번 요청 전체의 상업 가산을 철회한다.
    if boost_audit(ranked, settings.RECO_RESULTS)["preservation"] < 0.95:
        for item in ranked:
            item["score"] = item["score_detail"]["organic"]
            item["score_detail"]["partner_boost"] = 0.0
            item["score_detail"]["boost_withheld"] = True
        ranked.sort(key=lambda item: _sort_key(item, boosted=False), reverse=True)
    return ranked


def _sort_key(item: dict[str, Any], boosted: bool) -> tuple:
    """적합도 → 점수 → 고정 번호 순서. 동점에서도 입력 순서가 순위를 바꾸지 않는다."""
    detail = item["score_detail"]
    return (fit_signal(item), item["score"] if boosted else detail["organic"],
            -item["shop_id"], -item.get("menu_id", 0))


def boost_audit(ranked: list[dict[str, Any]], top_n: int) -> dict[str, Any]:
    """입점사 가산이 상위 결과를 얼마나 바꿨는지 잰다(상업 가산의 감사 기록).

    입력: ranked 는 rank 의 결과(최종 점수 순), top_n 은 화면에 내보내는 개수.
    반환: {"profile": 가중치 묶음, "top_n": 비교한 개수, "preservation": 보존율 0~1, "boosted_in": 가산 덕분에 상위에 든 업체 번호}.
          보존율 = 가산 전 점수로 뽑은 상위 top_n 업체 가운데 가산 뒤에도 상위에 남은 비율. 후보가 없으면 1.0.
    """
    final_top = [item["shop_id"] for item in ranked[:top_n]]
    by_organic = sorted(ranked, key=lambda item: _sort_key(item, boosted=False), reverse=True)
    organic_top = [item["shop_id"] for item in by_organic[:top_n]]
    kept = [shop_id for shop_id in organic_top if shop_id in final_top]
    return {
        "profile": ranked[0]["score_detail"]["profile"] if ranked else None,
        "top_n": len(final_top),
        "preservation": round(len(kept) / len(organic_top), 4) if organic_top else 1.0,
        "boosted_in": [shop_id for shop_id in final_top if shop_id not in organic_top],
    }

"""DB 와 LLM 없이 도는 규칙 시험 — 점수 계산·시간창·교차검증·백테스팅, 근거 검사, 순위·보존율, 조건 정리,
공급 조건 최신성·견적, 검색 순위 합치기, 수집 집계·광고 표시, 출처 목록과 인스타그램·RSS 수집(가짜 HTTP 응답).

실행(agent 폴더에서): python -m pytest tests/test_rules.py -q
여기서 확인하는 것은 "숫자와 판단은 코드가 한다" 는 원칙의 코드 쪽이다.
LLM·DB 까지 이어서 확인하는 시험은 tests/test_live.py 에 있다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import collecting, dates, grounding
from app.jobs import collect_instagram, collect_naver, collect_rss, seed_sample
from app.reco import capacity, explain, proposal, ranking, search, slots as slot_rules
from app.reco.slots import Slots
from app.trend import backtest, pipeline, scoring, sources, signals


# ───────────────────────────── 트렌드 점수 ─────────────────────────────

def test_growth_rate_needs_four_points():
    """값이 4개 미만이면 판단하지 않고 0 을 돌려준다."""
    assert scoring.growth_rate([1, 2, 3]) == 0.0


def test_growth_rate_compares_recent_three_with_previous_four():
    """최근 3일 평균(20)이 직전 4일 평균(10)의 두 배면 증가율은 1.0(100%)이다."""
    assert scoring.growth_rate([10, 10, 10, 10, 20, 20, 20]) == 1.0


def test_growth_rate_of_all_zero_series_is_zero():
    """계속 0건이면 증가율은 0 이다(-100% 가 아니다). 0 에서 새로 생긴 신호는 큰 증가로 본다."""
    assert scoring.growth_rate([0.0] * 10) == 0.0
    assert scoring.growth_rate([0.0] * 7 + [5.0] * 3) > 1.0


def test_sample_series_cover_every_stage():
    """샘플 시계열이 의도한 확산 단계로 판정되는지 — 단계 판정 규칙과 샘플 자료를 함께 지킨다."""
    stages = {term: scoring.stage([float(v) for v in series]) for term, (_category, series) in seed_sample.SERIES.items()}
    assert stages == {
        "(샘플) 피스타치오 크림빵": "성장기",
        "(샘플) 과일 찹쌀떡": "정점",
        "(샘플) 바스크 치즈케이크": "쇠퇴기",
        "(샘플) 버터 쿠키": "태동기",
    }


def test_trend_score_is_bounded_and_rewards_acceleration():
    """점수는 0~100 이고, 가속 중인 시계열이 변화 없는 시계열보다 높다."""
    growing = [float(v) for v in seed_sample.SERIES["(샘플) 피스타치오 크림빵"][1]]
    flat = [30.0] * 30
    high = scoring.trend_score(growing, growing, 0.8, 3)
    low = scoring.trend_score(flat, flat, 0.8, 3)
    assert 0 <= low < high <= 100


# ───────────────────────────── 근거 검사 ─────────────────────────────

EVIDENCE: grounding.Evidence = {
    "M1": {"text": "트렌드 점수는 80.7점이다 (기준일 2026-11-13).", "url": None},
    "M5": {"text": "최근 7일 언급 수 합계는 1,240건이다.", "url": None},
}


def _text(*sentences: tuple[str, list[str]]) -> grounding.GroundedText:
    """(문장, 출처 번호 목록) 쌍들로 검사 대상 글을 만든다."""
    return grounding.GroundedText(sentences=[grounding.Sentence(text=t, sources=s) for t, s in sentences])


def test_grounded_text_passes():
    """검증한 근거 문장을 그대로 선택하고 출처를 달면 통과한다."""
    answer = _text((EVIDENCE["M1"]["text"], ["M1"]), (EVIDENCE["M5"]["text"], ["M5"]))
    assert grounding.check(answer, EVIDENCE) == []


def test_sentence_without_source_is_rejected():
    """출처를 달지 않은 문장은 불합격이다."""
    assert grounding.check(_text(("요즘 인기가 많습니다.", [])), EVIDENCE) != []


def test_unknown_source_is_rejected():
    """근거 목록에 없는 번호(M9)를 출처로 대면 불합격이다."""
    assert grounding.check(_text(("점수는 80.7점입니다.", ["M9"])), EVIDENCE) != []


def test_invented_number_is_rejected():
    """근거에 없는 숫자(3배)를 쓰면 막는다."""
    assert grounding.check(_text(("검색량이 3배 늘었습니다.", ["M1"])), EVIDENCE) != []


def test_number_from_other_source_is_rejected():
    """숫자는 그 문장이 가리킨 근거에 있어야 한다. 다른 근거의 숫자를 끌어다 쓰면 막는다."""
    assert grounding.check(_text(("언급은 1,240건입니다.", ["M1"])), EVIDENCE) != []


def test_empty_answer_is_rejected():
    """문장이 하나도 없는 답은 불합격이다."""
    assert grounding.check(grounding.GroundedText(sentences=[]), EVIDENCE) != []


def test_render_appends_sources():
    """통과한 글은 문장 끝에 [출처:번호] 를 붙인 모양으로 나간다."""
    assert grounding.render(_text(("점수는 80.7점입니다.", ["M1"]))) == "점수는 80.7점입니다. [출처:M1]"


# ───────────────────────────── 순위 ─────────────────────────────

def _candidate(shop_id: int, similarity: float, **extra) -> dict:
    """순위 시험에 쓸 후보 한 건을 만든다. extra 로 준 칸만 기본값을 바꾼다."""
    base = {"shop_id": shop_id, "menu_id": shop_id * 10, "menu_name": "케이크", "description": "", "similarity": similarity,
            "rating": 4.5, "review_count": 100, "is_partner": False, "max_qty_per_day": None}
    return {**base, **extra}


def test_quality_signal_distrusts_few_reviews():
    """후기 2건의 5.0점보다 후기 300건의 4.7점이 높게 나와야 한다."""
    assert ranking.quality_signal(5.0, 2) < ranking.quality_signal(4.7, 300)


def test_partner_boost_breaks_near_ties_only():
    """입점사 가산은 작거나 큰 적합도 차이 모두 뒤집지 못한다."""
    slots = Slots(region="강남")
    near = ranking.rank([_candidate(1, 0.700), _candidate(2, 0.695, is_partner=True)], slots, {})
    far = ranking.rank([_candidate(1, 0.80), _candidate(2, 0.60, is_partner=True)], slots, {})
    assert [item["shop_id"] for item in near] == [1, 2]
    assert [item["shop_id"] for item in far] == [1, 2]


def test_rank_keeps_best_menu_per_shop():
    """같은 업체의 메뉴가 여러 개면 점수가 가장 높은 하나만 남긴다."""
    ranked = ranking.rank([_candidate(1, 0.5, menu_id=11), _candidate(1, 0.9, menu_id=12)], Slots(region="강남"), {})
    assert [item["menu_id"] for item in ranked] == [12]


def test_supply_signal_prefers_headroom_for_group_orders():
    """단체주문에서는 생산 여유가 큰 업체의 공급 점수가 높다."""
    slots = Slots(region="강남", occasion="단체주문", quantity=300, due_date="2099-01-01")
    tight = ranking.supply_signal(_candidate(1, 0.5, max_qty_per_day=300), slots)
    roomy = ranking.supply_signal(_candidate(2, 0.5, max_qty_per_day=3000), slots)
    assert tight < roomy


def test_trend_signal_uses_matching_keyword():
    """메뉴 이름에 들어 있는 키워드(피스타치오)의 점수만 쓰고, 들어 있지 않은 키워드(약과)는 쓰지 않는다."""
    candidate = _candidate(1, 0.5, menu_name="피스타치오 크림빵")
    assert ranking.trend_signal(candidate, {"피스타치오": 80.0, "약과": 99.0}) == 0.8


# ───────────────────────────── 조건 정리·되묻기 ─────────────────────────────

TODAY = date(2026, 11, 14)


def test_normalize_drops_unknown_diet_and_past_date():
    """표준 표기에 없는 식이 조건과 이미 지난 날짜는 버린다."""
    cleaned = slot_rules.normalize(Slots(diet=["비건", "키토"], due_date="2026-01-01", max_price=-5), "2026년 1월 1일까지 비건 키토", TODAY)
    assert cleaned.diet == ["비건"] and cleaned.due_date is None and cleaned.max_price is None


def test_normalize_drops_values_the_customer_never_said():
    """LLM 이 채워 넣었지만 문의 글에 근거가 없는 값(오늘 날짜·수량 1·단체주문·빈 지역)은 버린다."""
    guessed = Slots(dessert="케이크", region="", occasion="단체주문", quantity=1, due_date="2026-11-14")
    cleaned = slot_rules.normalize(guessed, "안 단 비건 생일 케이크 4만 원 이하로 추천해줘", TODAY)
    assert (cleaned.region, cleaned.occasion, cleaned.quantity, cleaned.due_date) == (None, None, None, None)
    assert slot_rules.missing(cleaned) == ["region"]


def test_normalize_keeps_values_the_customer_said():
    """문의 글에 적힌 수량·날짜·단체주문은 그대로 둔다."""
    text = "회사 행사 답례품으로 떡 200개, 강남으로 12월 10일까지"
    said = Slots(dessert="떡", region="강남", occasion="단체주문", quantity=200, due_date="2026-12-10")
    cleaned = slot_rules.normalize(said, text, TODAY)
    assert (cleaned.occasion, cleaned.quantity, cleaned.due_date) == ("단체주문", 200, "2026-12-10")


def test_large_quantity_becomes_group_order():
    """수량이 20개 이상이면 용도를 말하지 않아도 단체주문으로 본다."""
    cleaned = slot_rules.normalize(Slots(quantity=200), "쿠키 200개 주문", TODAY)
    assert cleaned.occasion == "단체주문"


def test_missing_region_always_asked():
    """지역이 없으면 항상 지역을 되물어야 한다."""
    assert slot_rules.missing(Slots(dessert="케이크")) == ["region"]


def test_group_order_needs_quantity_date_and_storage():
    """단체주문인데 수량·날짜·보관 방법이 없으면 모두 되묻는다."""
    lacking = slot_rules.missing(Slots(region="강남", occasion="단체주문"))
    assert lacking == ["quantity", "due_date", "storage"]
    question = slot_rules.reask_question(lacking)
    assert "수량" in question and "날짜" in question and "보관" in question


def test_complete_request_needs_nothing():
    """필요한 조건이 모두 있으면 되물을 것이 없다. 보관은 '무관' 이라고 답해도 채워진 것이다."""
    full = Slots(region="강남", occasion="단체주문", quantity=100, due_date="2026-12-01", storage="무관")
    assert slot_rules.missing(full) == []


def test_storage_question_can_be_turned_off(monkeypatch):
    """설정 RECO_ASK_STORAGE 를 끄면 보관 방법은 되묻지 않는다."""
    monkeypatch.setattr(slot_rules.settings, "RECO_ASK_STORAGE", False)
    assert slot_rules.missing(Slots(region="강남", occasion="단체주문", quantity=100, due_date="2026-12-01")) == []


def test_headcount_counts_as_order_quantity():
    """인원만 말해도 한 사람에 하나로 보고 수량을 채운다. 20명 이상이면 단체주문이다."""
    said = Slots(region="판교", headcount=200, due_date="2026-11-20", storage="냉장")
    cleaned = slot_rules.normalize(said, "다음 주 금요일 임직원 200명 간식, 판교 수령, 냉장 보관 가능", TODAY)
    assert slot_rules.order_quantity(cleaned) == 200 and cleaned.occasion == "단체주문"
    assert slot_rules.missing(cleaned) == []


def test_price_cap_takes_the_strictest_budget():
    """가격 상한은 1개 가격·1인 예산·(총예산 ÷ 수량) 가운데 가장 작은 값이다. 조건이 없으면 None."""
    assert slot_rules.price_cap(Slots()) is None
    assert slot_rules.price_cap(Slots(max_price=40000)) == 40000
    assert slot_rules.price_cap(Slots(headcount=200, budget_per_person=8000)) == 8000
    # 총예산 100만 원 ÷ 150개 = 6666.6… → 예산을 넘지 않게 나머지를 버려 6666
    assert slot_rules.price_cap(Slots(quantity=150, total_budget=1000000, budget_per_person=8000)) == 6666


def test_total_budget_is_computed_by_code():
    """총예산을 말하지 않았으면 1인 예산 × 인원으로 코드가 계산한다. 말한 총예산이 있으면 그 값을 쓴다."""
    assert slot_rules.total_budget(Slots(headcount=200, budget_per_person=8000)) == 1600000
    assert slot_rules.total_budget(Slots(headcount=200, budget_per_person=8000, total_budget=1500000)) == 1500000
    assert slot_rules.total_budget(Slots(budget_per_person=8000)) is None


def test_normalize_drops_group_conditions_the_customer_never_said():
    """문의에 없는 인원·예산·보관·포장·세금계산서·목적은 LLM 이 채워도 버린다."""
    guessed = Slots(region="강남", headcount=50, budget_per_person=8000, total_budget=400000, storage="냉장",
                    individual_packaging=True, gift_packaging=True, tax_invoice=True, purpose="사내 행사")
    cleaned = slot_rules.normalize(guessed, "강남에서 쿠키 추천해줘", TODAY)
    assert (cleaned.headcount, cleaned.budget_per_person, cleaned.total_budget, cleaned.storage) == (None, None, None, None)
    assert (cleaned.individual_packaging, cleaned.gift_packaging, cleaned.tax_invoice, cleaned.purpose) == (None, None, None, None)


def test_normalize_keeps_group_conditions_the_customer_said():
    """문의에 실제로 나온 인원·예산·보관·포장·세금계산서·목적은 그대로 둔다."""
    text = "사내 행사 간식 임직원 200명, 1인 8천 원, 상온 보관만 가능, 개별 포장하고 선물 포장도, 세금계산서 발행, 판교로 11월 20일"
    said = Slots(region="판교", headcount=200, budget_per_person=8000, storage="상온", individual_packaging=True,
                 gift_packaging=True, tax_invoice=True, purpose="사내 행사 간식", due_date="2026-11-20")
    cleaned = slot_rules.normalize(said, text, TODAY)
    assert (cleaned.headcount, cleaned.budget_per_person, cleaned.storage) == (200, 8000, "상온")
    assert (cleaned.individual_packaging, cleaned.gift_packaging, cleaned.tax_invoice) == (True, True, True)
    assert cleaned.purpose == "사내 행사 간식" and slot_rules.storage_level(cleaned) == 0


@pytest.mark.parametrize("text, expected", [
    ("쿠키 4만 원 이하", True), ("1인 8천원", True), ("예산은 넉넉해요", True), ("만 원짜리 쿠키", True),
    ("강남에서 쿠키 추천해줘", False), ("직접 만들어 주는 곳", False),
])
def test_money_hint_ignores_ordinary_words(text, expected):
    """'추천'·'만들어' 같은 낱말은 돈 이야기가 아니다. 숫자 뒤의 단위나 '만 원' 같은 모양만 돈으로 본다."""
    assert bool(slot_rules.MONEY_HINT.search(text)) is expected


@pytest.mark.parametrize("text, expected", [
    ("2099년 12월 10일에 받을게요", date(2099, 12, 10)),
    ("2026-12-10 까지", date(2026, 12, 10)),
    ("12월 10일까지 받아야 해요", date(2026, 12, 10)),
    ("1월 5일까지", date(2027, 1, 5)),                 # 올해 1월 5일은 지났으므로 내년
    ("내일 받을 수 있나요", date(2026, 11, 15)),
    ("금요일까지 부탁해요", date(2026, 11, 20)),        # 기준일(토요일)에서 가장 가까운 금요일
    ("토요일 수령", date(2026, 11, 14)),                # 오늘이 그 요일이면 오늘
    ("이번 주 금요일", date(2026, 11, 13)),             # 이미 지난 날 — 뒤에서 '지난 날짜' 로 버려진다
    ("다음 주 금요일 임직원 200명 간식", date(2026, 11, 20)),
    ("다음주 월요일", date(2026, 11, 16)),
    ("다다음 주 수요일", date(2026, 11, 25)),
])
def test_resolve_date_computes_by_code(text, expected):
    """날짜 표현은 코드가 계산한다. 기준일은 2026-11-14(토요일)이고 한 주는 월요일에 시작한다."""
    assert slot_rules.resolve_date(text, TODAY) == expected


@pytest.mark.parametrize("text", [
    "비건 케이크 추천해줘",                      # 날짜를 말하지 않음
    "12월 3일 행사인데 12월 1일까지 받아야 해요",   # 날짜가 둘이라 어느 것인지 코드가 정하지 않는다
    "12월 10일 행사 전날까지",                   # 말한 날짜에서 다시 계산해야 한다
    "일주일 뒤에 필요해요",
    "케이크 1/2 조각만 주문할게요",              # 분수는 날짜가 아니다
    "생일 당일에 받고 싶어요",                   # '당일' 이 오늘이라는 보장이 없다
    "13월 40일까지",                            # 없는 날짜
])
def test_resolve_date_leaves_unclear_cases_to_the_model(text):
    """날짜가 없거나, 둘 이상이거나, 한 번 더 계산해야 하는 문의는 코드가 읽지 않는다(None)."""
    assert slot_rules.resolve_date(text, TODAY) is None


def test_code_date_overrides_a_wrong_model_date():
    """LLM 이 연도나 요일을 틀리게 적어도, 문의에서 읽은 날짜로 바로잡는다. 비워 둔 날짜도 채운다."""
    wrong_year = slot_rules.normalize(Slots(region="강남", due_date="2029-12-10"), "2099년 12월 10일에 강남으로", TODAY)
    assert wrong_year.due_date == "2099-12-10"
    wrong_weekday = slot_rules.normalize(Slots(region="판교", due_date="2026-11-17"), "다음 주 금요일 판교 수령", TODAY)
    assert wrong_weekday.due_date == "2026-11-20"
    forgotten = slot_rules.normalize(Slots(region="판교"), "다음 주 금요일 판교 수령", TODAY)
    assert forgotten.due_date == "2026-11-20"


@pytest.mark.parametrize("text, expected", [
    ("1인 8천 원, 총 160만 원", {8000, 1600000}), ("8,000원짜리", {8000}), ("3만5천원 이하", {35000}),
    ("1.5만 원", {15000}), ("임직원 200명에게 쿠키 150개", set()), ("팔천 원", set()),
])
def test_money_amounts_reads_digits_with_units(text, expected):
    """숫자로 적은 금액만 원 단위로 읽는다. 인원·개수는 금액이 아니고, 한글로 쓴 금액은 읽지 않는다."""
    assert slot_rules.money_amounts(text) == expected


def test_computed_total_budget_is_dropped_and_recomputed_by_code():
    """LLM 이 1인 예산 × 인원을 스스로 곱해 넣은 총예산은 버리고, 코드가 다시 계산한다."""
    text = "임직원 200명 간식, 1인 8천 원 예산, 강남"
    cleaned = slot_rules.normalize(Slots(region="강남", headcount=200, budget_per_person=8000, total_budget=1700000), text, TODAY)
    assert cleaned.total_budget is None and slot_rules.total_budget(cleaned) == 1600000
    # 고객이 직접 말한 총예산은 그대로 둔다
    said = slot_rules.normalize(Slots(region="강남", headcount=200, total_budget=1500000), "200명 간식, 총예산 150만 원, 강남", TODAY)
    assert said.total_budget == 1500000


def test_per_person_budget_in_the_wrong_slot_is_moved():
    """1인 예산을 1개 가격 칸에 넣은 답은 1인 예산으로 옮긴다. '개당' 이라고 말한 가격은 옮기지 않는다."""
    moved = slot_rules.normalize(Slots(region="강남", headcount=200, max_price=8000), "임직원 200명 간식, 1인 8천 원, 강남", TODAY)
    assert (moved.budget_per_person, moved.max_price) == (8000, None)
    kept = slot_rules.normalize(Slots(region="강남", headcount=200, max_price=4000), "200명에게 인당 2개씩, 개당 4천 원 이하, 강남", TODAY)
    assert (kept.budget_per_person, kept.max_price) == (None, 4000)


def test_storage_level_ignores_unknown_and_indifferent():
    """보관을 말하지 않았거나 '무관' 이면 보관으로 거르지 않는다(None)."""
    assert slot_rules.storage_level(Slots()) is None and slot_rules.storage_level(Slots(storage="무관")) is None
    assert slot_rules.storage_level(Slots(storage="냉장")) == 1


# ───────────────────────────── 수집 집계 ─────────────────────────────

def test_complete_day_counts_drops_today_and_oldest_day():
    """오늘(아직 안 끝남)과 가장 오래된 날(중간에 잘렸을 수 있음)은 세지 않는다."""
    items = (
        [{"posted": date(2026, 11, 14)}] * 2    # 오늘
        + [{"posted": date(2026, 11, 13)}] * 5
        + [{"posted": date(2026, 11, 12)}] * 4
        + [{"posted": date(2026, 11, 11)}] * 1  # 가장 오래된 날
    )
    assert collect_naver.complete_day_counts(items, TODAY) == {date(2026, 11, 13): 5, date(2026, 11, 12): 4}


def test_clean_strips_markup():
    """검색 결과의 강조 표시(<b>)와 HTML 기호(&quot;)를 걷어 낸다."""
    assert collect_naver._clean("<b>두바이</b> 초콜릿 &quot;후기&quot;") == '두바이 초콜릿 "후기"'


@pytest.mark.parametrize("occasion", [None, "개인", "선물", "행사", "단체주문"])
def test_bulk_orders_require_capacity_and_date_for_every_occasion(occasion):
    """대량 선물·행사도 단체주문이다. 날짜가 없으면 검색하기 전에 되묻는다."""
    raw = Slots(region="강남", quantity=200, occasion=occasion)
    assert slot_rules.is_group_order(raw)
    assert slot_rules.missing(raw) == ["due_date", "storage"]
    cleaned = slot_rules.normalize(raw, "강남에서 쿠키 200개 선물", TODAY)
    assert cleaned.occasion == "단체주문"


@pytest.mark.parametrize("quantity, expected", [(19, False), (20, True)])
def test_group_order_quantity_boundary(quantity, expected):
    """선물 주문도 정확히 20개부터 공급 조건을 검사한다."""
    assert slot_rules.is_group_order(Slots(occasion="선물", quantity=quantity)) is expected


@pytest.mark.parametrize("instant, expected", [
    (datetime(2026, 11, 13, 18, tzinfo=timezone.utc), date(2026, 11, 14)),
    (datetime(2026, 11, 13, 14, 59, tzinfo=timezone.utc), date(2026, 11, 13)),
    (datetime(2026, 11, 13, 15, tzinfo=timezone.utc), date(2026, 11, 14)),
])
def test_business_date_uses_korean_midnight(instant, expected):
    """수집이 도는 한국 새벽 3시와 자정 경계의 날짜는 호스트 시간대와 관계없이 같다."""
    assert dates.today(instant) == expected


def test_business_date_rejects_naive_time():
    """시간대가 빠져 뜻이 불분명한 시험 입력은 조용히 컴퓨터 시간대로 해석하지 않는다."""
    with pytest.raises(ValueError):
        dates.today(datetime(2026, 11, 14, 3))


def test_news_posted_date_is_converted_to_korea():
    """뉴스가 UTC 날짜를 주어도 한국 날짜로 집계한다."""
    assert collect_naver._posted_date({"pubDate": "Fri, 13 Nov 2026 18:00:00 +0000"}, "news") == TODAY


def test_complete_search_records_zero_days_inside_coverage():
    """조회 범위 안의 글 없는 날은 확인된 0건이고, 잘린 가장 오래된 날은 없다."""
    counts = collect_naver.complete_day_counts([{"posted": TODAY - timedelta(days=3)}], TODAY)
    assert counts == {TODAY - timedelta(days=2): 0, TODAY - timedelta(days=1): 0}


def test_exhausted_empty_search_is_confirmed_zero():
    """끝까지 조회해 글이 하나도 없으면 최근 30일은 모두 확인된 0건이다."""
    counts = collect_naver.complete_day_counts([], TODAY, exhausted=True)
    assert len(counts) == 30 and set(counts.values()) == {0}


def test_unknown_date_cannot_be_marked_complete():
    """날짜를 못 읽은 글이 섞이면 어느 날이 누락됐는지 몰라 완료 집계를 내지 않는다."""
    assert collect_naver.complete_day_counts([{"posted": None}], TODAY, exhausted=True) == {}


@pytest.mark.parametrize("missing_source", ["naver_blog", "naver_news", "naver_datalab", None])
def test_metric_series_distinguishes_missing_source_and_real_zero(missing_source):
    """글·검색 두 계열이 남으면 확인된 0건도 쓴다. 검색 계열 자체가 빠지면 미완료다."""
    rows = [{"metric_date": TODAY, "source": source, "mention_count": 0,
             "search_ratio": 0.0 if source == "naver_datalab" else None, "is_complete": True}
            for source in ("naver_blog", "naver_news", "naver_datalab") if source != missing_source]
    last = pipeline.metric_series(rows, TODAY, is_sample=False)[-1]
    assert last["is_complete"] is (missing_source != "naver_datalab")
    assert last["mentions"] == 0.0
    assert last["search"] == (None if missing_source == "naver_datalab" else 0.0)


def test_legacy_metrics_are_not_assumed_complete():
    """기존 DB 에서 완료 표시 없이 넘어온 값은 양수여도 점수 근거로 쓰지 않는다."""
    row = {"metric_date": TODAY, "source": "sample", "mention_count": 50, "search_ratio": 100, "is_complete": False}
    last = pipeline.metric_series([row], TODAY, is_sample=True)[-1]
    assert last == {"metric_date": TODAY, "mentions": None, "search": None, "social": None, "is_complete": False}


# ───────────────────── 점수의 조정값·교차검증·시간창 ─────────────────────

def test_score_weights_can_be_retuned():
    """가중치는 인자로 바꿔 넣을 수 있다. 호감도만 100 으로 두면 점수는 긍정 비율 × 100 이다."""
    only_sentiment = scoring.ScoreWeights(mention_growth=0, search_momentum=0, sentiment=100, sources=0)
    assert scoring.trend_score([30.0] * 30, [30.0] * 30, 0.8, 3, only_sentiment) == 80.0


def test_stage_thresholds_can_be_retuned():
    """단계 기준도 바꿔 넣을 수 있다. 잠잠함의 기준을 올리면 같은 시계열이 태동기로 바뀐다."""
    growing = [float(v) for v in seed_sample.SERIES["(샘플) 피스타치오 크림빵"][1]]
    assert scoring.stage(growing) == "성장기"
    assert scoring.stage(growing, scoring.StageThresholds(quiet=5.0)) == "태동기"


@pytest.mark.parametrize("growth, expected", [
    ({"글": 0.4, "검색": 0.3}, "일치"), ({"글": 0.4, "검색": 0.0}, "불일치"), ({"글": 0.0, "검색": 0.4}, "불일치"),
    ({"글": 0.02, "검색": -0.1}, "약함"),
    ({"글": 0.0, "검색": 0.0, "SNS": 0.9}, "불일치"),     # SNS 만 터지면 판단을 미룬다
    ({"글": 0.0, "검색": 0.3, "SNS": 0.9}, "일치"),       # 검색과 SNS 가 함께 오르면 믿는다
    ({"글": 0.5}, "불일치"),                             # 견줄 다른 계열이 없으면 확인된 것이 아니다
])
def test_agreement_counts_rising_signal_families(growth, expected):
    """둘 이상의 계열이 함께 오르면 일치, 한 계열만 오르면 불일치, 모두 잠잠하면 약함이다."""
    assert scoring.agreement(growth) == expected


def test_weekly_means_groups_from_the_latest_day():
    """가장 최근 날부터 7일씩 묶고, 맨 앞의 자투리(여기서는 2일)는 버린다."""
    daily = [100.0, 100.0] + [1.0] * 7 + [3.0] * 7
    assert scoring.weekly_means(daily) == [1.0, 3.0]


def test_long_window_needs_ten_weeks():
    """긴 시간창은 주 단위 10칸(70일)이 있어야 판정한다. 모자라면 None 이다."""
    assert scoring.windows([20.0] * 10, [20.0] * 69)["3m"] is None
    assert scoring.windows([20.0] * 10, [20.0] * 70)["3m"]["stage"] == "태동기"


def test_short_and_long_windows_are_judged_separately():
    """최근 며칠만 뛴 시계열은 짧은 창에서 성장기지만, 석 달 흐름으로는 이제 막 오르는 정도다(둘을 따로 본다)."""
    growing = [float(v) for v in seed_sample.SERIES["(샘플) 피스타치오 크림빵"][1]]
    padded = [growing[0]] * (seed_sample.LONG_DAYS - seed_sample.DAYS) + growing
    views = scoring.windows(growing[-10:], padded)
    assert views["7d"]["stage"] == "성장기"
    assert views["3m"] is not None and views["3m"]["growth_rate"] > 0


# ───────────────────────────── 백테스팅 ─────────────────────────────

def _points(values: list[float], start: date = date(2024, 1, 1)) -> list[tuple[date, float]]:
    """값 목록을 하루 간격의 (날짜, 값) 목록으로 바꾼다."""
    return [(start + timedelta(days=offset), value) for offset, value in enumerate(values)]


# 잠잠하다가 점점 빨리 오르고(20일), 정점을 찍은 뒤 내려가는 모양
BOOM = [10.0] * 20 + [10.0 * 1.12 ** day for day in range(1, 21)] + [96.0, 80.0, 60.0, 45.0, 30.0, 20.0]


def test_backtest_signal_comes_before_the_peak():
    """급등 아이템은 정점보다 앞서 성장기 신호가 나온다. 리드타임은 양수다."""
    row = backtest.evaluate_case("급등", _points(BOOM), "2024-02")
    assert row["peak"] == "2024-02-09" and row["label_matches"] is True
    assert row["lead_days"] > 0 and row["false_signal"] is False


def test_backtest_uses_only_past_values():
    """신호일은 그날까지의 값만으로 정해진다. 뒤에 어떤 값이 오든 같은 날이어야 한다(미래 정보 누설 없음)."""
    cut = 30
    full = backtest.first_growth_signal(_points(BOOM))
    partial = backtest.first_growth_signal(_points(BOOM[:cut]))
    assert full is not None and full == partial


def test_backtest_signal_does_not_depend_on_scale():
    """데이터랩 값은 기간 안 최댓값 기준의 상대값이다. 전체에 같은 수를 곱해도 신호일이 같아야 한다."""
    assert backtest.first_growth_signal(_points(BOOM)) == backtest.first_growth_signal(_points([v * 0.37 for v in BOOM]))


def test_backtest_steady_item_gives_no_signal():
    """꾸준히 팔리는 지속형(변화 없음)에는 성장기 신호가 없어야 한다. 있으면 헛신호다."""
    steady = backtest.evaluate_case("지속형", _points([50.0] * 60), None)
    assert steady["signal"] is None and steady["false_signal"] is False
    bumpy = backtest.evaluate_case("지속형", _points(BOOM), None)
    assert bumpy["false_signal"] is True


def test_baseline_fires_later_than_our_signal():
    """비교 기준(직전 7칸 평균보다 50% 급증)은 값이 크게 뛴 뒤에야 걸려, 우리 신호보다 늦다."""
    points = _points(BOOM)
    assert backtest.first_growth_signal(points) < backtest.baseline_signal(points)


def test_backtest_summary_averages_only_labeled_items():
    """평균 리드타임은 정점이 있는 아이템만으로 내고, 지속형의 헛신호는 따로 센다."""
    rows = [backtest.evaluate_case("급등", _points(BOOM), "2024-02"),
            backtest.evaluate_case("지속형", _points([50.0] * 60), None),
            backtest.evaluate_case("헛신호", _points(BOOM), None)]
    summary = backtest.summarize(rows)
    assert summary["items"] == 3 and summary["false_signals"] == 1 and summary["missed"] == 0
    assert summary["avg_lead_days"] == rows[0]["lead_days"]
    assert summary["improvement_days"] == round(rows[0]["lead_days"] - rows[0]["baseline_lead_days"], 1)


def test_late_signal_is_counted_as_failure():
    """정점이 지난 뒤에야 처음 잡았으면 리드타임이 음수이고 실패(헛신호)로 센다."""
    assert backtest.lead_time_days(date(2024, 2, 20), date(2024, 2, 10)) == -10
    assert backtest.lead_time_days(None, date(2024, 2, 10)) is None


# ───────────────────────── 광고·체험단 글 표시 ─────────────────────────

@pytest.mark.parametrize("title, snippet, expected", [
    ("두바이초콜릿 체험단 후기", "맛있었어요", True),
    ("두바이초콜릿 후기", "업체로부터 제품을 제공받아 작성했습니다", True),
    ("두바이초콜릿 내돈내산 후기", "직접 사 먹어 봤다", False),
])
def test_ad_marks_are_detected(title, snippet, expected):
    """제목·요약에 체험단·제공받아 같은 표기가 있으면 광고 글로 본다."""
    assert collect_naver.is_ad(title, snippet) is expected


def test_ad_posts_are_counted_separately():
    """광고 글은 글 수에서 빠지고 따로 세지만, "어느 날까지 온전히 받았는가" 는 광고 글까지 포함해 판단한다."""
    yesterday, older = TODAY - timedelta(days=1), TODAY - timedelta(days=2)
    items = [{"posted": yesterday, "ad": False}, {"posted": yesterday, "ad": True}, {"posted": yesterday, "ad": True},
             {"posted": older, "ad": True}, {"posted": TODAY - timedelta(days=3), "ad": False}]
    assert collect_naver.complete_day_counts(items, TODAY) == {older: 0, yesterday: 1}
    assert collect_naver.complete_day_counts(items, TODAY, ads=True) == {older: 1, yesterday: 2}


# ───────────────────── 검색 순위 합치기 (RRF) ─────────────────────

def test_rrf_rewards_items_found_by_both_searches():
    """두 순위표에 모두 나온 메뉴가 한쪽에만 나온 메뉴보다 점수가 높다."""
    scores = search.rrf_scores([[1, 2, 3], [3, 4]])
    assert scores[3] > scores[1] > scores[2] and scores[4] < scores[1]
    assert scores[1] == pytest.approx(1 / (search.RRF_K + 1))


def test_fuse_keeps_keyword_only_match_and_records_ranks():
    """의미 검색 상위에 못 든 메뉴도 이름이 문의에 나왔으면 후보에 들어가고, 어느 순위표의 몇 위였는지 남는다."""
    by_vector = [{"menu_id": 1, "similarity": 0.9, "keyword_hits": 0}, {"menu_id": 2, "similarity": 0.8, "keyword_hits": 1}]
    by_keyword = [{"menu_id": 9, "similarity": 0.3, "keyword_hits": 2}, {"menu_id": 2, "similarity": 0.8, "keyword_hits": 1}]
    fused = search.fuse(by_vector, by_keyword, limit=10)
    assert [row["menu_id"] for row in fused][0] == 2          # 두 표에 모두 있는 메뉴가 맨 위
    assert {row["menu_id"] for row in fused} == {1, 2, 9}
    nine = next(row for row in fused if row["menu_id"] == 9)
    assert nine["vector_rank"] is None and nine["keyword_rank"] == 1


def test_every_hard_condition_has_a_label():
    """결과가 없을 때 보여 줄 이름이 조건마다 있어야 한다(종류 조건은 넓혀 찾을 때 풀므로 예외)."""
    assert set(search.CONDITIONS) - {"dessert"} == set(search.CONDITION_LABELS)


# ───────────────────── 순위: 가중치 묶음·적합도·지역·보존율 ─────────────────────

def test_fit_weight_is_the_largest_in_every_profile():
    """어느 가중치 묶음에서도 적합도가 가장 크고, 가중치의 합은 1 이다."""
    for weights in ranking.WEIGHTS.values():
        assert sum(weights.values()) == pytest.approx(1.0)
        assert weights["fit"] == max(weights.values())


def test_profiles_follow_the_purpose():
    """선물은 품질을, 단체주문은 지역·공급 능력을 기본보다 무겁게 본다."""
    base, gift, group = ranking.WEIGHTS["기본"], ranking.WEIGHTS["선물"], ranking.WEIGHTS["단체주문"]
    assert gift["quality"] > base["quality"] and gift["trend"] < base["trend"]
    assert group["region"] > base["region"] and group["supply"] > base["supply"]
    assert ranking.profile(Slots(occasion="선물", quantity=200)) == "단체주문"   # 대량 선물은 단체주문으로 본다
    assert ranking.profile(Slots(occasion="선물")) == "선물" and ranking.profile(Slots()) == "기본"


def test_keyword_match_raises_fit_up_to_a_limit():
    """문의에 이름이 그대로 나온 메뉴는 적합도가 오르되, 세 낱말까지만 센다."""
    assert ranking.fit_signal(_candidate(1, 0.5, keyword_hits=1)) == pytest.approx(0.6)
    assert ranking.fit_signal(_candidate(1, 0.5, keyword_hits=9)) == pytest.approx(0.8)
    assert ranking.fit_signal(_candidate(1, 0.95, keyword_hits=3)) == 1.0


def test_region_signal_prefers_local_shops():
    """같은 지역 업체 > 그 지역으로 배송하는 업체 > 전국 배송뿐인 업체."""
    scores = [ranking.region_signal(_candidate(1, 0.5, region_match=match)) for match in ("local", "delivery", "nationwide")]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == 3


def test_stale_capacity_lowers_supply_signal():
    """같은 생산 여유라도 공급 조건이 오래됐으면 공급 능력 점수가 낮다."""
    slots = Slots(region="강남", occasion="단체주문", quantity=100, due_date="2099-01-01")
    fresh = _candidate(1, 0.5, max_qty_per_day=1000, confirmation_status="confirmed", valid_until=TODAY + timedelta(days=5))
    stale = _candidate(2, 0.5, max_qty_per_day=1000, confirmation_status="confirmed", valid_until=TODAY - timedelta(days=1))
    assert ranking.supply_signal(fresh, slots, TODAY) > ranking.supply_signal(stale, slots, TODAY)


def test_boost_audit_measures_preservation():
    """적합도 우선 순서가 유지되면 가산 전후 상위 보존율은 1이며 후보가 없어도 1이다."""
    slots = Slots(region="강남")
    ranked = ranking.rank([_candidate(1, 0.700), _candidate(2, 0.695, is_partner=True), _candidate(3, 0.2)], slots, {})
    audit = ranking.boost_audit(ranked, top_n=1)
    assert audit == {"profile": "기본", "top_n": 1, "preservation": 1.0, "boosted_in": []}
    # 화면에 둘 다 나가면 순서만 바뀐 것이라 상위 묶음은 그대로다
    assert ranking.boost_audit(ranked, top_n=2)["preservation"] == 1.0
    assert ranking.boost_audit([], top_n=5)["preservation"] == 1.0


# ───────────────────── 공급 조건 최신성·견적 ─────────────────────

GROUP = Slots(region="강남", occasion="단체주문", quantity=50, due_date="2099-01-01", storage="무관")


def _capacity(**extra) -> dict:
    """확인된 지 얼마 안 된 공급 조건을 가진 후보 한 건. extra 로 준 칸만 바꾼다."""
    base = {"price": 3500, "max_qty_per_day": 2000, "confirmation_status": "confirmed",
            "valid_until": TODAY + timedelta(days=10), "last_verified_at": TODAY - timedelta(days=20)}
    return {**base, **extra}


def test_capacity_check_follows_freshness_rule():
    """유효기간 안이면 auto, 지났거나 확인 전이면 reconfirm, 대량 주문이면 조건이 최신이어도 confirm 이다."""
    assert capacity.check(_capacity(), GROUP, TODAY)["status"] == "auto"
    assert capacity.check(_capacity(valid_until=TODAY - timedelta(days=1)), GROUP, TODAY)["status"] == "reconfirm"
    assert capacity.check(_capacity(confirmation_status="unverified"), GROUP, TODAY)["status"] == "reconfirm"
    assert capacity.check(_capacity(valid_until=None), GROUP, TODAY)["status"] == "reconfirm"
    large = GROUP.model_copy(update={"quantity": 100})
    assert capacity.check(_capacity(), large, TODAY)["status"] == "confirm"
    # 하루 최대 생산량의 절반 이상을 차지해도 대량이다
    assert capacity.check(_capacity(max_qty_per_day=80), GROUP, TODAY)["status"] == "confirm"
    # 낡은 조건은 대량이어도 재확인이 먼저다
    assert capacity.check(_capacity(valid_until=None), large, TODAY)["status"] == "reconfirm"


def test_capacity_check_only_for_group_orders():
    """단체주문이 아니면 공급 조건을 따지지 않는다."""
    assert capacity.check(_capacity(), Slots(region="강남"), TODAY) is None


def test_quote_is_computed_by_code():
    """견적 합계 = 수량 × 1개 가격. 총예산이 있으면 넘는지도 계산한다."""
    slots = Slots(region="판교", headcount=200, budget_per_person=8000)
    assert capacity.quote({"price": 3500}, slots) == {
        "quantity": 200, "unit_price": 3500, "total_price": 700000, "budget": 1600000, "within_budget": True}
    assert capacity.quote({"price": 9000}, slots)["within_budget"] is False


def test_quote_is_not_invented_without_price_or_quantity():
    """수량을 모르거나 가격이 DB 에 없으면 견적을 만들지 않는다."""
    assert capacity.quote({"price": None}, GROUP) is None
    assert capacity.quote({"price": 3500}, Slots(region="강남")) is None


def test_evidence_carries_quote_and_capacity_check():
    """추천 사유의 근거에 견적 합계와 공급자 확인 필요 여부가 들어가고, 지역 밖 업체는 배송으로 맞췄음을 밝힌다."""
    slots = Slots(region="판교", headcount=200, budget_per_person=8000, due_date="2099-01-01", storage="냉장")
    result = {"shop_name": "떡집", "menu_name": "찹쌀떡", "price": 3500, "region": "서울 강남구", "region_match": "nationwide",
              "delivery_areas": ["전국"], "rating": 4.4, "review_count": 60, "storage": "냉장", **_capacity()}
    result["capacity_check"] = capacity.check(result, slots, TODAY)
    result["quote"] = capacity.quote(result, slots)
    evidence = explain.build_evidence(result, slots)
    assert "700000" in evidence["F9"]["text"] and "1600000" in evidence["F9"]["text"]
    assert "공급자 확인" in evidence["F8"]["text"] and "배송 가능 지역(전국)" in evidence["F2"]["text"]
    assert "인원 200명" in evidence["Q1"]["text"] and "보관 냉장" in evidence["Q1"]["text"]


def test_proposal_block_never_invents_a_quote():
    """제안서 초안은 저장된 견적을 그대로 쓰고, 값이 없으면 담당자 확인이 필요하다고 적는다. 출처 표시는 뗀다."""
    option = {"rank": 1, "shop_name": "떡집", "menu_name": "찹쌀떡", "quantity": 200, "unit_price": 3500,
              "total_price": 700000, "capacity_check": "confirm", "reason": "조건에 맞습니다. [출처:F1,Q1]\n가격이 맞습니다. [출처:F9]"}
    block = proposal.option_block(option)
    assert "200개 × 3,500원 = 700,000원" in block and "공급자 확인" in block and "[출처" in block
    unknown = proposal.option_block({**option, "quantity": None, "unit_price": None, "total_price": None})
    assert "담당자 확인" in unknown and "700,000" not in unknown


# ───────────────────── 출처 목록과 출처별·계열별 증가율 ─────────────────────

def test_source_registry_separates_core_and_supporting_sources():
    """점수 계산의 기본 출처는 네이버 셋이고, 인스타그램·RSS 는 계열만 정해진 보조 출처다."""
    assert sources.core_sources(sources.TEXT) == ("naver_blog", "naver_news")
    assert sources.core_sources(sources.SEARCH) == ("naver_datalab",) and sources.core_sources(sources.SOCIAL) == ()
    assert sources.family_of("instagram") == sources.SOCIAL and sources.family_of("rss:디저트매체") == sources.TEXT
    assert sources.family_of("모르는출처") is None and sources.family_of(sources.SAMPLE) is None


def _source_rows(source: str, values: list[float], *, skip_day: int | None = None) -> list[dict]:
    """출처 하나의 최근 날짜별 행을 만든다(마지막 값이 TODAY). skip_day 번째 날은 수집이 빠진 것으로 뺀다."""
    search = sources.family_of(source) == sources.SEARCH
    return [{"metric_date": TODAY - timedelta(days=len(values) - 1 - index), "source": source, "is_complete": True,
             "mention_count": None if search else value, "search_ratio": value if search else None}
            for index, value in enumerate(values) if index != skip_day]


def test_source_views_need_ten_complete_days_per_source():
    """출처마다 최근 10일이 온전할 때만 증가율을 낸다. 하루가 빠진 출처와 목록에 없는 출처는 넣지 않는다."""
    rising = [10.0] * 7 + [20.0] * 3
    rows = (_source_rows("naver_blog", rising) + _source_rows("instagram", rising, skip_day=4)
            + _source_rows("naver_datalab", [50.0] * 10) + _source_rows("모르는출처", rising))
    views = pipeline.source_views(rows, TODAY)
    assert set(views) == {"naver_blog", "naver_datalab"}
    assert views["naver_blog"]["growth_rate"] == 1.0 and views["naver_blog"]["total_7d"] == 100.0
    assert views["naver_datalab"]["family"] == sources.SEARCH and views["naver_datalab"]["growth_rate"] == 0.0


def test_family_growth_adds_sources_of_the_same_family():
    """같은 계열의 출처는 날짜별로 더해 계열 증가율을 낸다. SNS 만 오르면 교차검증은 불일치다."""
    flat, rising = [10.0] * 10, [10.0] * 7 + [40.0] * 3
    rows = (_source_rows("naver_blog", flat) + _source_rows("naver_news", flat) + _source_rows("rss:매체", flat)
            + _source_rows("naver_datalab", flat) + _source_rows("instagram", rising))
    growth = pipeline.family_growth(pipeline.source_views(rows, TODAY))
    assert growth == {sources.TEXT: 0.0, sources.SEARCH: 0.0, sources.SOCIAL: 3.0}
    assert scoring.agreement(growth) == "불일치"


# ───────────────────── 인스타그램 수집 (가짜 응답) ─────────────────────

@pytest.mark.parametrize("term, expected", [("두바이 초콜릿", "두바이초콜릿"), ("소금빵", "소금빵"), ("크룽지🥐", None), ("A+B", None)])
def test_hashtag_removes_spaces_and_rejects_unsupported_characters(term, expected):
    """해시태그는 띄어쓰기를 빼서 만들고, 이모지·기호가 든 말은 조회하지 않는다."""
    assert collect_instagram.hashtag_for(term) == expected


def test_pick_keywords_keeps_active_ones_and_fills_remaining_quota():
    """이미 조회 중인 키워드는 계속 잇고, 7일 한도의 남은 자리만 새 후보(점수 높은 순)로 채운다."""
    assert collect_instagram.pick_keywords([7, 8], [1, 7, 2, 8, 3], limit=4) == [7, 8, 1, 2]
    assert collect_instagram.pick_keywords([7, 8], [1, 2], limit=2) == [7, 8]      # 자리가 없으면 새 키워드를 넣지 않는다


def test_instagram_follows_cursor_and_sends_token_in_header(monkeypatch):
    """다음 쪽은 위치표(after)로 넘기고, 토큰은 주소가 아니라 헤더로 보낸다. 쪽 수 한도에 걸리면 끝까지 받지 못한 것이다."""
    monkeypatch.setattr(collect_instagram.settings, "INSTAGRAM_USER_ID", "111")
    monkeypatch.setattr(collect_instagram.settings, "INSTAGRAM_ACCESS_TOKEN", "secret-token")
    monkeypatch.setattr(collect_instagram.time, "sleep", lambda seconds: None)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """첫 쪽에는 다음 쪽이 있다고 답하고, 둘째 쪽에서 끝낸다."""
        seen.append(request)
        if request.url.path.endswith("/ig_hashtag_search"):
            return httpx.Response(200, json={"data": [{"id": "178"}]})
        if request.url.params.get("after") is None:
            return httpx.Response(200, json={"data": [{"id": "1", "timestamp": "2026-11-13T03:00:00+0000", "like_count": 5, "comments_count": 1}],
                                             "paging": {"cursors": {"after": "c2"}, "next": "https://graph.facebook.com/next"}})
        return httpx.Response(200, json={"data": [{"id": "2", "timestamp": "2026-11-13T04:00:00+0000", "comments_count": 2}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert collect_instagram.fetch_hashtag_id(client, "소금빵") == "178"
        items, exhausted = collect_instagram.fetch_recent_media(client, "178")
        assert [item["id"] for item in items] == ["1", "2"] and exhausted is True
        monkeypatch.setattr(collect_instagram, "MAX_PAGES", 1)
        assert collect_instagram.fetch_recent_media(client, "178")[1] is False
    assert all(request.headers["Authorization"] == "Bearer secret-token" for request in seen)
    assert all("secret-token" not in str(request.url) for request in seen)
    assert seen[0].url.params["q"] == "소금빵" and seen[2].url.params["after"] == "c2"


def test_instagram_day_summary_uses_korean_date():
    """게시물은 한국 날짜로 센다. UTC 15시는 한국의 다음 날 0시다. 좋아요 수를 숨긴 게시물은 0 으로 더한다."""
    items = [{"timestamp": "2026-11-12T15:00:00+0000", "like_count": 5, "comments_count": 1},   # 한국 11-13 00:00
             {"timestamp": "2026-11-13T14:59:59+0000", "comments_count": 2},                    # 한국 11-13 23:59
             {"timestamp": "2026-11-13T15:00:00+0000", "like_count": 9},                        # 한국 11-14
             {"timestamp": "읽을 수 없음"}]
    assert collect_instagram.day_summary(items, date(2026, 11, 13)) == {"count": 2, "likes": 5, "comments": 3}


# ───────────────────── RSS·웹 피드 수집 (가짜 응답) ─────────────────────

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>두바이초콜릿 &amp; 카다이프 열풍</title><link>https://example.com/a</link>
<description>&lt;p&gt;줄 서서 사는 &lt;b&gt;두바이초콜릿&lt;/b&gt;&lt;/p&gt;</description><pubDate>Fri, 13 Nov 2026 18:00:00 +0000</pubDate></item>
<item><title>소금빵 맛집</title><link>https://example.com/b</link><description>겉바속촉</description><pubDate>Thu, 12 Nov 2026 01:00:00 +0900</pubDate></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>크룽지 만들기</title><link href="https://example.com/c"/>
<summary>크루아상을 눌러 굽는다</summary><published>2026-11-13T18:00:00Z</published></entry></feed>"""


def test_parse_feed_reads_rss_and_atom():
    """RSS 와 Atom 을 같은 모양의 글 목록으로 읽는다. HTML 표시는 걷어 내고 날짜는 한국 날짜로 바꾼다."""
    first, second = collect_rss.parse_feed(RSS)
    assert first == {"title": "두바이초콜릿 & 카다이프 열풍", "url": "https://example.com/a",
                     "snippet": "줄 서서 사는 두바이초콜릿", "posted": date(2026, 11, 14)}
    assert second["posted"] == date(2026, 11, 12)
    assert collect_rss.parse_feed(ATOM) == [{"title": "크룽지 만들기", "url": "https://example.com/c",
                                             "snippet": "크루아상을 눌러 굽는다", "posted": date(2026, 11, 14)}]
    assert collect_rss.parse_feed("<html>피드가 아님") == []


def test_site_feed_counts_only_matching_posts_but_keeps_coverage():
    """매체 피드는 키워드가 든 글만 세되, 어느 날까지 받았는지는 피드의 글 전체로 판단한다."""
    items = [{"title": "두바이초콜릿 후기", "snippet": "", "posted": TODAY - timedelta(days=1)},
             {"title": "소금빵 후기", "snippet": "", "posted": TODAY - timedelta(days=2)},
             {"title": "다른 글", "snippet": "", "posted": TODAY - timedelta(days=3)}]
    marked = collect_rss.mark_for_keyword(items, ["소금빵"])
    assert [item["skip"] for item in marked] == [True, False, True]
    assert collecting.complete_day_counts(marked, TODAY) == {TODAY - timedelta(days=2): 1, TODAY - timedelta(days=1): 0}


def test_feed_settings_must_be_a_list_of_name_and_url(monkeypatch):
    """피드 설정은 이름과 주소를 가진 목록이어야 한다. 비어 있으면 빈 목록이다."""
    monkeypatch.setattr(collect_rss.settings, "RSS_FEEDS", "")
    assert collect_rss.load_feeds() == []
    monkeypatch.setattr(collect_rss.settings, "RSS_FEEDS", '[{"name": "매체"}]')
    with pytest.raises(ValueError):
        collect_rss.load_feeds()


def _fetcher(robots_status: int, robots_text: str = "") -> tuple[collecting.PoliteFetcher, list[str]]:
    """robots.txt 응답을 정해 둔 가짜 사이트와, 그 사이트를 읽는 PoliteFetcher 를 만든다. 요청한 주소도 함께 돌려준다."""
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """robots.txt 에는 정해 둔 답을, 나머지 주소에는 본문을 돌려준다."""
        called.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots_text)
        return httpx.Response(200, text=RSS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return collecting.PoliteFetcher(client, "ddalgiro-test", min_interval=0), called


def test_polite_fetcher_obeys_robots_txt():
    """robots.txt 가 막은 주소는 부르지 않고, 막지 않은 주소는 이름표(User-Agent)를 붙여 읽는다. robots.txt 는 한 번만 받는다."""
    fetcher, called = _fetcher(200, "User-agent: *\nDisallow: /private/")
    assert fetcher.get("https://example.com/private/feed.xml") is None
    assert collect_rss.fetch_feed(fetcher, "https://example.com/feed.xml")[0]["url"] == "https://example.com/a"
    assert called == ["/robots.txt", "/feed.xml"]


@pytest.mark.parametrize("status, expected", [(404, True), (503, False)])
def test_polite_fetcher_without_readable_robots(status, expected):
    """robots.txt 가 없으면(404) 막지 않은 것으로 보고, 서버 오류(503)면 알 수 없으므로 읽지 않는다."""
    fetcher, _called = _fetcher(status)
    assert fetcher.allowed("https://example.com/feed.xml") is expected


def test_feed_matching_ignores_spacing_and_uses_aliases():
    """띄어쓰기가 달라도 같은 키워드로 세고, 다른 표기(외국어)가 든 글도 그 키워드의 글이다."""
    items = [{"title": "두바이 초콜릿 열풍", "snippet": ""}, {"title": "Why DUBAI Chocolate went viral", "snippet": ""},
             {"title": "초콜릿 가격 인상", "snippet": ""}]
    assert [item["skip"] for item in collect_rss.mark_for_keyword(items, ["두바이초콜릿"])] == [False, True, True]
    assert [item["skip"] for item in collect_rss.mark_for_keyword(items, ["두바이초콜릿", "Dubai chocolate", " "])] == [False, False, True]


def test_check_feed_reports_whether_daily_counts_are_possible():
    """피드 후보 점검: robots.txt 가 허용하고 모든 글에 날짜가 있어 온전한 하루의 글이 나오면 쓸 수 있다."""
    feed = """<rss><channel>
    <item><title>어제 글</title><link>https://example.com/1</link><pubDate>Fri, 13 Nov 2026 10:00:00 +0900</pubDate></item>
    <item><title>사흘 전 글</title><link>https://example.com/2</link><pubDate>Wed, 11 Nov 2026 10:00:00 +0900</pubDate></item>
    </channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        """robots.txt 는 /private/ 만 막고, 나머지 주소에는 위 피드를 돌려준다."""
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        return httpx.Response(200, text=feed)

    fetcher = collecting.PoliteFetcher(httpx.Client(transport=httpx.MockTransport(handler)), "ddalgiro-test", min_interval=0)
    good = collect_rss.check_feed(fetcher, "https://example.com/feed.xml", TODAY)
    assert good["usable"] is True and good["items"] == 2 and good["undated"] == 0
    # 가장 오래된 날(11일)은 잘렸을 수 있어 빼고, 12일(0건)과 13일(1건)이 온전한 날이다
    assert (good["oldest"], good["newest"], good["complete_days"], good["per_day"]) == (date(2026, 11, 11), date(2026, 11, 13), 2, 0.5)
    blocked = collect_rss.check_feed(fetcher, "https://example.com/private/feed.xml", TODAY)
    assert blocked["usable"] is False and blocked["robots_allowed"] is False and "robots.txt" in blocked["note"]


def test_parse_feed_reads_rss1_and_dates_without_timezone():
    """RSS 1.0(RDF) 피드도 읽고, 시간대 없이 적은 날짜는 한국 시각으로 본다(국내 매체의 피드가 이렇게 적는다)."""
    rdf = """<rdf:RDF xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><item rdf:about="https://example.com/r">
        <title>新作スイーツ</title><link>https://example.com/r</link><description>発売</description>
        <dc:date>2026-11-13T23:30:00+09:00</dc:date></item></rdf:RDF>"""
    assert collect_rss.parse_feed(rdf) == [{"title": "新作スイーツ", "url": "https://example.com/r", "snippet": "発売", "posted": date(2026, 11, 13)}]
    naive = """<rss version="2.0"><channel><item><title>신제품 출시</title><link>https://example.com/n</link>
        <description>요약</description><pubDate>2026-11-13 23:59:00</pubDate></item></channel></rss>"""
    assert collect_rss.parse_feed(naive)[0]["posted"] == date(2026, 11, 13)


def test_check_feed_rejects_feeds_that_only_hold_today():
    """글이 너무 많아 오늘 것만 담긴 피드는, 오래된 글이 하나 끼어 있어도 하루치를 셀 수 없으므로 쓸 수 없다."""
    today_rfc = "Sat, 14 Nov 2026 01:00:00 +0900"
    busy = "<rss><channel>" + "".join(
        f"<item><title>글{n}</title><link>https://example.com/{n}</link><pubDate>{today_rfc}</pubDate></item>" for n in range(5)
    ) + "<item><title>고정 글</title><link>https://example.com/old</link><pubDate>Mon, 02 Nov 2026 01:00:00 +0900</pubDate></item></channel></rss>"

    def handler(request: httpx.Request) -> httpx.Response:
        """robots.txt 는 없다고 답하고, 피드 주소에는 오늘 글만 가득한 피드를 돌려준다."""
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, text=busy)

    fetcher = collecting.PoliteFetcher(httpx.Client(transport=httpx.MockTransport(handler)), "ddalgiro-test", min_interval=0)
    result = collect_rss.check_feed(fetcher, "https://example.com/feed.xml", TODAY)
    assert result["usable"] is False and result["items"] == 6 and result["per_day"] == 0.0


# ───────────────────── 계획서 대조에서 재현한 결함의 회귀 시험 ─────────────────────

def test_budget_uses_headcount_and_quantity_independently():
    """100명×8000원 예산으로 200개 주문하면 개당 상한은 4000원이며, 정수 나눗셈의 0도 유효한 상한이다."""
    assert slot_rules.price_cap(Slots(headcount=100, quantity=200, budget_per_person=8000)) == 4000
    assert slot_rules.price_cap(Slots(quantity=200, total_budget=100)) == 0


def test_partner_cannot_cross_fit_or_displace_top_five(monkeypatch):
    """가산 설정을 크게 해도 적합도 순서와 상위 95% 보존을 깨지 못한다. 동점 경계에서도 가산을 철회한다."""
    monkeypatch.setattr(ranking.settings, "PARTNER_BOOST_CAP", 9)
    monkeypatch.setattr(ranking.settings, "RECO_RESULTS", 5)
    rows = [_candidate(i, .91 - i / 100, is_partner=(i == 6)) for i in range(1, 7)]
    ranked = ranking.rank(rows, Slots(), {})
    assert [r["shop_id"] for r in ranked] == list(range(1, 7))
    assert all(r["score_detail"]["partner_boost"] <= .03 for r in ranked)
    tied = ranking.rank([_candidate(i, .8, is_partner=(i == 6)) for i in range(1, 7)], Slots(), {})
    assert ranking.boost_audit(tied, 5)["preservation"] >= .95
    assert all(r["score_detail"].get("boost_withheld") for r in tied)


@pytest.mark.parametrize("text", ["가격은 200원, 생산량은 3500개다.", "가격은 3500원, 생산량은 200개이고 비건이다.", "납품이 가능하다."])
def test_grounding_rejects_attribute_swaps_and_unsupported_claims(text):
    """숫자 집합이 같거나 숫자가 없어도 속성 바꾸기·근거 없는 장점·부정 뒤집기는 거절한다."""
    evidence = {"F1": {"text": "가격은 3500원, 생산량은 200개다. 납품이 불가능하다.", "url": None}}
    assert grounding.check(_text((text, ["F1"])), evidence)


def test_grounding_inserts_mandatory_caution_and_validates_fallback(monkeypatch):
    """모델이 가격만 골라도 공급자 재확인 문장은 코드가 붙이고, 잘못된 대체 문장도 차단한다."""
    evidence = {"F1": {"text": "가격은 3500원이다.", "url": None}, "F8": {"text": "공급자 재확인이 필요하다.", "url": None}}
    monkeypatch.setattr(grounding.llm, "ask_structured", lambda *a, **k: _text((evidence["F1"]["text"], ["F1"])))
    text, method = grounding.write_grounded("역할", "질문", evidence, lambda: _text(("위조", ["F1"])), required_sources={"F8"})
    assert "재확인이 필요하다. [출처:F8]" in text and method == "llm"
    monkeypatch.setattr(grounding.llm, "ask_structured", lambda *a, **k: _text(("위조", ["F1"])))
    with pytest.raises(ValueError):
        grounding.write_grounded("역할", "질문", evidence, lambda: _text(("위조", ["F1"])))


def test_instagram_requires_full_day_union_and_valid_identity():
    """00:10 단독 조회의 앞 10분 구멍을 완료 처리하지 않는다. 겹친 구간은 허용하고 잘못된 시각·번호는 거절한다."""
    midnight = datetime(2026, 11, 14, tzinfo=dates.SEOUL)
    window = (midnight - timedelta(days=1) + timedelta(minutes=10), midnight + timedelta(minutes=10))
    assert not collect_instagram.covers_day([window], TODAY - timedelta(days=1))
    assert collect_instagram.covers_day([window, (window[0] - timedelta(hours=6), window[1] - timedelta(hours=6))], TODAY - timedelta(days=1))
    for item in ({"id": "1", "timestamp": "bad"}, {"timestamp": midnight.isoformat()}, {"id": "1", "timestamp": "2026-11-14T00:00:00"}):
        assert collect_instagram.parse_observations([item]) == ([], False)


@pytest.mark.parametrize("target", ["https://example.com/blocked", "https://another.example/blocked"])
def test_crawler_checks_redirect_target_before_requesting_it(target):
    """같은 사이트와 다른 사이트로 이동할 때 모두 도착 경로의 robots 규칙을 먼저 확인한다."""
    requests = []
    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /blocked\n")
        assert request.url.path != "/blocked"
        return httpx.Response(302, headers={"Location": target})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        assert collecting.PoliteFetcher(client, "TestBot", 0).get("https://example.com/start") is None
    assert target not in requests


def test_crawler_bounds_redirect_loops():
    """계속 되돌아오는 이동 응답은 무한 요청하지 않고 실패한다."""
    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(302, headers={"Location": "/start"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.TooManyRedirects):
            collecting.PoliteFetcher(client, "TestBot", 0).get("https://example.com/start")


def test_non_naver_sources_drive_scores_without_claiming_search():
    """RSS+인스타그램만 10일 확인돼도 판정한다. 검색량은 비워 두고 사용 계열·출처를 기록한다."""
    rising = [10, 10, 10, 12, 15, 20, 30, 45, 70, 100]
    rows = _source_rows("rss:시험", rising) + _source_rows("instagram", rising)
    result = signals.compute(signals.prepare(rows, TODAY))
    assert result["stage"] == "성장기" and result["search_growth"] is None
    assert result["by_source"]["basis"]["secondary_family"] == "SNS"
    assert result["signal_check"] == "일치"
    assert signals.compute(signals.prepare(rows[:10], TODAY))["stage"] == "자료부족"
    evidence = pipeline.build_evidence("쿠키", TODAY, result, [])
    assert "SNS 지수" in evidence["M4"]["text"] and "검색 관심도" not in evidence["M4"]["text"]


def test_normalized_cohort_ignores_partial_sources_and_scale():
    """하루만 생긴 대형 출처가 추세를 만들지 않고, 같은 출처의 단위 배수도 결과를 바꾸지 않는다."""
    rising = [10, 10, 10, 12, 15, 20, 30, 45, 70, 100]
    rows = _source_rows("rss:시험", rising) + _source_rows("instagram", rising)
    a = signals.compute(signals.prepare(rows, TODAY))
    b = signals.compute(signals.prepare([{**r, "mention_count": r["mention_count"] * 1000} for r in rows]
        + [{"metric_date": TODAY, "source": "naver_news", "mention_count": 9999999, "search_ratio": None, "is_complete": True}], TODAY))
    assert (a["stage"], a["growth_rate"], a["score"]) == (b["stage"], b["growth_rate"], b["score"])
    assert "naver_news" not in b["by_source"]["sources"]


def test_production_replay_matches_runtime_and_waits_ten_days():
    """백테스트의 매일 결과가 운영 계산기와 같고, 8일 가속이나 누락 구간으로 조기 신호를 만들지 않는다."""
    rising = [10, 10, 10, 12, 15, 20, 30, 45, 70, 100]
    rows = _source_rows("rss:시험", rising) + _source_rows("instagram", rising)
    replay = backtest.evaluate_metrics("쿠키", rows, TODAY)["replay"]
    assert all(r["stage"] == "자료부족" for r in replay[:-1])
    assert {k: v for k, v in replay[-1].items() if k != "day"} == signals.compute(signals.prepare(rows, TODAY))
    assert backtest.growth_signals(_points(rising[:8])) == []
    sparse = [(TODAY - timedelta(days=i * 2), float(100 - i)) for i in range(20)]
    assert backtest.growth_signals(sorted(sparse)) == []


def test_backtest_improvement_uses_paired_matching_labels():
    """한쪽만 검출한 항목과 틀린 정점 정답을 평균에 섞지 않는다."""
    base = {"peak_label": "2026-11", "label_matches": True, "false_signal": False, "signal": "2026-11-01", "baseline_signal": "2026-11-02"}
    summary = backtest.summarize([{**base, "lead_days": 10, "baseline_lead_days": 7},
        {**base, "lead_days": 100, "baseline_lead_days": None, "baseline_signal": None},
        {**base, "lead_days": 100, "baseline_lead_days": 1, "label_matches": False}])
    assert summary["improvement_days"] == 3 and summary["paired_items"] == 1
    assert summary["baseline_missed"] == 1 and summary["label_mismatches"] == 1

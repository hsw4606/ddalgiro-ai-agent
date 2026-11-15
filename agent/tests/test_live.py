"""실제 DB 와 LLM 에 붙여 처음부터 끝까지 확인하는 시험. 평소에는 건너뛰고, 환경 변수 LIVE_TEST=1 일 때만 돈다.

실행(agent 폴더에서):
    LIVE_TEST=1 DATABASE_URL=… LITELLM_BASE_URL=… LITELLM_API_KEY=… python -m pytest tests/test_live.py -q
전제: DATABASE_URL 의 DB 에 vector 확장이 있고, 게이트웨이에 chat-fast·chat-smart·embed 이름표(또는 환경 변수로 바꾼 이름)가 있다.
⚠️ 게이트웨이의 chat-* 이름표가 Claude API 를 가리키면 이 시험은 실제 요금이 든다(샘플 키워드 4개 분석 + 문의 십여 건, 대략 몇백 원).
⚠️ 샘플 데이터를 넣었다가 지운다. 실제 자료가 든 DB 에서 돌려도 샘플 행만 건드리지만, 연습용 DB 에서 돌리기를 권한다.
확인하는 것: "어기면 안 되는 조건" 이 실제 결과에서 지켜지는지(식이·지역·가격·영업 여부·보관·공급 조건)와,
            단체주문의 견적·공급 조건 확인 상태가 코드 계산대로 나오는지.
게이트웨이 없이 OpenAI 호환 모델 서버(예: Ollama)에 바로 붙일 때는 LITELLM_BASE_URL 을 그 서버 주소로,
MODEL_CHAT_FAST·MODEL_CHAT_SMART·MODEL_EMBED 를 그 서버의 모델 이름으로 준다.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.environ.get("LIVE_TEST") != "1", reason="LIVE_TEST=1 일 때만 실행")


@pytest.fixture(scope="module")
def client():
    """샘플을 넣고 분석·임베딩까지 마친 API 클라이언트. 시험이 끝나면 샘플을 지운다."""
    from app import db
    from app.jobs import embed_catalog, run_trend, seed_sample
    from app.main import app

    db.wait_for_db()
    db.apply_schema()
    seed_sample.seed()
    assert embed_catalog.main() == 0
    assert run_trend.main() == 0
    with TestClient(app) as test_client:
        yield test_client
    seed_sample.clear()


def _recommend(client: TestClient, text: str) -> dict:
    """추천 API 를 불러 응답 본문을 돌려준다. HTTP 200 이 아니면 시험을 실패시킨다."""
    response = client.post("/v1/recommend", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def test_trend_stages_and_briefing(client):
    """샘플 키워드가 의도한 단계로 나오고, 성장기 키워드에는 출처가 붙은 브리핑이 있다."""
    trends = {item["term"]: item for item in client.get("/v1/trends").json()}
    assert trends["(샘플) 피스타치오 크림빵"]["stage"] == "성장기"
    assert trends["(샘플) 버터 쿠키"]["stage"] == "태동기"
    detail = client.get(f"/v1/trends/{trends['(샘플) 피스타치오 크림빵']['keyword_id']}").json()
    assert "[출처:" in detail["insight"]["briefing"]
    assert len(detail["series"]) == 30
    # 샘플은 글 수와 검색 관심도가 함께 오르므로 교차검증이 '일치' 이고, 91일치가 있어 긴 시간창도 판정된다
    assert detail["score"]["signal_check"] == "일치"
    assert detail["score"]["windows"]["7d"]["stage"] == "성장기" and detail["score"]["windows"]["3m"] is not None


def test_short_history_is_not_scored(client):
    """수집을 시작한 지 며칠 안 된 키워드는 점수를 매기지 않고 '자료부족' 으로 둔다.

    최근 10일이 연속으로 확인돼야 증가율·가속을 계산할 수 있기 때문이다.
    """
    from datetime import timedelta

    from app import dates, db
    from app.trend import pipeline

    with db.conn() as c:
        keyword_id = c.execute(
            "INSERT INTO keywords (term, category, is_sample) VALUES ('(샘플) 막 수집 시작', '빵', TRUE) RETURNING id"
        ).fetchone()["id"]
        for offset, count in enumerate([40, 55, 70]):   # 사흘치만 있다
            c.execute(
                "INSERT INTO daily_metrics (keyword_id, metric_date, source, mention_count, search_ratio, is_complete) "
                "VALUES (%s, %s, 'sample', %s, 50, TRUE)",
                (keyword_id, dates.today() - timedelta(days=3 - offset), count),
            )
    state = pipeline.run(keyword_id, force_insight=True)
    assert state["score"]["stage"] == "자료부족" and state["score"]["score"] == 0.0
    assert state["insight"] is None


def test_vendor_recommendation_prefers_matching_growth_item(client):
    """빵을 만드는 샘플 업체에는 성장기인 빵 키워드가 1순위로 나온다."""
    items = client.get("/v1/vendors/recommendations", params={"vendor": "(샘플) 달콤공방"}).json()["items"]
    assert items[0]["term"] == "(샘플) 피스타치오 크림빵"


def test_missing_region_is_asked(client):
    """지역을 말하지 않으면 검색하지 않고 지역을 되묻는다."""
    body = _recommend(client, "비건 케이크 추천해줘")
    assert body["status"] == "reask" and "지역" in body["question"]


def test_hard_constraints_hold(client):
    """식이·지역·가격 조건을 모두 지키는 결과만 나오고, 영업을 끝낸 업체는 나오지 않는다."""
    body = _recommend(client, "비건 케이크 5만 원 이하로 추천해줘. 강남에서 받을게요")
    assert body["status"] == "ok" and body["results"]
    for result in body["results"]:
        assert "비건" in result["diet_tags"]
        assert "강남" in result["region"]
        assert result["price"] <= 50000
        assert result["shop_name"] != "(샘플) 문닫은집"
        assert "[출처:" in result["reason"]
    assert body["results"][0]["shop_name"] == "(샘플) 초록오븐"


def test_group_order_asks_storage_first(client):
    """단체주문인데 보관 방법을 말하지 않으면 검색하기 전에 되묻는다."""
    body = _recommend(client, "회사 행사 답례품으로 떡 200개 필요해요. 강남으로 2099-12-10 까지 받아야 하고 개별 포장, 세금계산서 필요합니다")
    assert body["status"] == "reask" and "보관" in body["question"]


def test_group_order_respects_capacity(client):
    """단체주문은 수량·준비 기간·포장·세금계산서·보관 조건을 지키는 업체만 나오고, 견적과 공급 조건 확인 상태가 붙는다."""
    body = _recommend(client, "회사 행사 답례품으로 떡 200개 필요해요. 강남으로 2099-12-10 까지 받아야 하고 개별 포장, 세금계산서 필요합니다. 냉장 보관 가능합니다")
    assert body["status"] == "ok" and body["results"]
    for result in body["results"]:
        assert result["max_qty_per_day"] >= 200 and (result["moq"] or 0) <= 200
        assert result["individual_packaging"] is True and result["tax_invoice"] is True
        # 견적 합계는 코드가 계산한 값이다
        assert result["quote"]["quantity"] == 200 and result["quote"]["total_price"] == 200 * result["price"]
        # 200개는 대량 주문이라, 조건이 최신이어도 견적 전에 공급자 확인을 받아야 한다
        assert result["capacity_check"]["status"] == "confirm"
    assert body["audit"]["profile"] == "단체주문" and body["audit"]["preservation"] == 1.0


def test_headcount_and_per_person_budget(client):
    """인원과 1인 예산으로 물어도 수량·가격 상한이 정해지고, 예산을 넘는 메뉴는 나오지 않는다."""
    body = _recommend(client, "2099년 12월 10일에 임직원 200명 간식이 필요합니다. 1인 8천 원 예산이고 개별 포장, 세금계산서 필요해요. 강남에서 받고 냉장 보관 가능합니다")
    assert body["slots"]["headcount"] == 200 and body["slots"]["budget_per_person"] == 8000
    assert body["status"] == "ok" and body["results"]
    for result in body["results"]:
        assert result["price"] <= 8000
        assert result["quote"] == {"quantity": 200, "unit_price": result["price"], "total_price": 200 * result["price"],
                                   "budget": 1600000, "within_budget": True}


def test_stale_capacity_is_flagged_for_reconfirmation(client):
    """확인한 지 오래된 공급 조건으로 맞춘 결과는 "공급자 재확인" 으로 표시된다."""
    body = _recommend(client, "부산 해운대로 소금빵 50개를 2099-12-10 까지 받아야 해요. 개별 포장 필요하고 상온 보관만 가능합니다")
    assert body["status"] == "ok"
    assert body["results"][0]["shop_name"] == "(샘플) 해운대빵집"
    assert body["results"][0]["capacity_check"]["status"] == "reconfirm"


def test_no_match_returns_empty(client):
    """조건을 모두 지키는 업체가 없으면 조건을 어기는 업체를 대신 보여 주지 않고 빈 결과를 돌려준다."""
    body = _recommend(client, "부산 해운대에서 글루텐프리 마카롱 추천해줘")
    assert body["status"] == "empty" and body["results"] == []
    # 막힌 조건을 함께 알린다(이 문의에서는 지역과 식이 조건이 걸려 있다)
    assert {item["condition"] for item in body["blockers"]} >= {"region", "diet"}


def test_structured_output_fallback(client, monkeypatch):
    """모델 서버가 'JSON 양식 강제' 를 지원하지 않는 상황을 흉내 내도, 글로 양식을 설명하는 방법으로 답을 받는다."""
    from langchain_openai import ChatOpenAI

    from app import llm
    from app.reco.slots import Slots

    def refuse(self, *args, **kwargs):
        """양식 강제 기능을 쓰려 하면 항상 실패하게 만든 가짜 함수."""
        raise RuntimeError("json_schema 미지원(시험용)")

    monkeypatch.setattr(ChatOpenAI, "with_structured_output", refuse)
    slots = llm.ask_structured(Slots, "문의에서 조건을 뽑아라.", "문의: 강남에서 쿠키 추천해줘")
    assert isinstance(slots, Slots)

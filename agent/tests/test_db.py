"""실제 PostgreSQL 에서 수집·분석·추천·거래 기록의 회귀 문제를 확인한다. LLM 은 쓰지 않는다.

실행(agent 폴더): DB_TEST=1 DATABASE_URL=… python -m pytest tests/test_db.py -q
전제: vector 확장이 있는 연습용 DB. 스키마도 적용하므로 운영 DB 에서는 실행하지 않는다.
시험이 만든 키워드·업체·프로필·요청은 각 시험 뒤 지운다. DB_TEST 가 없으면 모두 건너뛴다.
LLM 이 필요한 자리(조건 뽑기·추천 사유)는 정해 둔 값과 "LLM 장애" 로 바꿔 끼워, 코드가 만든 글(template)이 나가는 길을 탄다.
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pgvector import Vector

from app import dates, db
import httpx

from app import llm
from app.jobs import collect_instagram, collect_naver, collect_rss, import_catalog
from app.main import app
from app.reco import search, slots as slot_rules
from app.reco.slots import Slots
from app.settings import settings
from app.trend import pipeline, recommend

pytestmark = pytest.mark.skipif(os.environ.get("DB_TEST") != "1", reason="DB_TEST=1 일 때만 실행")


@pytest.fixture(scope="module", autouse=True)
def schema():
    """일반 에이전트 계정으로 DB 구조를 맞춘다. 두 번 실행해도 기존 자료를 유지해야 한다."""
    db.wait_for_db()
    db.apply_schema()
    db.apply_schema()


@pytest.fixture
def records():
    """시험의 이름 앞머리를 만들고 해당 자료만 정리한다. 다른 시험과 이름이 겹치지 않는다."""
    prefix = f"test-{uuid4().hex}"
    yield prefix
    with db.conn() as c:
        c.execute("DELETE FROM keywords WHERE term LIKE %s", (prefix + "%",))
        c.execute("DELETE FROM shops WHERE name LIKE %s", (prefix + "%",))
        c.execute("DELETE FROM vendor_profiles WHERE vendor_key LIKE %s", (prefix + "%",))
        c.execute("DELETE FROM trend_adoptions WHERE vendor_key LIKE %s", (prefix + "%",))
        # 요청에 딸린 제안 옵션·반응·납품·제안서 기록은 연쇄 삭제된다
        c.execute("DELETE FROM group_order_requests WHERE raw_text LIKE %s", (prefix + "%",))


def _keyword(term: str) -> int:
    """시험 키워드를 만들고 번호를 반환한다. 호출자는 records 의 앞머리를 붙여 정리 대상임을 알린다."""
    with db.conn() as c:
        return c.execute("INSERT INTO keywords(term) VALUES (%s) RETURNING id", (term,)).fetchone()["id"]


def _metrics(keyword_id: int, offsets: list[int], *, missing_source: str | None = None) -> None:
    """날짜별 세 출처의 완료된 지표를 넣는다. missing_source 는 최근 5일 전 출처 하나를 빼는 시험용이다."""
    with db.conn() as c:
        for offset in offsets:
            for source in ("naver_blog", "naver_news", "naver_datalab"):
                if offset == 4 and source == missing_source:
                    continue
                # 0도 수집이 완료된 실제 값이다. 누락 날짜는 이 함수가 행을 넣지 않는다
                c.execute(
                    "INSERT INTO daily_metrics(keyword_id, metric_date, source, mention_count, search_ratio, is_complete) "
                    "VALUES (%s, %s, %s, 0, %s, TRUE)",
                    (keyword_id, dates.today() - timedelta(days=offset + 1), source,
                     0.0 if source == "naver_datalab" else None),
                )


def test_sparse_history_is_not_growth_and_old_briefing_is_removed(records):
    """21일 구간의 4일치 자료를 성장기로 오판하지 않고, 같은 날짜의 옛 브리핑도 공개하지 않는다."""
    kid = _keyword(records + "-gaps")
    _metrics(kid, [20, 2, 1, 0])
    with db.conn() as c:
        c.execute("UPDATE daily_metrics SET mention_count=20 WHERE keyword_id=%s AND source='naver_blog'", (kid,))
        c.execute("INSERT INTO trend_insights(keyword_id,score_date,briefing,citations,generated_by) "
                  "VALUES (%s,%s,'old','{}','template')", (kid, dates.today() - timedelta(days=1)))
    state = pipeline.run(kid, force_insight=True)
    assert state["history_days"] == 0
    assert state["score"]["stage"] == "자료부족" and state["score"]["score"] == 0
    assert state["score"]["growth_rate"] == 0 and state["insight"] is None
    with db.conn() as c:
        assert c.execute("SELECT count(*) AS n FROM trend_insights WHERE keyword_id=%s", (kid,)).fetchone()["n"] == 0


@pytest.mark.parametrize("missing_source", ["naver_blog", "naver_news", "naver_datalab", None])
def test_continuous_history_requires_every_source_and_accepts_real_zero(records, missing_source):
    """최근 10일이 0건이어도 두 계열이 완전하면 계산하고, 검색 계열 전체가 빠지면 자료부족으로 둔다."""
    kid = _keyword(records + "-coverage")
    _metrics(kid, list(range(10)), missing_source=missing_source)
    state = pipeline.run(kid)
    assert state["history_days"] == (0 if missing_source == "naver_datalab" else 10)
    assert state["score"]["stage"] == ("자료부족" if missing_source == "naver_datalab" else "태동기")


def test_shared_articles_belong_to_each_keyword_without_duplicates(records, monkeypatch):
    """두 키워드가 같은 기사를 각각 저장하며, 재수집은 중복 행을 만들지 않는다."""
    ids = [_keyword(records + suffix) for suffix in ("-one", "-two")]

    def fetch(client, kind, query):
        """네이버 응답을 고정해 DB 의 키워드별 중복 처리를 확인한다."""
        return collect_naver.SearchResults([
            {"title": "시험 기사", "url": f"https://example.com/{records}/{kind}",
             "snippet": "시험 요약", "posted": dates.today() - timedelta(days=1)}
        ], exhausted=True)

    monkeypatch.setattr(collect_naver, "fetch_search", fetch)
    monkeypatch.setattr(collect_naver, "fetch_datalab", lambda *args: {})
    monkeypatch.setattr(collect_naver.time, "sleep", lambda seconds: None)
    for kid in [*ids, ids[0]]:
        collect_naver.collect_keyword(None, kid, records, dates.today())
    with db.conn() as c:
        counts = c.execute("SELECT keyword_id, count(*) AS n FROM documents WHERE keyword_id=ANY(%s) GROUP BY keyword_id", (ids,)).fetchall()
        assert {row["keyword_id"]: row["n"] for row in counts} == {kid: 2 for kid in ids}
        # 끝까지 조회한 글 없는 날짜는 확인된 0건으로 남는다
        assert c.execute("SELECT mention_count, is_complete FROM daily_metrics WHERE keyword_id=%s "
                         "AND metric_date=%s AND source='naver_blog'", (ids[0], dates.today() - timedelta(days=2))).fetchone() == {
                             "mention_count": 0, "is_complete": True}


def test_blank_capacity_replaces_previous_values(records, tmp_path):
    """공급 조건을 전부 비운 CSV 를 다시 넣으면 이전 생산량·가능 표시가 모두 모름으로 바뀐다."""
    path = tmp_path / "catalog.csv"
    header = "shop_name,region,menu_name,moq,max_qty_per_day,lead_time_days,individual_packaging,tax_invoice\n"
    path.write_text(header + f"{records},서울 강남구,쿠키,100,1000,3,Y,Y\n")
    import_catalog.import_file(str(path))
    path.write_text(header + f"{records},서울 강남구,쿠키,,,,,\n")
    import_catalog.import_file(str(path))
    with db.conn() as c:
        row = c.execute("SELECT c.moq,c.max_qty_per_day,c.lead_time_days,c.individual_packaging,c.tax_invoice "
                        "FROM supplier_capacity c JOIN shops s ON s.id=c.shop_id WHERE s.name=%s", (records,)).fetchone()
    assert row is not None and all(value is None for value in row.values())


@pytest.mark.parametrize("occasion", ["선물", "행사"])
def test_bulk_gift_and_event_search_filters_capacity(records, monkeypatch, occasion):
    """LLM 이 선물·행사로 분류해도 200개 주문은 실제 SQL 에서 생산량과 포장 조건을 지킨다."""
    vector = Vector([1.0] + [0.0] * 1023)
    with db.conn() as c:
        cases = [("good", 500, 2, True, True), ("small", 100, 2, True, True),
                 ("unpacked", 500, 2, False, True), ("late", 500, 11, True, True), ("no-tax", 500, 2, True, False)]
        for suffix, capacity, lead_days, packaging, tax_invoice in cases:
            shop = c.execute("INSERT INTO shops(name,region) VALUES (%s,'서울 강남구') RETURNING id", (records + suffix,)).fetchone()["id"]
            menu = c.execute("INSERT INTO menus(shop_id,name,category,embedding) VALUES (%s,'쿠키','쿠키',%s) RETURNING id",
                             (shop, vector)).fetchone()["id"]
            c.execute("INSERT INTO supplier_capacity(menu_id,shop_id,moq,max_qty_per_day,lead_time_days,individual_packaging,tax_invoice) "
                      "VALUES (%s,%s,10,%s,%s,%s,%s)", (menu, shop, capacity, lead_days, packaging, tax_invoice))
    monkeypatch.setattr(search.llm, "embed", lambda texts: [vector.to_list()])
    slots = Slots(region="강남", dessert="쿠키", quantity=200, occasion=occasion,
                  due_date=(dates.today() + timedelta(days=10)).isoformat(), individual_packaging=True, tax_invoice=True)
    found = search.search(slots, "쿠키 200개")
    assert {row["shop_name"] for row in found} == {records + "good"}


def test_vendor_name_query_preserves_slash_and_other_reserved_characters(records):
    """슬래시·앰퍼샌드·물음표가 든 업체명을 쿼리로 저장하고 같은 프로필을 조회한다."""
    vendor = records + "/한글 & 50%?#"
    headers = {"X-AI-Key": settings.AI_API_KEY}
    with TestClient(app) as client:
        saved = client.put("/v1/vendors/profile", params={"vendor": vendor}, json={"categories": ["빵"]}, headers=headers)
        assert saved.status_code == 200 and saved.json()["vendor_key"] == vendor
        response = client.get("/v1/vendors/recommendations", params={"vendor": vendor}, headers=headers)
        assert response.status_code == 200 and response.json()["profile"]["vendor_key"] == vendor


# ───────────────────── 공급 조건: 상품 단위·확인 표시 ─────────────────────

HEADERS = {"X-AI-Key": settings.AI_API_KEY}
VECTOR = Vector([1.0] + [0.0] * 1023)          # 시험 문의와 뜻이 같은 메뉴의 벡터
OTHER = Vector([0.0, 1.0] + [0.0] * 1022)      # 뜻이 전혀 다른 메뉴의 벡터(유사도 0)


def _shop(name: str, region: str = "서울 강남구", *, vector: Vector = VECTOR, menu: str = "쿠키", price: int | None = 3000,
          storage: str | None = "상온", capacity: dict | None = None, partner: bool = False) -> tuple[int, int]:
    """업체·메뉴 한 쌍(과 공급 조건)을 만들고 (업체 번호, 메뉴 번호)를 돌려준다.

    capacity 를 주면 그 칸만 채운 공급 조건을 넣는다. 기본은 오늘 확인했고 30일 동안 유효한 조건이다.
    """
    with db.conn() as c:
        shop_id = c.execute("INSERT INTO shops(name,region,is_partner,rating,review_count) VALUES (%s,%s,%s,4.5,100) RETURNING id",
                            (name, region, partner)).fetchone()["id"]
        menu_id = c.execute("INSERT INTO menus(shop_id,name,category,price,storage,embedding) VALUES (%s,%s,'쿠키',%s,%s,%s) RETURNING id",
                            (shop_id, menu, price, storage, vector)).fetchone()["id"]
        if capacity is not None:
            row = {"moq": 10, "max_qty_per_day": 1000, "lead_time_days": 2, "individual_packaging": True, "gift_packaging": True,
                   "tax_invoice": True, "delivery_areas": [], "last_verified_at": dates.today(),
                   "valid_until": dates.today() + timedelta(days=30), "confirmation_status": "confirmed", **capacity}
            c.execute(
                "INSERT INTO supplier_capacity(menu_id,shop_id,moq,max_qty_per_day,lead_time_days,individual_packaging,gift_packaging,"
                "tax_invoice,delivery_areas,last_verified_at,valid_until,confirmation_status) "
                "VALUES (%(menu_id)s,%(shop_id)s,%(moq)s,%(max_qty_per_day)s,%(lead_time_days)s,%(individual_packaging)s,"
                "%(gift_packaging)s,%(tax_invoice)s,%(delivery_areas)s,%(last_verified_at)s,%(valid_until)s,%(confirmation_status)s)",
                {**row, "menu_id": menu_id, "shop_id": shop_id})
    return shop_id, menu_id


def _fake_llm(monkeypatch, slots: Slots) -> None:
    """LLM 을 쓰는 자리를 바꿔 끼운다: 조건은 정해 둔 slots, 임베딩은 VECTOR, 글쓰기는 장애(→ 코드가 만든 글)."""
    monkeypatch.setattr(slot_rules, "extract", lambda text, today=None: slots.model_copy(deep=True))
    monkeypatch.setattr(llm, "embed", lambda texts: [VECTOR.to_list() for _text in texts])

    def down(*args, **kwargs):
        """모델 서버가 꺼진 상황을 흉내 낸다."""
        raise RuntimeError("LLM 장애(시험용)")

    monkeypatch.setattr(llm, "ask_structured", down)


def test_capacity_is_kept_per_menu_and_marks_verification(records, tmp_path):
    """같은 업체라도 메뉴마다 공급 조건을 따로 두고, 확인 날짜가 있는 줄만 '확인됨' 이 된다. 유효기간을 비우면 30일이다."""
    path = tmp_path / "catalog.csv"
    path.write_text(
        "shop_name,region,menu_name,storage,max_qty_per_day,delivery_areas,capacity_verified_on,capacity_valid_until\n"
        f"{records},서울 강남구,쿠키,상온,1000,전국;서울,2026-11-01,\n"
        f"{records},서울 강남구,케이크,냉장,50,,,\n")
    import_catalog.import_file(str(path))
    with db.conn() as c:
        rows = c.execute(
            "SELECT m.name, m.storage, c.max_qty_per_day, c.delivery_areas, c.confirmation_status, c.last_verified_at, c.valid_until "
            "FROM supplier_capacity c JOIN menus m ON m.id=c.menu_id JOIN shops s ON s.id=c.shop_id WHERE s.name=%s",
            (records,)).fetchall()
    # DB 의 한글 정렬 순서에 기대지 않게 메뉴 이름으로 찾는다
    by_name = {row["name"]: row for row in rows}
    cake, cookie = by_name["케이크"], by_name["쿠키"]
    assert (cookie["storage"], cookie["max_qty_per_day"], cookie["delivery_areas"]) == ("상온", 1000, ["전국", "서울"])
    assert cookie["confirmation_status"] == "confirmed" and cookie["valid_until"] - cookie["last_verified_at"] == timedelta(days=30)
    assert (cake["storage"], cake["max_qty_per_day"]) == ("냉장", 50)
    assert cake["confirmation_status"] == "unverified" and cake["valid_until"] is None


def test_catalog_rejects_unknown_storage(records, tmp_path):
    """보관 방법이 상온·냉장·냉동이 아니면 몇 번째 줄인지 알리고 아무것도 넣지 않는다."""
    path = tmp_path / "catalog.csv"
    path.write_text(f"shop_name,region,menu_name,storage\n{records},서울 강남구,쿠키,서늘한곳\n")
    with pytest.raises(ValueError, match="2번 줄"):
        import_catalog.import_file(str(path))
    with db.conn() as c:
        assert c.execute("SELECT count(*) AS n FROM shops WHERE name=%s", (records,)).fetchone()["n"] == 0


def test_confirm_capacity_sets_validity_and_pending_keeps_it(records):
    """확인 표시는 오늘 날짜와 유효기간을 적고, 재확인 요청(pending)은 상태만 바꾼다. 공급 조건이 없으면 404."""
    shop_id, menu_id = _shop(records + "confirm", capacity={"confirmation_status": "unverified", "last_verified_at": None, "valid_until": None})
    bare_shop, _menu = _shop(records + "bare")
    with TestClient(app) as client:
        done = client.post("/v1/suppliers/capacity/confirm", json={"shop_id": shop_id, "valid_days": 7}, headers=HEADERS)
        assert done.status_code == 200
        row = done.json()["updated"][0]
        assert (row["menu_id"], row["confirmation_status"]) == (menu_id, "confirmed")
        assert row["last_verified_at"] == dates.today().isoformat()
        assert row["valid_until"] == (dates.today() + timedelta(days=7)).isoformat()
        waiting = client.post("/v1/suppliers/capacity/confirm", json={"shop_id": shop_id, "status": "pending"}, headers=HEADERS).json()
        assert waiting["updated"][0]["confirmation_status"] == "pending"
        assert waiting["updated"][0]["valid_until"] == row["valid_until"]
        assert client.post("/v1/suppliers/capacity/confirm", json={"shop_id": bare_shop}, headers=HEADERS).status_code == 404


# ───────────────────── 검색: 합치기·보관·배송 지역·막힌 조건 ─────────────────────

def test_keyword_search_rescues_a_named_shop(records, monkeypatch):
    """의미 검색 상위에 못 든 업체도 이름이 문의에 나오면 후보에 든다(의미 검색만 쓰면 놓친다)."""
    region = records[-12:]   # 다른 자료와 섞이지 않게 이 시험만의 지역 이름을 쓴다
    _shop(records + "가까운집1", region=region)
    # 둘째 업체는 뜻이 조금 덜 가깝게 둔다. 유사도가 똑같으면 DB 가 돌려주는 순서가 정해져 있지 않기 때문이다
    _shop(records + "가까운집2", region=region, vector=Vector([1.0, 0.3] + [0.0] * 1022))
    _shop(records + "이름난집", region=region, vector=OTHER)
    monkeypatch.setattr(search.llm, "embed", lambda texts: [VECTOR.to_list()])
    # 후보를 2개로 줄여, 뜻이 먼 업체가 의미 검색만으로는 들어올 수 없게 한다
    monkeypatch.setattr(settings, "RECO_CANDIDATES", 2)
    found = {row["shop_name"].removeprefix(records): row for row in search.search(Slots(region=region), f"{records}이름난집 주문하고 싶어요")}
    assert set(found) == {"가까운집1", "이름난집"}
    assert found["이름난집"]["vector_rank"] is None and found["이름난집"]["keyword_rank"] == 1
    assert found["이름난집"]["keyword_hits"] == 1 and found["가까운집1"]["keyword_rank"] is None
    # 이름을 말하지 않으면 뜻이 가까운 업체만 나온다
    plain = search.search(Slots(region=region), "과자 주문하고 싶어요")
    assert {row["shop_name"].removeprefix(records) for row in plain} == {"가까운집1", "가까운집2"}


@pytest.mark.parametrize("storage, expected", [("상온", {"상온"}), ("냉장", {"상온", "냉장"}), ("무관", {"상온", "냉장", "모름"}), (None, {"상온", "냉장", "모름"})])
def test_storage_filter_respects_customer_facility(records, monkeypatch, storage, expected):
    """상온만 되는 곳에는 냉장 제품과 보관 방법을 모르는 제품을 내놓지 않는다. 냉장이 되면 상온 제품도 된다."""
    _shop(records + "상온", storage="상온")
    _shop(records + "냉장", storage="냉장")
    _shop(records + "모름", storage=None)
    monkeypatch.setattr(search.llm, "embed", lambda texts: [VECTOR.to_list()])
    found = search.search(Slots(region="강남", storage=storage), "쿠키")
    assert {row["shop_name"].removeprefix(records) for row in found if row["shop_name"].startswith(records)} == expected


def test_delivery_area_lets_remote_shops_match(records, monkeypatch):
    """다른 지역 업체는 배송 가능 지역에 요청 지역이나 '전국' 이 있을 때만 후보가 되고, 맞은 방식이 남는다."""
    _shop(records + "local")
    _shop(records + "delivery", region="부산 해운대구", capacity={"delivery_areas": ["서울 강남구"]})
    _shop(records + "nationwide", region="부산 해운대구", capacity={"delivery_areas": ["전국"]})
    _shop(records + "elsewhere", region="부산 해운대구", capacity={"delivery_areas": ["대구"]})
    monkeypatch.setattr(search.llm, "embed", lambda texts: [VECTOR.to_list()])
    found = {row["shop_name"].removeprefix(records): row["region_match"]
             for row in search.search(Slots(region="강남"), "쿠키") if row["shop_name"].startswith(records)}
    assert found == {"local": "local", "delivery": "delivery", "nationwide": "nationwide"}


def test_budget_caps_unit_price(records, monkeypatch):
    """1인 예산이 메뉴 1개 가격의 상한이 된다. 가격을 모르는 메뉴는 예산 안이라고 할 수 없어 빠진다."""
    _shop(records + "cheap", price=7000, capacity={})
    _shop(records + "pricey", price=9000, capacity={})
    _shop(records + "unknown", price=None, capacity={})
    monkeypatch.setattr(search.llm, "embed", lambda texts: [VECTOR.to_list()])
    slots = Slots(region="강남", headcount=200, budget_per_person=8000, due_date=(dates.today() + timedelta(days=10)).isoformat())
    assert {row["shop_name"] for row in search.search(slots, "간식") if row["shop_name"].startswith(records)} == {records + "cheap"}


def test_diagnose_names_the_blocking_condition(records):
    """조건에 맞는 업체가 없을 때, 어느 조건 하나만 빼면 몇 곳이 맞는지 알려 준다."""
    region = records[-12:]   # 다른 시험의 업체와 섞이지 않게 이 시험만의 지역 이름을 쓴다
    _shop(records + "late", region=region, capacity={"lead_time_days": 30})
    slots = Slots(region=region, quantity=200, due_date=(dates.today() + timedelta(days=5)).isoformat(),
                  tax_invoice=True, storage="상온")
    blockers = {item["condition"]: item["shops_if_relaxed"] for item in search.diagnose(slots, "쿠키 200개")}
    assert blockers["due_date"] == 1                      # 날짜만 풀면 한 곳이 맞는다
    assert blockers["quantity"] == 0 and blockers["tax_invoice"] == 0 and blockers["storage"] == 0
    assert "diet" not in blockers                         # 고객이 걸지 않은 조건은 세지 않는다


# ───────────────────── 추천 → 반응 → 대체 공급자 → 제안서 → 납품 → 지표 ─────────────────────

def test_group_order_flow_from_request_to_delivery(records, monkeypatch):
    """요청부터 납품까지 한 줄로 이어 본다: 견적·확인 상태 → 공급자 거절 → 대체 공급자 → 제안서 → 납품 → 지표."""
    region = records[-12:]
    fresh_shop, _menu = _shop(records + "fresh", region=region, capacity={})
    stale_shop, _menu = _shop(records + "stale", region=region, capacity={"valid_until": dates.today() - timedelta(days=1)})
    slots = Slots(region=region, headcount=50, budget_per_person=4000, storage="상온",
                  due_date=(dates.today() + timedelta(days=10)).isoformat(), occasion="단체주문")
    _fake_llm(monkeypatch, slots)
    with TestClient(app) as client:
        first = client.post("/v1/recommend", json={"text": records + " 임직원 50명 간식"}, headers=HEADERS).json()
        assert first["status"] == "ok" and first["round"] == 1 and first["blockers"] == []
        by_shop = {result["shop_id"]: result for result in first["results"]}
        assert set(by_shop) == {fresh_shop, stale_shop}
        # 견적은 코드가 계산한다: 50개 × 3000원 = 150000원, 총예산 50명 × 4000원 = 200000원 이내
        assert by_shop[fresh_shop]["quote"] == {"quantity": 50, "unit_price": 3000, "total_price": 150000,
                                               "budget": 200000, "within_budget": True}
        assert by_shop[fresh_shop]["capacity_check"]["status"] == "auto"
        assert by_shop[stale_shop]["capacity_check"]["status"] == "reconfirm"
        # 조건이 오래된 업체는 공급 능력 점수가 낮아 아래에 온다
        assert first["results"][0]["shop_id"] == fresh_shop
        assert first["audit"]["preservation"] == 1.0 and first["results"][0]["generated_by"] == "template"
        assert "150000" in first["results"][0]["reason"]

        # 1위 공급자가 거절 → 그 업체를 뺀 대체 공급자를 다음 회차로 제안한다
        top = first["results"][0]["quote_option_id"]
        declined = client.post("/v1/recommend/feedback", json={"quote_option_id": top, "action": "supplier_declined",
                                                               "reason": "그 주 휴무"}, headers=HEADERS).json()
        assert declined["request_id"] == first["request_id"]
        second = client.post(f"/v1/recommend/{first['request_id']}/reroute", headers=HEADERS).json()
        assert second["round"] == 2 and [result["shop_id"] for result in second["results"]] == [stale_shop]

        # 제안서 초안은 가장 최근 회차로 만들고, 채택 결과를 남긴다
        draft = client.post(f"/v1/recommend/{first['request_id']}/proposal", headers=HEADERS).json()
        option = second["results"][0]["quote_option_id"]
        assert draft["round"] == 2 and draft["option_ids"] == [option]
        assert "50개 × 3,000원 = 150,000원" in draft["body"] and "재확인" in draft["body"] and "[출처" in draft["body"]
        adopted = client.post(f"/v1/recommend/proposals/{draft['proposal_id']}/outcome", json={"outcome": "adopted"}, headers=HEADERS)
        assert adopted.status_code == 200 and adopted.json()["outcome"] == "adopted"

        # 공급자 수락 → 주문 → 납품까지 끝나야 "거래 가능 공급자" 다
        for action in ("selected", "supplier_accepted", "ordered"):
            assert client.post("/v1/recommend/feedback", json={"quote_option_id": option, "action": action, "order_ref": "order-test" if action == "ordered" else None}, headers=HEADERS).status_code == 200
        with db.conn() as c:
            assert c.execute("SELECT tradable FROM quote_outcomes WHERE quote_option_id=%s", (option,)).fetchone()["tradable"] is False
        assert client.post("/v1/recommend/delivery", json={"quote_option_id": option, "event": "delivered", "order_ref": "order-test"}, headers=HEADERS).status_code == 200
        with db.conn() as c:
            outcome = c.execute("SELECT * FROM quote_outcomes WHERE quote_option_id=%s", (option,)).fetchone()
            assert outcome["tradable"] and outcome["ordered"] and not outcome["supplier_declined"]
            assert c.execute("SELECT supplier_declined, tradable FROM quote_outcomes WHERE quote_option_id=%s", (top,)).fetchone() == {
                "supplier_declined": True, "tradable": False}

        # 지표는 위 기록을 센다(다른 시험의 기록이 섞일 수 있어 "이상" 으로 본다)
        kpi = client.get("/v1/kpi", params={"days": 1}, headers=HEADERS).json()
        assert kpi["reco"]["requests"]["ok"] >= 1 and kpi["reco"]["outcomes"]["tradable"] >= 1
        assert kpi["reco"]["outcomes"]["supplier_declined"] >= 1 and kpi["reco"]["proposals"]["adopted"] >= 1
        assert kpi["reco"]["capacity_check"]["reconfirm"] >= 2 and kpi["reco"]["preservation_avg"] is not None
        assert 0 < kpi["reco"]["supplier_accept_rate"] < 1 or kpi["reco"]["supplier_accept_rate"] in (0, 1)

        # 없는 번호와 되묻기 요청은 거절한다
        assert client.post("/v1/recommend/999999999/reroute", headers=HEADERS).status_code == 404
        assert client.post("/v1/recommend/999999999/proposal", headers=HEADERS).status_code == 404
        assert client.post("/v1/recommend/delivery", json={"quote_option_id": 999999999, "event": "delivered"}, headers=HEADERS).status_code == 404


def test_empty_result_reports_blockers_and_cannot_be_rerouted(records, monkeypatch):
    """조건에 맞는 업체가 없으면 어긴 업체를 대신 내놓지 않고 막힌 조건을 알린다. 그런 요청은 재탐색·제안서 대상이 아니다."""
    region = records[-12:]
    _shop(records + "late", region=region, capacity={"lead_time_days": 30})
    slots = Slots(region=region, quantity=200, storage="무관", due_date=(dates.today() + timedelta(days=5)).isoformat())
    _fake_llm(monkeypatch, slots)
    with TestClient(app) as client:
        body = client.post("/v1/recommend", json={"text": records + " 쿠키 200개"}, headers=HEADERS).json()
        assert body["status"] == "empty" and body["results"] == [] and body["audit"] is None
        assert body["blockers"][0] == {"condition": "due_date", "label": "받을 날짜(준비 기간)", "shops_if_relaxed": 1}
        assert client.post(f"/v1/recommend/{body['request_id']}/reroute", headers=HEADERS).status_code == 409
        assert client.post(f"/v1/recommend/{body['request_id']}/proposal", headers=HEADERS).status_code == 409


# ───────────────────── 트렌드: 교차검증·긴 시간창·채택 기록 ─────────────────────

def test_mismatched_signal_is_stored_and_holds_vendor_timing(records):
    """글만 늘고 검색은 그대로면 '불일치' 로 저장하고, 업체 추천의 시기 점수를 낮춘다. 91일이 차 있으면 긴 시간창도 판정한다."""
    with db.conn() as c:
        kid = c.execute("INSERT INTO keywords(term, category, is_sample) VALUES (%s, '빵', TRUE) RETURNING id",
                        (records + "-mismatch",)).fetchone()["id"]
        mentions = [20] * 81 + [22, 25, 29, 34, 40, 48, 58, 70, 85, 104]   # 최근 10일만 가속
        for offset, count in enumerate(mentions):
            c.execute("INSERT INTO daily_metrics(keyword_id, metric_date, source, mention_count, search_ratio, is_complete) "
                      "VALUES (%s, %s, 'sample', %s, 50, TRUE)", (kid, dates.today() - timedelta(days=91 - offset), count))
    score = pipeline.run(kid)["score"]
    assert score["stage"] == "성장기" and score["signal_check"] == "불일치" and score["search_growth"] == 0
    assert score["windows"]["7d"]["stage"] == "성장기" and score["windows"]["3m"]["stage"] == "태동기"
    with db.conn() as c:
        saved = c.execute("SELECT signal_check, windows FROM trend_scores WHERE keyword_id=%s", (kid,)).fetchone()
    assert saved["signal_check"] == "불일치" and saved["windows"]["3m"]["growth_rate"] == 0
    item = next(item for item in recommend.recommend(records, top_n=20)["items"] if item["keyword_id"] == kid)
    assert item["timing"] == recommend.HOLD_TIMING[0] and item["signal_check"] == "불일치"


def test_ad_documents_are_not_used_as_evidence(records):
    """광고·체험단으로 표시된 글은 브리핑 근거에 들어가지 않는다."""
    with db.conn() as c:
        kid = c.execute("INSERT INTO keywords(term, is_sample) VALUES (%s, TRUE) RETURNING id", (records + "-ads",)).fetchone()["id"]
        for offset in range(10):
            c.execute("INSERT INTO daily_metrics(keyword_id, metric_date, source, mention_count, search_ratio, is_complete) "
                      "VALUES (%s, %s, 'sample', 30, 50, TRUE)", (kid, dates.today() - timedelta(days=10 - offset)))
        for number, is_ad in enumerate([True, False]):
            c.execute("INSERT INTO documents(keyword_id, source, title, url, posted_at, is_ad) VALUES (%s, 'sample', %s, %s, %s, %s)",
                      (kid, f"글 {number}", f"https://example.com/{records}/{number}", dates.today() - timedelta(days=1), is_ad))
    assert pipeline.run(kid, force_insight=True)["insight"] is not None
    with db.conn() as c:
        citations = c.execute("SELECT citations FROM trend_insights WHERE keyword_id=%s", (kid,)).fetchone()["citations"]
        urls = {item["url"] for key, item in citations.items() if key.startswith("D")}
    assert urls == {f"https://example.com/{records}/1"}


def test_vendor_adoption_snapshots_the_score(records):
    """업체의 채택 기록에는 그때의 단계·점수가 함께 남는다. 없는 키워드는 404."""
    kid = _keyword(records + "-adopt")
    with db.conn() as c:
        c.execute("INSERT INTO trend_scores(keyword_id, score_date, score, stage, growth_rate, momentum, n_sources, mentions_7d) "
                  "VALUES (%s, %s, 71.5, '성장기', 0.4, 0.1, 3, 500)", (kid, dates.today()))
    with TestClient(app) as client:
        saved = client.post("/v1/vendors/adoptions", params={"vendor": records + "/업체"},
                            json={"keyword_id": kid, "action": "adopted", "note": "시험 판매"}, headers=HEADERS)
        assert saved.status_code == 200
        assert (saved.json()["stage"], saved.json()["trend_score"]) == ("성장기", 71.5)
        missing = client.post("/v1/vendors/adoptions", params={"vendor": records}, json={"keyword_id": 999999999, "action": "adopted"}, headers=HEADERS)
        assert missing.status_code == 404
        kpi = client.get("/v1/kpi", params={"days": 1}, headers=HEADERS).json()
        assert kpi["trend"]["adoptions"]["adopted"] >= 1 and kpi["trend"]["adoption_rate"] is not None


# ───────────────────── 보조 출처: 인스타그램·RSS ─────────────────────

def test_instagram_collect_saves_yesterday_count_and_quota_row(records, monkeypatch):
    """인스타그램 수집은 어제 날짜의 게시물 수·좋아요·댓글 합을 저장하고, 7일 한도를 세기 위해 조회 사실을 적는다."""
    kid = _keyword(records + "소금빵")
    today = dates.today()
    stamp = (today - timedelta(days=1)).isoformat() + "T03:00:00+0000"   # 한국 시각으로 어제 낮 12시
    monkeypatch.setattr(collect_instagram.time, "sleep", lambda seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        """해시태그 번호 조회와 최근 게시물 조회에 정해 둔 답을 돌려준다."""
        if request.url.path.endswith("/ig_hashtag_search"):
            return httpx.Response(200, json={"data": [{"id": "178"}]})
        return httpx.Response(200, json={"data": [{"id": "1", "timestamp": stamp, "like_count": 5, "comments_count": 1},
                                                  {"id": "2", "timestamp": stamp, "like_count": 7, "comments_count": 0}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # 겹친 두 조회가 어제 전체를 덮는다. 같은 게시물을 두 번 받아도 두 건으로 유지한다.
        for stamp_now in (datetime.combine(today, time(0, 10), dates.SEOUL) - timedelta(hours=6),
                          datetime.combine(today, time(0, 10), dates.SEOUL)):
            assert collect_instagram.collect_keyword(client, kid, "소금빵", None, stamp_now.date(), now=stamp_now) is True
    with db.conn() as c:
        row = c.execute("SELECT metric_date, mention_count, is_complete, extra FROM daily_metrics WHERE keyword_id=%s AND source='instagram' AND metric_date=%s",
                        (kid, today - timedelta(days=1))).fetchone()
        quota = c.execute("SELECT hashtag, hashtag_id, last_queried FROM instagram_hashtags WHERE keyword_id=%s", (kid,)).fetchone()
    assert row == {"metric_date": today - timedelta(days=1), "mention_count": 2, "is_complete": True, "extra": {"likes": 12, "comments": 1, "coverage_version": "v2"}}
    assert quota == {"hashtag": "소금빵", "hashtag_id": "178", "last_queried": today}


def test_rss_save_records_counts_and_documents_under_feed_name(records):
    """피드에서 받은 글은 'rss:매체이름' 출처로 날짜별 수와 글 메타 정보가 저장된다. 키워드와 무관한 글은 저장하지 않는다."""
    kid = _keyword(records + "크룽지")
    today = dates.today()
    items = [{"title": "크룽지 후기", "url": f"https://example.com/{records}/1", "snippet": "바삭", "posted": today - timedelta(days=1), "skip": False},
             {"title": "다른 글", "url": f"https://example.com/{records}/2", "snippet": "", "posted": today - timedelta(days=1), "skip": True},
             {"title": "옛 글", "url": f"https://example.com/{records}/3", "snippet": "", "posted": today - timedelta(days=3), "skip": True}]
    collect_rss.save(kid, "rss:시험매체", items, today)
    with db.conn() as c:
        counts = {row["metric_date"]: row["mention_count"] for row in c.execute(
            "SELECT metric_date, mention_count FROM daily_metrics WHERE keyword_id=%s AND source='rss:시험매체' AND is_complete", (kid,)).fetchall()}
        urls = [row["url"] for row in c.execute("SELECT url FROM documents WHERE keyword_id=%s", (kid,)).fetchall()]
    assert counts == {today - timedelta(days=2): 0, today - timedelta(days=1): 1}
    assert urls == [f"https://example.com/{records}/1"]


def test_supporting_source_joins_cross_check_but_not_the_score(records):
    """보조 출처(인스타그램)는 교차검증·출처 다양성·출처별 기록에는 쓰이지만, 점수의 재료(글 수)에는 더해지지 않는다."""
    kid = _keyword(records + "-social")
    _metrics(kid, list(range(10)))   # 네이버 세 출처는 10일 내내 0건(확인된 값)
    with db.conn() as c:
        for offset, count in enumerate([10] * 7 + [40] * 3):   # 인스타그램만 최근 3일에 급증
            c.execute("INSERT INTO daily_metrics(keyword_id, metric_date, source, mention_count, is_complete) VALUES (%s, %s, 'instagram', %s, TRUE)",
                      (kid, dates.today() - timedelta(days=10 - offset), count))
    score = pipeline.run(kid)["score"]
    assert score["stage"] == "태동기" and score["growth_rate"] == 0 and score["mentions_7d"] == 0   # 점수의 재료는 그대로
    assert score["by_source"]["families"] == {"글": 0.0, "검색": 0.0, "SNS": 3.0}
    assert score["by_source"]["sources"]["instagram"] == {"family": "SNS", "growth_rate": 3.0, "total_7d": 160.0}
    assert score["signal_check"] == "불일치" and score["n_sources"] == 1    # SNS 만 오르면 판단을 미룬다
    with db.conn() as c:
        saved = c.execute("SELECT by_source FROM trend_scores WHERE keyword_id=%s", (kid,)).fetchone()["by_source"]
    assert set(saved["sources"]) == {"naver_blog", "naver_news", "naver_datalab", "instagram"}


def test_keyword_aliases_are_saved_and_kept_when_not_sent(records):
    """키워드의 다른 표기를 저장하고, 다른 표기를 보내지 않은 수정에서는 기존 값을 그대로 둔다."""
    term = records + "두바이초콜릿"
    with TestClient(app) as client:
        made = client.post("/v1/keywords", json={"term": term, "category": "초콜릿", "aliases": ["Dubai chocolate", " "]}, headers=HEADERS).json()
        assert made["aliases"] == ["Dubai chocolate"]
        kept = client.post("/v1/keywords", json={"term": term, "category": "디저트"}, headers=HEADERS).json()
        assert (kept["category"], kept["aliases"]) == ("디저트", ["Dubai chocolate"])
        listed = next(row for row in client.get("/v1/keywords", headers=HEADERS).json() if row["term"] == term)
        assert listed["aliases"] == ["Dubai chocolate"]


def _recommended_option(records, monkeypatch, client):
    """이번 시험만의 업체와 추천 옵션을 만든다. 주문 상태·근거 재조회 시험의 공통 준비다."""
    region = records[-12:]
    shop_id, menu_id = _shop(records + "-snapshot", region=region, capacity={})
    _fake_llm(monkeypatch, Slots(region=region, quantity=50, storage="상온", total_budget=200000,
                               due_date=(dates.today() + timedelta(days=10)).isoformat()))
    response = client.post("/v1/recommend", json={"text": records + " 쿠키 50개"}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["status"] == "ok"
    return response.json(), shop_id, menu_id


def test_computed_budget_filters_before_recommendation(records, monkeypatch):
    """100명·200개·1인 8000원 문의의 140만원 후보를 제외하고, 80만원 이내 후보만 저장한다."""
    region = records[-12:]
    _shop(records + "-expensive", region=region, price=7000, capacity={})
    good, _ = _shop(records + "-affordable", region=region, price=4000, capacity={})
    _fake_llm(monkeypatch, Slots(region=region, headcount=100, quantity=200, budget_per_person=8000, storage="상온",
                               due_date=(dates.today() + timedelta(days=10)).isoformat()))
    with TestClient(app) as client:
        body = client.post("/v1/recommend", json={"text": records + " 100명 200개"}, headers=HEADERS).json()
    assert body["status"] == "ok" and [r["shop_id"] for r in body["results"]] == [good]
    assert body["results"][0]["quote"]["total_price"] == 800000
    assert body["results"][0]["quote"]["within_budget"] is True


def test_proposal_keeps_original_evidence_after_catalog_edit(records, monkeypatch):
    """추천 뒤 메뉴 이름·가격이 바뀌어도 재조회·제안서에는 당시 값과 출처가 남는다. 근거 없는 옛 행은 재생성한다."""
    with TestClient(app) as client:
        body, shop_id, menu_id = _recommended_option(records, monkeypatch, client)
        option = body["results"][0]["quote_option_id"]
        before = client.get(f"/v1/recommend/options/{option}", headers=HEADERS).json()
        assert before["citations"]["F1"] and before["snapshot"]["constraints_passed"]
        with db.conn() as c:
            c.execute("UPDATE menus SET name='바뀐 메뉴', price=99999 WHERE id=%s", (menu_id,))
            c.execute("UPDATE shops SET name=%s WHERE id=%s", (records + "-renamed", shop_id))
        after = client.get(f"/v1/recommend/options/{option}", headers=HEADERS).json()
        assert after == before
        draft = client.post(f"/v1/recommend/{body['request_id']}/proposal", headers=HEADERS).json()["body"]
        assert "3,000원" in draft and "바뀐 메뉴" not in draft and "[출처:F1]" in draft
        assert before["citations"]["F1"]["text"] in draft
        with db.conn() as c:
            c.execute("UPDATE quote_options SET citations='{}', snapshot='{}' WHERE id=%s", (option,))
        assert client.post(f"/v1/recommend/{body['request_id']}/proposal", headers=HEADERS).status_code == 409


def test_order_states_retries_and_matching_delivery(records, monkeypatch):
    """최종 의사·수락 뒤 주문·주문 번호별 납품을 검증하고 같은 event_key 재시도는 중복 집계하지 않는다."""
    with TestClient(app) as client:
        body, _, _ = _recommended_option(records, monkeypatch, client)
        option = body["results"][0]["quote_option_id"]
        def feedback(action, **extra):
            return client.post("/v1/recommend/feedback", json={"quote_option_id": option, "action": action, **extra}, headers=HEADERS)
        def delivery(ref):
            return client.post("/v1/recommend/delivery", json={"quote_option_id": option, "event": "delivered", "order_ref": ref}, headers=HEADERS)
        assert feedback("ordered", order_ref="A").status_code == 409
        first = feedback("supplier_accepted", event_key="accept-1")
        assert first.status_code == 200 and feedback("supplier_accepted", event_key="accept-1").json() == first.json()
        assert feedback("supplier_declined", event_key="accept-1").status_code == 409
        assert feedback("supplier_declined").status_code == 200
        assert delivery("A").status_code == 409
        with db.conn() as c:
            state = c.execute("SELECT * FROM quote_outcomes WHERE quote_option_id=%s", (option,)).fetchone()
        assert state["supplier_declined"] and not state["supplier_accepted"] and not state["tradable"]
        assert feedback("supplier_accepted", event_key="accept-2").status_code == 200
        assert feedback("ordered").status_code == 409 and delivery("A").status_code == 409
        order = feedback("ordered", order_ref="A", event_key="order-1")
        assert order.status_code == 200 and feedback("ordered", order_ref="A", event_key="order-1").json() == order.json()
        assert delivery("B").status_code == 409
        delivered = delivery("A")
        assert delivered.status_code == 200 and delivery("A").json() == delivered.json()
        with db.conn() as c:
            state = c.execute("SELECT * FROM quote_outcomes WHERE quote_option_id=%s", (option,)).fetchone()
            assert state["tradable"] and state["ordered"] and state["delivered"]
            assert c.execute("SELECT count(*) AS n FROM delivery_cs_logs WHERE quote_option_id=%s", (option,)).fetchone()["n"] == 1
        assert feedback("supplier_declined").status_code == 200
        with db.conn() as c:
            assert not c.execute("SELECT tradable FROM quote_outcomes WHERE quote_option_id=%s", (option,)).fetchone()["tradable"]


@pytest.mark.parametrize("items,exhausted", [([{"id": "1", "timestamp": "bad"}], True), ([], False), ([], True)])
def test_instagram_incomplete_observation_is_not_completed_zero(records, monkeypatch, items, exhausted):
    """잘못된 시각·페이지 한도·첫날 00:10 조회만으로는 완료된 0건을 만들지 않는다."""
    kid = _keyword(records + "-ig-invalid")
    now = datetime.combine(dates.today(), time(0, 10), dates.SEOUL)
    monkeypatch.setattr(collect_instagram, "fetch_recent_media", lambda *args: (items, exhausted))
    with httpx.Client() as client:
        collect_instagram.collect_keyword(client, kid, "쿠키", "178", now.date(), now=now)
    with db.conn() as c:
        rows = c.execute("SELECT is_complete FROM daily_metrics WHERE keyword_id=%s", (kid,)).fetchall()
    assert rows and all(not r["is_complete"] for r in rows)


def test_non_naver_database_pipeline_matches_replay(records):
    """DB에 RSS·인스타그램만 있어도 운영 분석이 되고, 같은 자료 재생의 점수·단계·근거와 같다."""
    from app.trend import backtest
    kid = _keyword(records + "-fallback")
    rows = []
    with db.conn() as c:
        for i, count in enumerate([10, 10, 10, 12, 15, 20, 30, 45, 70, 100]):
            day = dates.today() - timedelta(days=10 - i)
            for source in ("rss:시험", "instagram"):
                c.execute("INSERT INTO daily_metrics(keyword_id,metric_date,source,mention_count,is_complete) VALUES (%s,%s,%s,%s,TRUE)", (kid, day, source, count))
                rows.append({"metric_date": day, "source": source, "mention_count": count, "search_ratio": None, "is_complete": True})
    actual = pipeline.run(kid)["score"]
    expected = backtest.evaluate_metrics("쿠키", rows, day)["replay"][-1]
    assert actual["stage"] == "성장기" and actual["search_growth"] is None
    assert actual == {k: v for k, v in expected.items() if k != "day"}


def test_schema_invalidates_unproven_instagram_counts_only(records):
    """스키마 재적용은 관측 증거 없는 옛 완료 표시만 해제하고 v2 집계와 숫자는 보존한다."""
    kid = _keyword(records + "-legacy-ig")
    with db.conn() as c:
        for offset, extra in [(1, '{}'), (2, '{"coverage_version":"v2"}')]:
            c.execute("INSERT INTO daily_metrics(keyword_id,metric_date,source,mention_count,is_complete,extra) "
                      "VALUES (%s,%s,'instagram',5,TRUE,%s)", (kid, dates.today() - timedelta(days=offset), extra))
    db.apply_schema()
    db.apply_schema()
    with db.conn() as c:
        rows = c.execute("SELECT mention_count,is_complete FROM daily_metrics WHERE keyword_id=%s ORDER BY metric_date DESC", (kid,)).fetchall()
    assert rows == [{"mention_count": 5, "is_complete": False}, {"mention_count": 5, "is_complete": True}]

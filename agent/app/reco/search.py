"""후보 검색 — 의미 검색과 키워드 검색을 나란히 돌려 합치고, "절대 어기면 안 되는 조건" 은 SQL 로 정확히 거른다 (안건 2 · 기획 03 §5.2).

쓰는 곳: reco/graph.py 의 retrieve·no_result 단계.
세 가지를 묶는 이유:
  - 의미 검색(벡터 유사도)은 "입에서 녹는 부드러운" 같은 표현을 알아듣지만, 업체 이름·메뉴 이름을 콕 집어 말한 문의를 놓칠 수 있다.
  - 키워드 검색은 문의에 나온 이름을 정확히 잡지만 뜻을 읽지 못한다. 그래서 둘을 따로 줄 세운 뒤 순위를 합친다(RRF).
  - 둘 다 비건·지역·수량 같은 조건을 어길 수 있어, 어기면 안 되는 조건은 SQL 의 WHERE 로 먼저 정확히 거른다.
    뜻이 아무리 가까워도 조건을 어기면 후보가 되지 못한다.
용어:
  - RRF(Reciprocal Rank Fusion): 여러 순위표를 합치는 방법. 순위표마다 1 ÷ (RRF_K + 순위)를 더한다. 두 표에서 모두 위에 있을수록 점수가 크다.
  - 키워드 검색은 "메뉴·업체 이름의 낱말이 문의 글에 들어 있는 개수" 로 줄 세운다. 한국어는 조사가 붙어('케이크를')
    문의의 낱말로 이름을 찾기 어렵기 때문에, 반대로 이름의 낱말을 문의에서 찾는다.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from pgvector import Vector

from app import dates, db, llm
from app.reco.slots import STORAGE_LEVELS, Slots, is_group_order, order_quantity, price_cap, storage_level
from app.settings import settings

RRF_K = 60  # RRF 의 완충값. 클수록 1위와 10위의 차이가 줄어든다(널리 쓰는 기본값)

# 보관 방법을 설비 수준 숫자로 바꾸는 SQL 조각. slots.STORAGE_LEVELS 에서 만들어 두 곳의 순서가 어긋나지 않게 한다
_STORAGE_CASE = "CASE m.storage " + " ".join(f"WHEN '{name}' THEN {level}" for name, level in STORAGE_LEVELS.items()) + " END"

# 요청 지역에 맞는지: 업체가 그 지역에 있거나, 배송 가능 지역에 그 지역(또는 '전국')이 있어야 한다.
# 지역 낱말은 어느 쪽이 더 길지 몰라 양쪽으로 포함 여부를 본다('강남' ⊂ '서울 강남구', '서울' ⊂ '서울 강남구').
_REGION_OK = """(s.region LIKE '%%' || %(region)s::text || '%%'
       OR EXISTS (SELECT 1 FROM unnest(c.delivery_areas) AS area
                  WHERE area = '전국' OR area LIKE '%%' || %(region)s::text || '%%' OR %(region)s::text LIKE '%%' || area || '%%'))"""

# 고객 조건에서 나온 "어기면 안 되는 조건". 이름을 붙여 둔 것은 결과가 없을 때 하나씩 빼 보며 어디서 막혔는지 찾기 위해서다(diagnose).
# %(이름)s 는 값을 안전하게 끼워 넣는 자리다. ::int 같은 표기는 값이 NULL 일 때도 형식을 알 수 있게 붙인다.
# 값이 NULL(고객이 말하지 않음)이면 그 조건은 모두 통과한다.
CONDITIONS: dict[str, str] = {
    "region": _REGION_OK,
    # 식이: 요청한 표시를 메뉴가 모두 갖고 있어야 한다
    "diet": "m.diet_tags @> %(diet)s::text[]",
    # 가격: 가격을 모르는 메뉴는 "예산 안" 이라고 할 수 없어 빠진다
    "price": "(%(price_cap)s::int IS NULL OR m.price <= %(price_cap)s::int)",
    # 종류: 정확히 찾을 때만 건다(넓혀 찾을 때는 NULL 을 넘긴다)
    "dessert": "(%(dessert)s::text IS NULL OR m.category = %(dessert)s::text OR m.name LIKE '%%' || %(dessert)s::text || '%%')",
    # 보관: 메뉴에 필요한 설비가 고객 쪽 설비 이하여야 한다. 보관 방법을 모르는 메뉴는 빠진다
    "storage": f"(%(storage_level)s::int IS NULL OR ({_STORAGE_CASE}) <= %(storage_level)s::int)",
    # 아래는 공급 조건(supplier_capacity)과 비교한다. 공급 조건이 등록되지 않은 메뉴는 확인할 수 없으므로 빠진다
    "quantity": "(%(quantity)s::int IS NULL OR (c.max_qty_per_day >= %(quantity)s::int AND COALESCE(c.moq, 0) <= %(quantity)s::int))",
    "due_date": "(%(days_left)s::int IS NULL OR c.lead_time_days <= %(days_left)s::int)",
    "individual_packaging": "(%(need_packaging)s::boolean IS NOT TRUE OR c.individual_packaging IS TRUE)",
    "gift_packaging": "(%(need_gift)s::boolean IS NOT TRUE OR c.gift_packaging IS TRUE)",
    "tax_invoice": "(%(need_tax_invoice)s::boolean IS NOT TRUE OR c.tax_invoice IS TRUE)",
}
# 결과가 없을 때 고객·직원에게 보여 줄 조건 이름
CONDITION_LABELS = {
    "region": "지역", "diet": "식이", "price": "가격·예산", "storage": "보관 방법",
    "quantity": "수량(최소 주문 수량·하루 생산량)", "due_date": "받을 날짜(준비 기간)",
    "individual_packaging": "개별 포장", "gift_packaging": "선물 포장", "tax_invoice": "세금계산서",
}

_FROM = """
FROM menus m
JOIN shops s ON s.id = m.shop_id
LEFT JOIN supplier_capacity c ON c.menu_id = m.id
WHERE s.is_open                                        -- 영업 중인 업체만
  AND m.embedding IS NOT NULL
  AND NOT (s.id = ANY(%(exclude_shops)s::bigint[]))    -- 이미 거절한 공급자는 다시 제안하지 않는다
"""

_COLUMNS = """
       m.id AS menu_id, m.name AS menu_name, m.description, m.category, m.price, m.diet_tags, m.storage,
       s.id AS shop_id, s.name AS shop_name, s.region, s.is_partner, s.rating, s.review_count, s.source_url,
       c.moq, c.max_qty_per_day, c.lead_time_days, c.individual_packaging, c.gift_packaging, c.tax_invoice,
       c.delivery_areas, c.last_verified_at, c.valid_until, c.confirmation_status,
       -- 지역이 맞은 방식: local(업체가 그 지역에 있음) / delivery(배송 가능 지역에 있음) / nationwide(전국 배송뿐)
       CASE WHEN s.region LIKE '%%' || %(region)s::text || '%%' THEN 'local'
            WHEN EXISTS (SELECT 1 FROM unnest(c.delivery_areas) AS area
                         WHERE area <> '전국' AND (area LIKE '%%' || %(region)s::text || '%%' OR %(region)s::text LIKE '%%' || area || '%%'))
                 THEN 'delivery'
            ELSE 'nationwide' END AS region_match,
       1 - (m.embedding <=> %(query_vector)s) AS similarity,   -- <=> 는 코사인 거리. 1 에서 빼면 유사도(클수록 비슷)
       -- 메뉴·업체 이름과 분류의 낱말(2글자 이상) 가운데 문의 글에 들어 있는 것의 개수
       (SELECT count(DISTINCT word)
        FROM unnest(regexp_split_to_array(m.name || ' ' || s.name || ' ' || COALESCE(m.category, ''), '\\s+')) AS word
        WHERE length(word) >= 2 AND position(word IN %(text)s::text) > 0) AS keyword_hits
"""


def _where(skip: str | None = None) -> str:
    """조건들을 AND 로 이은 SQL 조각을 만든다. skip 에 조건 이름을 주면 그 조건 하나만 뺀다."""
    return "".join(f"\n  AND {sql}" for name, sql in CONDITIONS.items() if name != skip)


# 의미 검색: 조건을 통과한 메뉴를 뜻이 가까운 순으로
VECTOR_SQL = f"SELECT {_COLUMNS} {_FROM} {_where()}\nORDER BY m.embedding <=> %(query_vector)s\nLIMIT %(limit)s"
# 키워드 검색: 조건을 통과한 메뉴 가운데 이름이 문의에 나온 것을, 맞은 낱말이 많은 순으로
KEYWORD_SQL = (
    f"SELECT * FROM (SELECT {_COLUMNS} {_FROM} {_where()}) AS pool\n"
    "WHERE keyword_hits > 0\nORDER BY keyword_hits DESC, similarity DESC, menu_id\nLIMIT %(limit)s"
)


def query_text(slots: Slots, original: str) -> str:
    """의미 검색에 쓸 글을 만든다. 뽑은 조건 가운데 '뜻' 에 해당하는 것(맛·종류·목적)만 모은다.

    지역·가격처럼 정확히 거르는 조건은 넣지 않는다. 모을 것이 없으면 문의 원문을 쓴다.
    """
    parts = [*slots.taste, slots.dessert or "", slots.purpose or slots.occasion or ""]
    text = " ".join(part for part in parts if part).strip()
    return text or original


def _params(slots: Slots, original: str, broaden: bool, today: date, exclude_shops: list[int]) -> dict[str, Any]:
    """SQL 에 끼워 넣을 값들을 만든다. 단체주문이 아니면 수량·날짜·개별 포장·세금계산서 조건은 NULL(거르지 않음)이다."""
    group = is_group_order(slots)
    # 받을 날까지 남은 날수. 업체의 준비 기간이 이보다 길면 만들 수 없다
    days_left = (date.fromisoformat(slots.due_date) - today).days if group and slots.due_date else None
    return {
        "text": original,
        "region": slots.region,
        "diet": slots.diet,
        "price_cap": price_cap(slots),
        "dessert": None if broaden else slots.dessert,
        "storage_level": storage_level(slots),
        "quantity": order_quantity(slots) if group else None,
        "days_left": days_left,
        "need_packaging": slots.individual_packaging if group else None,
        # 선물 포장은 한 개를 사도 필요한 조건이라 단체주문이 아니어도 건다
        "need_gift": slots.gift_packaging,
        "need_tax_invoice": slots.tax_invoice if group else None,
        "exclude_shops": exclude_shops,
        "limit": settings.RECO_CANDIDATES,
    }


def rrf_scores(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """여러 순위표를 RRF 로 합친 점수를 돌려준다.

    입력: rankings 는 순위표들의 목록. 순위표 하나는 메뉴 번호를 1위부터 늘어놓은 목록이다.
    반환: {메뉴 번호: 점수}. 점수 = 그 메뉴가 나온 순위표마다 1 ÷ (k + 순위)를 더한 값.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, menu_id in enumerate(ranking, start=1):
            scores[menu_id] = scores.get(menu_id, 0.0) + 1.0 / (k + position)
    return scores


def fuse(by_vector: list[dict[str, Any]], by_keyword: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """의미 검색 결과와 키워드 검색 결과를 RRF 로 합쳐 위에서부터 limit 개를 돌려준다.

    반환: 후보 사전에 "rrf"(합친 점수)·"vector_rank"·"keyword_rank"(그 순위표에 없으면 None)를 더한 목록.
    """
    vector_ids = [row["menu_id"] for row in by_vector]
    keyword_ids = [row["menu_id"] for row in by_keyword]
    scores = rrf_scores([vector_ids, keyword_ids])
    # 두 표에 모두 나온 메뉴는 내용이 같으므로 한 줄만 남긴다
    rows = {row["menu_id"]: row for row in [*by_keyword, *by_vector]}
    merged = sorted(rows.values(), key=lambda row: (scores[row["menu_id"]], row["similarity"]), reverse=True)[:limit]
    return [
        {**row, "rrf": round(scores[row["menu_id"]], 6),
         "vector_rank": vector_ids.index(row["menu_id"]) + 1 if row["menu_id"] in vector_ids else None,
         "keyword_rank": keyword_ids.index(row["menu_id"]) + 1 if row["menu_id"] in keyword_ids else None}
        for row in merged
    ]


def search(slots: Slots, original: str, broaden: bool = False, today: date | None = None,
           exclude_shops: list[int] | None = None) -> list[dict[str, Any]]:
    """조건에 맞는 메뉴 후보를 돌려준다.

    입력: slots 는 뽑은 조건(지역이 반드시 있어야 한다), original 은 문의 원문,
          broaden=True 면 디저트 종류 조건을 풀어 비슷한 종류까지 찾는다. 다른 조건은 풀지 않는다.
          exclude_shops 는 후보에서 뺄 업체 번호(대체 공급자를 찾을 때 이미 거절한 업체).
    반환: 메뉴·업체·공급 조건·유사도·키워드 일치 수·합친 순위 점수가 든 사전의 목록(최대 RECO_CANDIDATES 개). 없으면 빈 목록.
    """
    params = _params(slots, original, broaden, today or dates.today(), exclude_shops or [])
    params["query_vector"] = Vector(llm.embed([query_text(slots, original)])[0])
    with db.conn() as c:
        by_vector = c.execute(VECTOR_SQL, params).fetchall()
        by_keyword = c.execute(KEYWORD_SQL, params).fetchall()
    return fuse(by_vector, by_keyword, settings.RECO_CANDIDATES)


def diagnose(slots: Slots, original: str, today: date | None = None,
             exclude_shops: list[int] | None = None) -> list[dict[str, Any]]:
    """후보가 하나도 없을 때 "어느 조건 때문에 막혔는지" 를 찾는다.

    방법: 고객이 실제로 건 조건을 하나씩만 빼고 업체 수를 세어 본다. 종류 조건은 이미 풀어 본 뒤라 여기서도 풀어 둔다.
    반환: [{"condition": 조건 이름, "label": 보여 줄 말, "shops_if_relaxed": 그 조건만 뺐을 때 맞는 업체 수}] — 수가 큰 순.
          모두 0 이면 조건 여러 개가 함께 막고 있다는 뜻이다. 조건을 어긴 업체를 대신 추천하지는 않는다.
    """
    params = _params(slots, original, True, today or dates.today(), exclude_shops or [])
    # 고객이 말하지 않은 조건은 빼 봐야 달라지지 않으므로 세지 않는다
    active = [name for name, value in [
        ("region", True), ("diet", slots.diet), ("price", params["price_cap"] is not None), ("storage", params["storage_level"] is not None),
        ("quantity", params["quantity"]), ("due_date", params["days_left"] is not None),
        ("individual_packaging", params["need_packaging"]), ("gift_packaging", params["need_gift"]),
        ("tax_invoice", params["need_tax_invoice"]),
    ] if value]
    blockers = []
    with db.conn() as c:
        for name in active:
            count = c.execute(f"SELECT count(DISTINCT s.id) AS n {_FROM} {_where(skip=name)}", params).fetchone()["n"]
            blockers.append({"condition": name, "label": CONDITION_LABELS[name], "shops_if_relaxed": count})
    return sorted(blockers, key=lambda item: item["shops_if_relaxed"], reverse=True)

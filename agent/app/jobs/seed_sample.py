"""연습용 샘플 데이터를 넣거나 지운다. 네이버 API 키나 실제 카탈로그가 없어도 전체 흐름을 확인하기 위한 것이다.

실행: python -m app.jobs.seed_sample            (넣기 — 여러 번 실행해도 같은 결과)
      python -m app.jobs.seed_sample --clear    (샘플만 지우기)
넣는 것: 이름이 '(샘플)' 로 시작하는 키워드 4개와 91일치 지표·글, 업체 6곳과 메뉴·공급 조건, 업체 프로필 1건.
        공급 조건은 확인한 지 며칠 안 된 것과 오래된 것을 섞어, 추천 결과의 "공급자 재확인" 표시까지 볼 수 있게 했다.
⚠️ 숫자는 모두 지어낸 것이다. 실제 트렌드가 아니므로 운영을 시작하기 전에 --clear 로 지운다.
구분: 키워드는 is_sample=TRUE, 업체는 source='sample', 프로필은 이름이 '(샘플)' 로 시작한다.
"""
from __future__ import annotations

import sys
from datetime import timedelta

from app import dates, db
from app.settings import settings

DAYS = 30        # 아래 SERIES 에 적은 날수(점수 계산에 쓰는 기간)
LONG_DAYS = 91   # 실제로 넣는 날수. 긴 시간창(약 3개월) 판정까지 보이도록, SERIES 앞의 날들은 첫 값으로 채운다

# 키워드별 최근 30일 언급 수(오래된 날 → 최근 날). 확산 단계가 하나씩 나오도록 모양을 정했다.
#   성장기: 잠잠하다가 최근 10일 동안 점점 더 빨리 늘어난다
#   정점:   크게 늘었지만 늘어나는 폭이 줄고 있다
#   쇠퇴기: 조금씩 늘지만 증가가 눈에 띄게 둔해졌다
#   태동기: 변화가 없다
SERIES: dict[str, tuple[str, list[int]]] = {
    "(샘플) 피스타치오 크림빵": ("빵", [20] * 20 + [22, 25, 29, 34, 40, 48, 58, 70, 85, 104]),
    "(샘플) 과일 찹쌀떡": ("떡", [20] * 22 + [40, 80, 150, 240, 330, 400, 450, 480]),
    "(샘플) 바스크 치즈케이크": ("케이크", [50] * 19 + [50, 60, 72, 84, 95, 104, 111, 116, 119, 121, 122]),
    "(샘플) 버터 쿠키": ("쿠키", [30] * 30),
}

# (업체 이름, 지역, 입점사 여부, 평점, 후기 수, 공급 조건 또는 None, 메뉴 목록)
# 공급 조건: (최소 주문 수량, 하루 최대 생산량, 준비 기간(일), 개별 포장, 선물 포장, 세금계산서, 배송 가능 지역, 조건을 확인한 지 며칠 됐는지)
#           이 조건을 그 업체의 메뉴마다 똑같이 넣는다. 확인한 지 CAPACITY_VALID_DAYS(30일)를 넘긴 조건은 "공급자 재확인" 으로 나온다.
# 메뉴: (이름, 분류, 가격(원), 식이 표시, 보관 방법, 설명)
SHOPS = [
    ("(샘플) 달콤공방", "서울 마포구", True, 4.7, 320, (30, 600, 3, True, True, True, [], 5), [
        ("비건 당근 케이크", "케이크", 38000, ["비건"], "냉장", "우유·달걀 없이 구운 촉촉하고 달지 않은 당근 케이크"),
        ("피스타치오 크림빵", "빵", 4800, [], "냉장", "피스타치오 크림을 가득 채운 부드러운 빵"),
    ]),
    ("(샘플) 초록오븐", "서울 강남구", True, 4.5, 140, (50, 300, 5, True, True, True, [], 5), [
        ("비건 초코 케이크", "케이크", 42000, ["비건", "글루텐프리"], "냉장", "쌀가루로 만든 진한 초콜릿 케이크"),
        ("저당 레몬 케이크", "케이크", 36000, ["저당"], "냉장", "설탕을 줄여 상큼하고 가벼운 레몬 케이크"),
    ]),
    ("(샘플) 강남제과", "서울 강남구", False, 4.8, 950, None, [
        ("생크림 딸기 케이크", "케이크", 39000, [], "냉장", "제철 딸기를 올린 부드러운 생크림 케이크"),
        ("버터 쿠키 세트", "쿠키", 18000, [], "상온", "버터 향이 진한 쿠키 12개 세트"),
    ]),
    ("(샘플) 해운대빵집", "부산 해운대구", False, 4.6, 410, (20, 400, 2, True, False, False, [], 60), [
        ("소금빵", "빵", 3200, [], "상온", "겉은 바삭하고 속은 쫄깃한 소금빵"),
        ("비건 바나나 파운드", "케이크", 29000, ["비건", "견과류없음"], "상온", "바나나로 단맛을 낸 달지 않은 파운드 케이크"),
    ]),
    ("(샘플) 떡하니", "서울 강남구", True, 4.4, 60, (100, 2000, 4, True, True, True, ["전국"], 3), [
        ("과일 찹쌀떡", "떡", 3500, ["비건"], "냉장", "딸기·샤인머스캣을 넣은 쫀득한 찹쌀떡. 개별 포장"),
    ]),
    ("(샘플) 문닫은집", "서울 강남구", False, 4.9, 30, None, [
        ("비건 케이크", "케이크", 30000, ["비건"], "냉장", "영업을 끝낸 업체 — 추천에 나오면 안 된다"),
    ]),
]
CLOSED_SHOP = "(샘플) 문닫은집"


def clear() -> None:
    """샘플 행만 지운다. 딸린 지표·글·점수·메뉴는 DB 의 연쇄 삭제(ON DELETE CASCADE)로 함께 지워진다."""
    with db.conn() as c:
        c.execute("DELETE FROM keywords WHERE is_sample")
        c.execute("DELETE FROM shops WHERE source = 'sample'")
        c.execute("DELETE FROM vendor_profiles WHERE vendor_key LIKE '(샘플)%%'")


def seed() -> None:
    """샘플을 넣는다. 먼저 기존 샘플을 지워 여러 번 실행해도 같은 상태가 되게 한다."""
    clear()
    # 마지막 날을 어제로 둔다(실제 수집도 어제까지의 값을 모은다)
    last_day = dates.today() - timedelta(days=1)
    with db.conn() as c:
        for term, (category, mentions) in SERIES.items():
            keyword_id = c.execute(
                "INSERT INTO keywords (term, category, is_sample) VALUES (%s, %s, TRUE) RETURNING id", (term, category)
            ).fetchone()["id"]
            peak = max(mentions)
            # 30일보다 앞선 날들은 첫 값으로 채워 "그 전에는 변화가 없었다" 는 모양을 만든다
            padded = [mentions[0]] * (LONG_DAYS - DAYS) + mentions
            for offset, count in enumerate(padded):
                day = last_day - timedelta(days=LONG_DAYS - 1 - offset)
                # 검색 관심도는 언급 수를 0~100 으로 바꾼 값으로 흉내 낸다
                c.execute(
                    "INSERT INTO daily_metrics (keyword_id, metric_date, source, mention_count, search_ratio, is_complete) "
                    "VALUES (%s, %s, 'sample', %s, %s, TRUE)",
                    (keyword_id, day, count, round(count / peak * 100, 1)),
                )
            for number, (title, snippet) in enumerate(
                [("먹어 본 후기", "기대보다 맛있어서 또 사 먹을 생각이다"),
                 ("요즘 자주 보이는 이유", "여러 가게에서 새로 내놓고 있다는 소식"),
                 ("만드는 법 정리", "집에서 따라 해 본 과정과 재료")], start=1):
                c.execute(
                    "INSERT INTO documents (keyword_id, source, title, url, snippet, posted_at) VALUES (%s, 'sample', %s, %s, %s, %s)",
                    (keyword_id, f"{term} {title}", f"https://example.com/sample/{keyword_id}/{number}", snippet,
                     last_day - timedelta(days=number)),
                )
        for name, region, is_partner, rating, reviews, capacity, menus in SHOPS:
            shop_id = c.execute(
                "INSERT INTO shops (name, region, is_partner, is_open, rating, review_count, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'sample') RETURNING id",
                (name, region, is_partner, name != CLOSED_SHOP, rating, reviews),
            ).fetchone()["id"]
            for menu_name, category, price, diet_tags, storage, description in menus:
                menu_id = c.execute(
                    "INSERT INTO menus (shop_id, name, category, price, diet_tags, storage, description) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (shop_id, menu_name, category, price, diet_tags, storage, description),
                ).fetchone()["id"]
                if capacity is None:
                    continue
                # 확인한 날짜와 유효기간을 오늘 기준으로 계산해 넣는다(오래 전에 확인한 조건은 유효기간이 이미 지난 날짜가 된다)
                *conditions, delivery_areas, verified_days_ago = capacity
                verified_on = dates.today() - timedelta(days=verified_days_ago)
                c.execute(
                    "INSERT INTO supplier_capacity (menu_id, shop_id, moq, max_qty_per_day, lead_time_days, individual_packaging, "
                    "gift_packaging, tax_invoice, delivery_areas, last_verified_at, valid_until, confirmation_status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed')",
                    (menu_id, shop_id, *conditions, delivery_areas, verified_on,
                     verified_on + timedelta(days=settings.CAPACITY_VALID_DAYS)),
                )
        c.execute(
            "INSERT INTO vendor_profiles (vendor_key, categories, region, note) VALUES (%s, %s, %s, %s)",
            ("(샘플) 달콤공방", ["빵", "케이크"], "서울 마포구", "오븐 2대, 하루 600개까지 생산"),
        )


def main() -> int:
    """인자에 --clear 가 있으면 지우고, 없으면 넣는다. 반환값은 종료 코드(항상 0)."""
    db.wait_for_db()
    db.apply_schema()
    if "--clear" in sys.argv:
        clear()
        print("샘플 데이터를 지웠습니다.")
    else:
        seed()
        print(f"샘플 데이터를 넣었습니다: 키워드 {len(SERIES)}개, 업체 {len(SHOPS)}곳. 다음: run_trend, embed_catalog")
    return 0


if __name__ == "__main__":
    sys.exit(main())

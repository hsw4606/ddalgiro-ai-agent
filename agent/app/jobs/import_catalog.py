"""CSV 파일의 업체·메뉴·공급 조건을 DB 에 넣는다 (안건 2 · 기획 03 §4 카탈로그).

실행: python -m app.jobs.import_catalog 파일.csv
      넣은 뒤 python -m app.jobs.embed_catalog 로 벡터를 채워야 검색에 나온다.
CSV:  첫 줄은 칸 이름, UTF-8, 한 줄이 메뉴 하나다. 같은 업체의 메뉴가 여러 줄이면 업체 칸을 줄마다 똑같이 적는다.
  필수 칸: shop_name, region, menu_name
  선택 칸: address, is_partner, is_open, rating, review_count, source_url,
           description, category, price, diet_tags(여러 개면 ; 로 구분), storage(상온 / 냉장 / 냉동),
           moq, max_qty_per_day, lead_time_days, individual_packaging, gift_packaging, tax_invoice,
           delivery_areas(여러 개면 ; 로 구분, 어디든 보내면 전국),
           capacity_verified_on(공급자가 조건을 확인해 준 날짜 YYYY-MM-DD), capacity_valid_until(그 값을 믿을 마지막 날짜)
  참·거짓 칸은 Y / N 으로 적는다. 비워 두면 "모름" 이다(is_partner 는 N, is_open 은 Y 로 본다).
같은 업체(이름+지역)·같은 메뉴(업체+이름)가 이미 있으면 새 값으로 덮어쓴다. 내용이 바뀐 메뉴는 벡터를 비워 다시 만들게 한다.
공급 조건은 메뉴(상품)마다 따로 저장한다. 같은 업체라도 메뉴마다 생산량·준비 기간이 다를 수 있기 때문이다.
확인 날짜: capacity_verified_on 을 적으면 '확인됨', 비우면 '확인 전' 이 된다. 확인 전인 조건은 추천에서 "공급자 재확인" 으로 표시된다.
          capacity_valid_until 을 비우면 확인한 날 + 설정 CAPACITY_VALID_DAYS 일로 정한다.
"""
from __future__ import annotations

import csv
import sys
from datetime import date, timedelta

from app import db
from app.reco.slots import STORAGE_LEVELS
from app.settings import settings

REQUIRED = ("shop_name", "region", "menu_name")


def _flag(value: str | None, default: bool | None = None) -> bool | None:
    """Y/N 글자를 참·거짓으로 바꾼다. 비어 있으면 default."""
    text = (value or "").strip().upper()
    if not text:
        return default
    return text in ("Y", "YES", "TRUE", "1")


def _number(value: str | None, kind: type = int):
    """숫자 글자를 숫자로 바꾼다. 비어 있으면 None. 숫자가 아니면 ValueError(몇 번째 줄인지는 부르는 쪽이 알린다)."""
    text = (value or "").strip().replace(",", "")
    return kind(text) if text else None


def _date(value: str | None) -> date | None:
    """YYYY-MM-DD 글자를 날짜로 바꾼다. 비어 있으면 None. 형식이 틀리면 ValueError."""
    text = (value or "").strip()
    return date.fromisoformat(text) if text else None


def _list(value: str | None) -> list[str]:
    """';' 로 나눈 글자를 목록으로 바꾼다. 빈 항목은 버린다."""
    return [part.strip() for part in (value or "").split(";") if part.strip()]


def _storage(value: str | None) -> str | None:
    """보관 방법 글자를 확인한다. 비어 있으면 None. 상온·냉장·냉동이 아니면 검색 조건과 비교할 수 없어 ValueError."""
    text = (value or "").strip()
    if text and text not in STORAGE_LEVELS:
        raise ValueError(f"storage 는 {list(STORAGE_LEVELS)} 중 하나여야 합니다: {text}")
    return text or None


def import_file(path: str) -> int:
    """CSV 한 파일을 넣고 넣은 줄 수를 돌려준다. 한 줄이라도 형식이 틀리면 전체를 넣지 않는다(트랜잭션 하나)."""
    with open(path, newline="", encoding="utf-8-sig") as file:  # utf-8-sig: 엑셀이 붙이는 파일 머리 표시를 무시한다
        rows = list(csv.DictReader(file))
    count = 0
    with db.conn() as c:
        for line, row in enumerate(rows, start=2):  # 1번 줄은 칸 이름이라 자료는 2번 줄부터다
            lacking = [name for name in REQUIRED if not (row.get(name) or "").strip()]
            if lacking:
                raise ValueError(f"{line}번 줄: 필수 칸 {lacking} 이 비어 있습니다.")
            try:
                shop_id = c.execute(
                    """
                    INSERT INTO shops (name, region, address, is_partner, is_open, rating, review_count, source, source_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'csv', %s)
                    ON CONFLICT (name, region) DO UPDATE SET
                        address = EXCLUDED.address, is_partner = EXCLUDED.is_partner, is_open = EXCLUDED.is_open,
                        rating = EXCLUDED.rating, review_count = EXCLUDED.review_count,
                        source_url = EXCLUDED.source_url, updated_at = now()
                    RETURNING id
                    """,
                    (row["shop_name"].strip(), row["region"].strip(), row.get("address") or None,
                     _flag(row.get("is_partner"), False), _flag(row.get("is_open"), True),
                     _number(row.get("rating"), float), _number(row.get("review_count")) or 0, row.get("source_url") or None),
                ).fetchone()["id"]
                # embedding = NULL: 내용이 바뀌었을 수 있으므로 벡터를 다시 만들게 한다
                menu_id = c.execute(
                    """
                    INSERT INTO menus (shop_id, name, description, category, price, diet_tags, storage)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (shop_id, name) DO UPDATE SET
                        description = EXCLUDED.description, category = EXCLUDED.category, price = EXCLUDED.price,
                        diet_tags = EXCLUDED.diet_tags, storage = EXCLUDED.storage, embedding = NULL
                    RETURNING id
                    """,
                    (shop_id, row["menu_name"].strip(), row.get("description") or None, row.get("category") or None,
                     _number(row.get("price")), _list(row.get("diet_tags")), _storage(row.get("storage"))),
                ).fetchone()["id"]
                # 확인 날짜가 있어야 '확인됨' 이다. 유효기간을 비웠으면 확인한 날부터 정해진 날수만큼 믿는다
                verified_on = _date(row.get("capacity_verified_on"))
                valid_until = _date(row.get("capacity_valid_until"))
                if verified_on and not valid_until:
                    valid_until = verified_on + timedelta(days=settings.CAPACITY_VALID_DAYS)
                # 빈 칸도 '모름(NULL)' 으로 덮어써야, 철회된 공급 조건이 추천에 계속 쓰이지 않는다.
                # 확인 표시도 같다 — 날짜 없이 다시 넣은 조건은 누가 확인한 값인지 알 수 없어 '확인 전' 으로 돌아간다
                c.execute(
                    """
                    INSERT INTO supplier_capacity
                        (menu_id, shop_id, moq, max_qty_per_day, lead_time_days, individual_packaging, gift_packaging,
                         tax_invoice, delivery_areas, last_verified_at, valid_until, confirmation_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (menu_id) DO UPDATE SET
                        shop_id = EXCLUDED.shop_id, moq = EXCLUDED.moq, max_qty_per_day = EXCLUDED.max_qty_per_day,
                        lead_time_days = EXCLUDED.lead_time_days, individual_packaging = EXCLUDED.individual_packaging,
                        gift_packaging = EXCLUDED.gift_packaging, tax_invoice = EXCLUDED.tax_invoice,
                        delivery_areas = EXCLUDED.delivery_areas, last_verified_at = EXCLUDED.last_verified_at,
                        valid_until = EXCLUDED.valid_until, confirmation_status = EXCLUDED.confirmation_status,
                        updated_at = now()
                    """,
                    (menu_id, shop_id, _number(row.get("moq")), _number(row.get("max_qty_per_day")),
                     _number(row.get("lead_time_days")), _flag(row.get("individual_packaging")), _flag(row.get("gift_packaging")),
                     _flag(row.get("tax_invoice")), _list(row.get("delivery_areas")), verified_on, valid_until,
                     "confirmed" if verified_on else "unverified"),
                )
            except ValueError as error:
                raise ValueError(f"{line}번 줄: 숫자·날짜·보관 칸의 값이 형식에 맞지 않습니다 ({error}).") from error
            count += 1
    return count


def main() -> int:
    """명령 뒤에 준 CSV 파일을 넣는다. 반환값은 종료 코드: 사용법이 틀리면 2."""
    if len(sys.argv) != 2:
        print("사용법: python -m app.jobs.import_catalog 파일.csv")
        return 2
    db.wait_for_db()
    db.apply_schema()
    print(f"{import_file(sys.argv[1])}줄을 넣었습니다. 다음: python -m app.jobs.embed_catalog")
    return 0


if __name__ == "__main__":
    sys.exit(main())

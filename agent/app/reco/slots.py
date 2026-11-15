"""자연어 문의를 "조건 카드"(슬롯)로 바꾼다 (안건 2 · 기획 03 §5.1).

쓰는 곳: reco/graph.py 의 understand 단계. 검색·순위·견적은 이 파일의 order_quantity·price_cap 을 함께 쓴다.
나누기: 문장에서 조건을 뽑는 일은 LLM 이 하고, 뽑은 값이 믿을 만한지와 "꼭 필요한 조건이 빠졌는지" 는 코드가 규칙으로 판단한다.
        LLM 은 말하지 않은 조건을 그럴듯하게 채워 넣는 일이 있어(예: 날짜를 오늘로, 용도를 단체주문으로),
        문의 글에 실제로 근거가 있는 값만 남긴다.
단체주문 조건: 상담에서 실제로 확인하는 항목(목적·인원·1인 예산·총예산·수령일·배송지·포장·보관·세금계산서)을 그대로 칸으로 두었다.
숫자·날짜 계산: "인원 200명 × 1인 8천 원" 같은 곱셈과 "다음 주 금요일" 같은 날짜 계산은 LLM 에게 맡기지 않고 이 파일의 함수가 한다.
              LLM 은 연도를 바꿔 적거나 요일을 틀리게 세는 일이 있어, 문의에서 날짜 표현을 읽을 수 있으면 코드가 계산한 날짜를 쓴다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app import dates, llm
from app.settings import settings

# DB 의 menus.diet_tags 에 쓰는 표준 표기. 이 밖의 값은 조건에서 버린다.
DIET_TAGS = ["비건", "글루텐프리", "저당", "견과류없음"]
# 보관 방법과 "필요한 설비 수준". 숫자가 클수록 설비가 더 필요하다(냉장고가 있으면 상온 제품도 받을 수 있다).
# DB 의 menus.storage 와 같은 표기를 쓴다. reco/search.py 의 SQL 도 같은 순서를 쓴다.
STORAGE_LEVELS = {"상온": 0, "냉장": 1, "냉동": 2}
# 이 수량 이상이면 용도를 말하지 않아도 단체주문으로 본다
GROUP_ORDER_MIN_QUANTITY = 20
# 문의에 이런 말이 있어야 "단체주문" 이라는 판단을 받아들인다(수량이 많은 경우는 별도로 인정)
GROUP_WORDS = ("단체", "회사", "기업", "임직원", "직원", "행사", "답례", "납품", "대량", "워크숍", "세미나", "간식",
               "개별 포장", "개별포장", "세금계산서")
# 문의에 날짜를 말한 흔적. 이런 표현이 없는데 날짜가 뽑혔으면 LLM 이 지어낸 것으로 보고 버린다
DATE_HINT = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\s*월|\d{1,2}\s*일|\d{1,2}/\d{1,2}|오늘|당일|내일|모레|글피|이번\s*주|다음\s*주|주말|요일")
# 문의에 돈을 말한 흔적. 없으면 가격·예산 칸을 버린다.
# '추천'·'만들어' 같은 낱말에 걸리지 않게, 숫자 뒤의 단위('4만'·'8천')나 '만 원' 같은 모양만 본다
MONEY_HINT = re.compile(r"\d\s*(?:원|만|천|백)|[만천백]\s*원|예산|가격|₩")
# 문의에 보관을 말한 흔적. 없으면 보관 칸을 버린다
STORAGE_HINT = re.compile(r"상온|실온|냉장|냉동|보관|상관\s*없|무관")
# 금액이 "한 사람 몫" 이라는 흔적과 "한 개 값" 이라는 흔적. 1인 예산을 1개 가격 칸에 잘못 넣은 것을 바로잡을 때 본다
PER_PERSON_HINT = re.compile(r"1\s*인|인당|명당|한\s*사람")
PER_ITEM_HINT = re.compile(r"개당|1\s*개|한\s*개|하나에")

# ── 날짜 표현 읽기에 쓰는 식 ──
_FULL_DATE = re.compile(r"(\d{4})\s*[-년./]\s*(\d{1,2})\s*[-월./]\s*(\d{1,2})")   # 2026-12-10, 2026년 12월 10일
_MONTH_DAY = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")   # 12월 10일
# '당일'(생일 당일)·'12/10'(1/2 조각)처럼 날짜가 아닐 수 있는 표현은 코드가 읽지 않는다
_RELATIVE_DAYS = {"오늘": 0, "내일": 1, "모레": 2, "글피": 3}
_WEEKDAY = re.compile(r"(이번\s*주|다다음\s*주|다음\s*주)?\s*([월화수목금토일])요일")
_WEEKDAYS = "월화수목금토일"   # date.weekday() 의 0~6 과 같은 순서
_WEEK_OFFSETS = {"이번주": 0, "다음주": 1, "다다음주": 2}
# "행사 전날까지"·"3일 전" 처럼 말한 날짜에서 다시 계산해야 하는 표현. 이런 문의는 코드가 읽지 않고 LLM 의 답을 쓴다
_DATE_SHIFT = re.compile(r"전날|하루\s*전|이틀\s*전|\d+\s*일\s*전|\d+\s*일\s*(?:뒤|후)|일주일|보름")
# 숫자로 적은 금액: 8,000원 / 8천 원 / 160만 원 / 3만5천원 / 1.5만 원
_MONEY = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(만|천|백)?(?:\s*(\d+)\s*(천|백))?\s*(원)?")
_UNITS = {"만": 10000, "천": 1000, "백": 100}


class Slots(BaseModel):
    """문의 한 건에서 뽑은 조건. 말하지 않은 조건은 None 이나 빈 목록으로 둔다."""

    dessert: str | None = Field(None, description="디저트 종류 한 낱말. 예: 케이크, 쿠키, 마카롱, 빵. 말하지 않았으면 null")
    taste: list[str] = Field(default_factory=list, description="문의에 나온 맛·식감·분위기 표현. 없으면 빈 목록")
    diet: list[str] = Field(default_factory=list, description=f"식이 제약. 다음 중에서만 고른다: {DIET_TAGS}. 없으면 빈 목록")
    max_price: int | None = Field(None, description="1개 가격 상한(원). '4만 원 이하' → 40000. 말하지 않았으면 null")
    region: str | None = Field(None, description="받을 지역. 예: 강남, 서울 마포구, 부산. 말하지 않았으면 null")
    occasion: Literal["개인", "선물", "행사", "단체주문"] | None = Field(None, description="용도. 말하지 않았으면 null")
    purpose: str | None = Field(None, description="주문 목적을 문의에 나온 말 그대로 짧게. 예: 사내 행사, 워크숍 간식, 답례품. 말하지 않았으면 null")
    quantity: int | None = Field(None, description="문의에 숫자로 나온 개수. 말하지 않았으면 null")
    headcount: int | None = Field(None, description="문의에 숫자로 나온 인원 수. '임직원 200명' → 200. 말하지 않았으면 null")
    budget_per_person: int | None = Field(None, description="1인 예산(원). '1인 8천 원' → 8000. 말하지 않았으면 null")
    total_budget: int | None = Field(None, description="총예산(원). '총 160만 원' → 1600000. 말하지 않았으면 null")
    due_date: str | None = Field(None, description="받아야 하는 날짜 YYYY-MM-DD. 말하지 않았으면 null")
    storage: Literal["상온", "냉장", "냉동", "무관"] | None = Field(
        None, description="받는 곳에서 가능한 보관 방법. 상온만 되면 '상온', 냉장고가 있으면 '냉장', 냉동고가 있으면 '냉동', 상관없다고 했으면 '무관'. 말하지 않았으면 null")
    individual_packaging: bool | None = Field(None, description="개별 포장이 필요하다고 했으면 true, 아니면 null")
    gift_packaging: bool | None = Field(None, description="선물 포장이 필요하다고 했으면 true, 아니면 null")
    tax_invoice: bool | None = Field(None, description="세금계산서가 필요하다고 했으면 true, 아니면 null")


SYSTEM = """너는 디저트 주문 문의에서 조건을 뽑는다.
가장 중요한 규칙: 문의에 실제로 나온 내용만 채운다. 말하지 않은 칸은 반드시 null(목록은 빈 목록)로 둔다. 짐작해서 채우지 마라.
세부 규칙:
- '비건' 은 diet 에 넣는다. '저당·무설탕' 처럼 당을 제한해야 한다고 분명히 말한 경우만 diet 에 '저당' 을 넣는다.
- '안 단·덜 단' 처럼 맛의 취향은 diet 가 아니라 taste 에 넣는다.
- 회사·행사용 대량 주문이라고 말한 경우만 occasion 을 '단체주문' 으로 한다. 생일·기념일은 단체주문이 아니다.
- 날짜를 말한 경우만 오늘 날짜를 기준으로 계산해 YYYY-MM-DD 로 적는다.
- '200명' 처럼 사람 수는 headcount 에, '200개' 처럼 개수는 quantity 에 넣는다. 서로 옮겨 적지 마라.
- '1인 8천 원' 은 budget_per_person, '총 160만 원'·'예산 160만 원' 은 total_budget, '개당 4만 원 이하' 는 max_price 다. 곱하거나 나눠서 다른 칸을 채우지 마라.
- 보관은 받는 쪽 사정이다. '상온 보관만 가능' → 상온, '냉장 보관 가능' → 냉장, '상관없음' → 무관.

예시 1) 문의: "부드러운 치즈케이크 3만 원 이하로 찾아요"
→ {"dessert":"케이크","taste":["부드러운"],"diet":[],"max_price":30000,"region":null,"occasion":null,"purpose":null,"quantity":null,"headcount":null,"budget_per_person":null,"total_budget":null,"due_date":null,"storage":null,"individual_packaging":null,"gift_packaging":null,"tax_invoice":null}
예시 2) 문의: "회사 워크숍 간식으로 쿠키 150개, 부산으로 12월 3일까지, 세금계산서 필요"  (오늘이 2026-11-14 일 때)
→ {"dessert":"쿠키","taste":[],"diet":[],"max_price":null,"region":"부산","occasion":"단체주문","purpose":"회사 워크숍 간식","quantity":150,"headcount":null,"budget_per_person":null,"total_budget":null,"due_date":"2026-12-03","storage":null,"individual_packaging":null,"gift_packaging":null,"tax_invoice":true}
예시 3) 문의: "다음 주 금요일 임직원 200명 간식, 1인 8천 원, 개별 포장, 세금계산서, 판교 수령, 냉장 보관 가능"  (오늘이 2026-11-14 토요일일 때)
→ {"dessert":null,"taste":[],"diet":[],"max_price":null,"region":"판교","occasion":"단체주문","purpose":"임직원 간식","quantity":null,"headcount":200,"budget_per_person":8000,"total_budget":null,"due_date":"2026-11-20","storage":"냉장","individual_packaging":true,"gift_packaging":null,"tax_invoice":true}"""


def extract(text: str, today: date | None = None) -> Slots:
    """문의 글에서 조건을 뽑는다.

    입력: text 는 고객이 쓴 글(되물은 뒤의 추가 답이 이어 붙어 있을 수 있다), today 는 날짜 계산의 기준일.
    반환: 문의 글과 대조해 정리한 Slots.
    실패: LLM 이 양식에 맞는 답을 내지 못하면 llm.ask_structured 의 예외가 그대로 올라간다.
    """
    today = today or dates.today()
    slots = llm.ask_structured(Slots, f"{SYSTEM}\n오늘 날짜: {today.isoformat()}", f"문의: {text}")
    return normalize(slots, text, today)


def resolve_date(text: str, today: date) -> date | None:
    """문의 글의 날짜 표현을 코드로 계산한다. 읽을 수 없거나 날짜가 여러 개면 None(그때는 LLM 의 답을 쓴다).

    읽는 표현: 2026-12-10·2026년 12월 10일 / 12월 10일(올해, 이미 지났으면 내년) / 오늘·내일·모레·글피 /
              금요일(오늘부터 가장 가까운 그 요일)·이번 주 금요일·다음 주 금요일·다다음 주 금요일.
    한 주는 월요일에 시작한다고 본다. "다음 주 금요일" 은 다음 월요일로 시작하는 주의 금요일이다.
    "전날까지"·"3일 뒤" 처럼 말한 날짜에서 한 번 더 계산해야 하는 문의는 읽지 않는다.
    """
    if _DATE_SHIFT.search(text):
        return None
    found: set[date] = set()
    rest = text
    for year, month, day in _FULL_DATE.findall(text):
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:   # 13월·32일처럼 없는 날짜
            return None
    # 연도까지 적은 날짜는 지워, 그 안의 "12월 10일" 을 한 번 더 세지 않게 한다
    rest = _FULL_DATE.sub(" ", rest)
    for month, day in _MONTH_DAY.findall(rest):
        try:
            candidate = date(today.year, int(month), int(day))
        except ValueError:
            return None
        # 연도를 말하지 않은 날짜가 이미 지났으면 내년의 그 날로 본다
        found.add(candidate if candidate >= today else candidate.replace(year=today.year + 1))
    for word, offset in _RELATIVE_DAYS.items():
        if word in rest:
            found.add(today + timedelta(days=offset))
    for week_word, weekday in _WEEKDAY.findall(rest):
        target = _WEEKDAYS.index(weekday)
        if week_word:
            # 이번 주 월요일에서 주 수만큼 건너뛴 뒤 그 요일로 간다
            monday = today - timedelta(days=today.weekday())
            found.add(monday + timedelta(weeks=_WEEK_OFFSETS[week_word.replace(" ", "")], days=target))
        else:
            # 주를 말하지 않았으면 오늘부터 가장 가까운 그 요일(오늘이 그 요일이면 오늘)
            found.add(today + timedelta(days=(target - today.weekday()) % 7))
    return found.pop() if len(found) == 1 else None


def money_amounts(text: str) -> set[int]:
    """문의 글에 숫자로 적힌 금액을 모두 원 단위로 바꿔 돌려준다. 예: '1인 8천 원, 총 160만 원' → {8000, 1600000}.

    '원' 이나 단위(만·천·백)가 붙은 숫자만 금액으로 본다. '200명'·'150개' 는 금액이 아니다. 한글로 쓴 금액('팔천 원')은 읽지 않는다.
    """
    amounts: set[int] = set()
    for number, unit, tail_number, tail_unit, won in _MONEY.findall(text):
        if not unit and not won:
            continue
        value = float(number.replace(",", "")) * _UNITS.get(unit, 1)
        if tail_number:
            value += int(tail_number) * _UNITS[tail_unit]
        amounts.add(int(value))
    return amounts


def _said_number(value: int | None, text: str) -> int | None:
    """숫자가 문의 글에 실제로 적혀 있을 때만 돌려준다. 0 이하거나 글에 없으면 None(LLM 이 지어낸 값으로 본다)."""
    if value is None or value <= 0 or str(value) not in text.replace(",", ""):
        return None
    return value


def _said_money(value: int | None, text: str) -> int | None:
    """금액이 0 보다 크고 문의 글에 돈 이야기가 있을 때만 돌려준다.

    '8천 원' 을 8000 으로 바꾼 값은 글자 그대로 찾을 수 없어, 돈을 말한 흔적(MONEY_HINT)이 있는지만 본다.
    """
    if value is None or value <= 0 or not MONEY_HINT.search(text):
        return None
    return value


def normalize(slots: Slots, text: str, today: date) -> Slots:
    """LLM 이 뽑은 값을 문의 글과 대조해 정리한다. 양식은 맞아도 값이 지어낸 것일 수 있기 때문이다.

    원칙: 의심스러운 값은 고치려 하지 않고 비운다. 비워진 필수 조건은 고객에게 되묻게 된다(missing).
    """
    # 빈 글자("")는 "말하지 않음" 으로 바꾼다
    slots.dessert = (slots.dessert or "").strip() or None
    slots.region = (slots.region or "").strip() or None
    slots.taste = [word.strip() for word in slots.taste if word.strip()]
    # 표준 표기에 없는 식이 조건은 버린다(DB 와 정확히 비교하는 조건이라 표기가 다르면 아무것도 못 찾는다)
    slots.diet = [tag for tag in slots.diet if tag in DIET_TAGS]
    # 목적: 뽑은 말의 낱말(2글자 이상)이 하나라도 문의에 있어야 인정한다. 없으면 LLM 이 지어낸 목적이다
    purpose = (slots.purpose or "").strip()
    slots.purpose = purpose if any(len(word) >= 2 and word in text for word in purpose.split()) else None
    # 가격·예산: 0 이하이거나 문의에 돈 이야기가 없으면 비운다
    slots.max_price = _said_money(slots.max_price, text)
    slots.budget_per_person = _said_money(slots.budget_per_person, text)
    slots.total_budget = _said_money(slots.total_budget, text)
    # 총예산: LLM 이 "1인 예산 × 인원" 을 스스로 곱해 채우는 일이 있다. 문의에 금액이 숫자로 적혀 있는데
    # 그 가운데 이 값이 없으면 계산해 넣은 값으로 보고 비운다(곱셈은 total_budget 함수가 한다)
    said = money_amounts(text)
    if slots.total_budget and said and slots.total_budget not in said:
        slots.total_budget = None
    # 1인 예산을 1개 가격 칸에 넣은 경우: 문의에 "1인·인당" 이 있고 "개당" 이 없으면 1인 예산으로 옮긴다
    if slots.max_price and not slots.budget_per_person and PER_PERSON_HINT.search(text) and not PER_ITEM_HINT.search(text):
        slots.budget_per_person, slots.max_price = slots.max_price, None
    # 수량·인원: 그 숫자가 문의 글에 실제로 적혀 있을 때만 인정한다
    slots.quantity = _said_number(slots.quantity, text)
    slots.headcount = _said_number(slots.headcount, text)
    # 날짜: 문의의 날짜 표현을 코드가 읽을 수 있으면 그 계산을 쓴다(LLM 이 말하지 않았다고 비워 둔 경우도 채운다)
    resolved = resolve_date(text, today)
    if resolved:
        slots.due_date = resolved.isoformat()
    # 그다음: 문의에 날짜 표현이 있고, 형식이 맞고, 지나지 않은 날일 때만 인정한다
    if slots.due_date:
        try:
            valid = bool(DATE_HINT.search(text)) and date.fromisoformat(slots.due_date) >= today
        except ValueError:
            valid = False
        if not valid:
            slots.due_date = None
    # 보관·포장·세금계산서: 문의에 그 말이 있을 때만 인정한다. 지어낸 조건은 멀쩡한 업체를 후보에서 빼 버린다
    if slots.storage and not STORAGE_HINT.search(text):
        slots.storage = None
    if slots.individual_packaging and "개별" not in text:
        slots.individual_packaging = None
    if slots.gift_packaging and not ("선물" in text and "포장" in text):
        slots.gift_packaging = None
    if slots.tax_invoice and "계산서" not in text:
        slots.tax_invoice = None
    # 용도: 수량(또는 인원)이 많으면 단체주문으로 보고, 수량도 단체주문을 뜻하는 말도 없으면 단체주문 판단을 버린다
    many = (order_quantity(slots) or 0) >= GROUP_ORDER_MIN_QUANTITY
    if many:
        slots.occasion = "단체주문"
    elif slots.occasion == "단체주문" and not any(word in text for word in GROUP_WORDS):
        slots.occasion = None
    return slots


def order_quantity(slots: Slots) -> int | None:
    """주문 수량(개). 개수를 말했으면 그 값, 인원만 말했으면 한 사람에 하나로 보고 인원 수, 둘 다 없으면 None."""
    return slots.quantity or slots.headcount


def total_budget(slots: Slots) -> int | None:
    """총예산(원). 고객이 말한 총예산이 있으면 그 값, 없으면 1인 예산 × 인원(둘 다 있을 때). 알 수 없으면 None.

    곱셈은 여기서 코드가 한다. LLM 이 계산한 값은 쓰지 않는다.
    """
    if slots.total_budget:
        return slots.total_budget
    if slots.budget_per_person and slots.headcount:
        return slots.budget_per_person * slots.headcount
    return None


def price_cap(slots: Slots) -> int | None:
    """메뉴 1개 가격의 상한(원). 고객이 말한 가격·예산 조건 가운데 가장 엄격한 값. 조건이 없으면 None.

    후보: 1개 가격 상한, 1인 예산(한 사람에 하나로 본다), 총예산 ÷ 수량(나머지는 버려 예산을 넘지 않게 한다).
    """
    quantity = order_quantity(slots)
    limits = [slots.max_price, slots.budget_per_person]
    budget = total_budget(slots)
    if budget is not None and quantity:
        limits.append(budget // quantity)
    # 총예산이 수량보다 작으면 개당 상한은 0원이다. 0을 빼면 예산 제한 자체가 사라진다.
    known = [limit for limit in limits if limit is not None]
    return min(known) if known else None


def storage_level(slots: Slots) -> int | None:
    """고객 쪽 보관 설비 수준(STORAGE_LEVELS). 보관을 말하지 않았거나 '무관' 이면 None(거르지 않는다)."""
    return STORAGE_LEVELS.get(slots.storage or "")


def is_group_order(slots: Slots) -> bool:
    """단체주문 조건을 따질지 반환한다. 용도가 선물·행사여도 수량(또는 인원)이 기준 이상이면 공급 검사를 한다."""
    return slots.occasion == "단체주문" or (order_quantity(slots) or 0) >= GROUP_ORDER_MIN_QUANTITY


def missing(slots: Slots) -> list[str]:
    """추천에 꼭 필요한데 빠진 조건의 이름 목록. 비어 있으면 바로 검색할 수 있다.

    지역은 항상 필요하다(배송·수령이 불가능한 업체를 추천하면 안 된다).
    단체주문은 수량(또는 인원)과 받을 날짜가 있어야 "그날 그만큼 만들 수 있는 업체" 를 가릴 수 있다.
    단체주문의 보관 방법은 설정 RECO_ASK_STORAGE 가 켜져 있을 때 묻는다(냉장이 필요한 제품을 상온 보관만 되는 곳에 제안하지 않기 위해서다).
    """
    lacking = []
    if not slots.region:
        lacking.append("region")
    if is_group_order(slots):
        if not order_quantity(slots):
            lacking.append("quantity")
        if not slots.due_date:
            lacking.append("due_date")
        if settings.RECO_ASK_STORAGE and not slots.storage:
            lacking.append("storage")
    return lacking


# 빠진 조건별로 고객에게 되물을 말
QUESTIONS = {
    "region": "받으실 지역(예: 서울 강남구)을 알려 주세요.",
    "quantity": "필요한 수량(개수)이나 인원을 숫자로 알려 주세요.",
    "due_date": "받으셔야 하는 날짜를 알려 주세요.",
    "storage": "받으시는 곳에서 냉장 보관이 가능한가요? (상온만 가능 / 냉장 가능 / 냉동 가능 / 상관없음)",
}


def reask_question(lacking: list[str]) -> str:
    """빠진 조건들을 한 번에 묻는 문장을 만든다."""
    return " ".join(QUESTIONS[name] for name in lacking)

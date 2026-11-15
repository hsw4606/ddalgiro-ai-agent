"""공급 조건의 최신성 규칙과 견적 계산 — LLM 을 쓰지 않는 순수 계산 (안건 2).

쓰는 곳: reco/ranking.py(공급 신호), reco/graph.py 의 rank 단계(결과마다 확인 상태·견적을 붙인다), reco/explain.py(근거 문장).
왜 필요한가: 생산량·최소 수량·준비 기간은 수시로 바뀐다. DB 에 적힌 값이 오래됐으면 실제로는 못 만드는 업체를 제안하게 된다.
            그래서 "언제 확인한 값인지" 를 보고, 사람이 공급자에게 다시 확인해야 하는 경우를 결과에 표시한다.
규칙(연구개발계획서의 Capacity Freshness Rule):
  - 유효기간 안의 확인된 조건  → auto      바로 제안할 수 있다
  - 유효기간이 지났거나 확인 전 → reconfirm 공급자에게 다시 확인한다
  - 대량 주문                  → confirm   조건이 최신이어도 최종 견적 전에 반드시 공급자 확인을 받는다
견적: 수량 × 1개 가격 = 합계. 고객 예산과의 비교도 여기서 코드가 계산한다(LLM 은 이 숫자를 문장으로 옮기기만 한다).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.reco.slots import Slots, is_group_order, order_quantity, total_budget
from app.settings import settings

AUTO, RECONFIRM, CONFIRM = "auto", "reconfirm", "confirm"
# 확인 상태별로 화면·추천 사유에 그대로 쓰는 안내 문장
MESSAGES = {
    AUTO: "유효기간 안의 확인된 공급 조건이라 바로 제안할 수 있다.",
    RECONFIRM: "공급 조건을 확인한 지 오래됐거나 확인한 적이 없어, 제안 전에 공급자 재확인이 필요하다.",
    CONFIRM: "대량 주문이라 최종 견적 전에 공급자 확인을 반드시 받아야 한다.",
}


def is_fresh(candidate: dict[str, Any], today: date) -> bool:
    """공급 조건이 "확인됨" 이고 유효기간이 지나지 않았으면 True.

    candidate 는 검색 결과 한 건이다(confirmation_status·valid_until 칸을 본다). 칸이 없거나 비어 있으면 False.
    """
    valid_until = candidate.get("valid_until")
    return candidate.get("confirmation_status") == "confirmed" and valid_until is not None and valid_until >= today


def is_large(candidate: dict[str, Any], quantity: int | None) -> bool:
    """대량 주문인지: 수량이 CONFIRM_QUANTITY 이상이거나, 하루 최대 생산량의 CONFIRM_LOAD_RATIO 이상을 차지하면 True."""
    if not quantity:
        return False
    daily_max = candidate.get("max_qty_per_day")
    return quantity >= settings.CONFIRM_QUANTITY or bool(daily_max and quantity >= daily_max * settings.CONFIRM_LOAD_RATIO)


def check(candidate: dict[str, Any], slots: Slots, today: date) -> dict[str, str] | None:
    """후보 한 건의 공급 조건 확인 상태를 정한다.

    반환: {"status": auto / reconfirm / confirm, "message": 안내 문장}. 단체주문이 아니면 공급 조건을 따지지 않으므로 None.
    순서: 오래된 조건이 먼저다 — 조건 자체가 낡았으면 대량이든 아니든 재확인부터 해야 한다.
    """
    if not is_group_order(slots):
        return None
    if not is_fresh(candidate, today):
        status = RECONFIRM
    elif is_large(candidate, order_quantity(slots)):
        status = CONFIRM
    else:
        status = AUTO
    return {"status": status, "message": MESSAGES[status]}


def quote(candidate: dict[str, Any], slots: Slots) -> dict[str, Any] | None:
    """견적 한 줄을 계산한다.

    반환: {"quantity": 수량, "unit_price": 1개 가격, "total_price": 합계, "budget": 총예산 또는 None,
           "within_budget": 합계가 총예산 이하인지(총예산을 모르면 None)}.
          수량을 모르거나 메뉴 가격이 DB 에 없으면 계산할 수 없어 None — 지어내지 않는다.
    """
    quantity = order_quantity(slots)
    unit_price = candidate.get("price")
    if not quantity or unit_price is None:
        return None
    total = quantity * unit_price
    budget = total_budget(slots)
    return {
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total,
        "budget": budget,
        "within_budget": (total <= budget) if budget else None,
    }

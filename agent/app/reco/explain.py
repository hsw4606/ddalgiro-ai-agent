"""추천 사유를 근거 안에서만 쓴다 (안건 2 · 기획 03 §6.1·§6.6).

쓰는 곳: reco/graph.py 의 explain 단계.
원칙:   가격·평점·영업 정보·공급 조건 같은 사실은 DB 에서 읽은 값만 근거로 주고, LLM 은 그것을 문장으로 옮긴다.
        단체주문의 수량·준비 기간이 DB 에 없으면 지어내지 않고 "담당자 확인 필요" 라고 적는다.
        견적 합계와 공급 조건 확인 상태는 코드(reco/capacity.py)가 정한 것을 근거 문장으로 넣는다.
속도:   결과마다 LLM 을 한 번씩 부르므로 동시에 요청한다(결과가 5건이면 호출 5번이 한꺼번에 나간다).
밖으로: 아래 근거 문장(업체·메뉴·가격·공급 조건·견적·고객 조건)은 게이트웨이를 거쳐 Claude API 로 전달된다.
        근거에 고객의 이름·연락처 같은 개인정보나 공급가(원가)를 더하지 않는다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any

from app import grounding
from app.reco.slots import Slots, is_group_order, order_quantity


def _known(value: Any, unit: str = "") -> str:
    """DB 값이 있으면 '값+단위', 없으면 지어내지 않도록 '확인된 값 없음(담당자 확인 필요)' 을 돌려준다."""
    return f"{value}{unit}" if value is not None else "확인된 값 없음(담당자 확인 필요)"


def _yes_no(value: bool | None) -> str:
    """가능 여부를 글로 바꾼다. 값이 없으면 '담당자 확인 필요'."""
    if value is None:
        return "확인된 값 없음(담당자 확인 필요)"
    return "가능" if value else "불가"


def build_evidence(result: dict[str, Any], slots: Slots, broadened: bool = False) -> grounding.Evidence:
    """추천 결과 한 건의 근거 목록을 만든다. F 는 DB 에서 읽은 사실과 코드가 계산한 값, Q 는 고객이 말한 조건이다.

    result 에 capacity_check(공급 조건 확인 상태)·quote(견적)가 붙어 있으면 근거 F8·F9 로 넣는다.
    broadened=True 면 "요청한 종류가 없어 다른 종류까지 넓혀 찾았다" 는 사실을 근거(Q2)로 넣어 사유에 드러나게 한다.
    """
    url = result.get("source_url")
    # 업체가 요청 지역 밖에 있으면 "배송으로 맞춘 것" 임을 드러낸다. 가까운 업체로 오해하면 안 되기 때문이다
    region_text = f"업체 지역은 {result['region']}이다."
    if (result.get("region_match") or "local") != "local":
        region_text += f" 요청 지역 밖이지만 배송 가능 지역({', '.join(result.get('delivery_areas') or [])})에 들어 있다."
    evidence: grounding.Evidence = {
        "F1": {"text": f"업체 '{result['shop_name']}' 의 메뉴는 '{result['menu_name']}' 이고 가격은 {_known(result.get('price'), '원')}이다.", "url": url},
        "F2": {"text": region_text, "url": url},
        "F3": {"text": f"평점은 {_known(result.get('rating'), '점')}, 후기 수는 {result.get('review_count') or 0}건이다.", "url": url},
    }
    if result.get("description"):
        evidence["F4"] = {"text": f"메뉴 설명: {result['description']}", "url": url}
    if result.get("diet_tags"):
        evidence["F5"] = {"text": f"식이 표시: {', '.join(result['diet_tags'])}.", "url": url}
    if result.get("is_partner"):
        evidence["F6"] = {"text": "딸기로 입점사다.", "url": url}
    if is_group_order(slots):
        evidence["F7"] = {
            "text": (
                f"최소 주문 수량 {_known(result.get('moq'), '개')}, 하루 최대 생산량 {_known(result.get('max_qty_per_day'), '개')}, "
                f"준비 기간 {_known(result.get('lead_time_days'), '일')}, 개별 포장 {_yes_no(result.get('individual_packaging'))}, "
                f"선물 포장 {_yes_no(result.get('gift_packaging'))}, 세금계산서 {_yes_no(result.get('tax_invoice'))}."
            ),
            "url": url,
        }
    check = result.get("capacity_check")
    if check:
        # 공급 조건을 언제 확인했는지와, 사람이 공급자에게 확인해야 하는지를 그대로 알린다
        verified = result.get("last_verified_at")
        verified_text = f" 공급 조건을 마지막으로 확인한 날은 {verified.isoformat()}이다." if verified else " 공급 조건을 확인한 기록이 없다."
        evidence["F8"] = {"text": check["message"] + verified_text, "url": url}
    quote = result.get("quote")
    if quote:
        # 합계는 코드가 계산한 값이다. 원문 일치 검사가 속성·수량·가격을 함께 보존한다
        text = (f"견적은 {quote['quantity']}개 × {quote['unit_price']}원 = {quote['total_price']}원이다."
                if quote["total_price"] is not None else "수량 또는 단가가 확인되지 않아 견적 확정에 담당자 확인이 필요하다.")
        if quote["budget"] is not None and quote["within_budget"] is not None:
            text += f" 총예산 {quote['budget']}원 {'이내다' if quote['within_budget'] else '을 넘는다'}."
        evidence["F9"] = {"text": text, "url": url}
    if result.get("storage"):
        evidence["F10"] = {"text": f"보관 방법은 {result['storage']}이다.", "url": url}
    # 고객이 말한 조건도 근거로 준다. "조건에 맞는다" 는 문장이 이 번호를 가리키게 된다
    quantity = order_quantity(slots)
    conditions = [f"목적 {slots.purpose}" if slots.purpose else "",
                  f"종류 {slots.dessert}" if slots.dessert else "", f"맛 {', '.join(slots.taste)}" if slots.taste else "",
                  f"식이 {', '.join(slots.diet)}" if slots.diet else "", f"가격 {slots.max_price}원 이하" if slots.max_price else "",
                  f"1인 예산 {slots.budget_per_person}원" if slots.budget_per_person else "",
                  f"총예산 {slots.total_budget}원" if slots.total_budget else "",
                  f"지역 {slots.region}" if slots.region else "", f"인원 {slots.headcount}명" if slots.headcount else "",
                  f"수량 {quantity}개" if quantity else "", f"받을 날짜 {slots.due_date}" if slots.due_date else "",
                  f"보관 {slots.storage}" if slots.storage else "",
                  "개별 포장 필요" if slots.individual_packaging else "", "선물 포장 필요" if slots.gift_packaging else "",
                  "세금계산서 필요" if slots.tax_invoice else ""]
    evidence["Q1"] = {"text": "고객 조건: " + ", ".join(part for part in conditions if part) + ".", "url": None}
    if broadened and slots.dessert:
        evidence["Q2"] = {
            "text": f"요청한 종류({slots.dessert})이면서 나머지 조건을 모두 지키는 메뉴가 없어, 다른 종류까지 넓혀 찾은 결과다.",
            "url": None,
        }
    return evidence


def explain_one(result: dict[str, Any], slots: Slots, broadened: bool = False) -> dict[str, Any]:
    """결과 한 건의 추천 사유를 만든다.

    반환: {"reason": 글, "generated_by": 'llm' 또는 'template', "citations": 근거 목록}
    """
    evidence = build_evidence(result, slots, broadened)

    def fallback() -> grounding.GroundedText:
        """LLM 글을 쓸 수 없을 때 대신 쓸 글: DB 사실(F…)과 넓혀 찾았다는 사실(Q2)을 문장 그대로 나열한다."""
        return grounding.GroundedText(
            sentences=[grounding.Sentence(text=str(item["text"]), sources=[key]) for key, item in evidence.items()
                       if key.startswith("F") or key == "Q2"]
        )

    question = (
        "고객 조건에 비추어 이 업체·메뉴를 추천하는 이유를 2~3문장으로 써라. "
        "근거에 적힌 맛·특징만 말하고, 근거에 없는 맛이나 장점을 덧붙이지 마라. "
        "'확인된 값 없음' 인 항목은 담당자 확인이 필요하다고 그대로 알려라."
    )
    if (result.get("region_match") or "local") != "local":
        # 요청 지역 밖의 업체를 가까운 업체처럼 소개하면 안 된다
        question += " 업체가 요청 지역 밖에 있고 배송으로 받는다는 점(근거 F2)을 반드시 알려라."
    if (result.get("capacity_check") or {}).get("status") in ("reconfirm", "confirm"):
        # 공급자 확인이 필요한 결과는 그 사실이 사유에서 빠지면 안 된다
        question += " 공급자 확인이 필요하다는 점(근거 F8)을 반드시 한 문장으로 알려라."
    if "Q2" in evidence:
        # 넓혀 찾은 결과일 때만 붙인다. 늘 붙이면 모델이 정확히 맞는 결과에도 사과하는 문장을 쓴다
        question += " 이 메뉴는 고객이 요청한 종류와 다르므로, 첫 문장에서 그 점을 먼저 알려라(근거 Q2)."
    reason, generated_by = grounding.write_grounded(
        system="너는 디저트 주문 상담원이다. 고객에게 이 업체를 추천하는 이유를 쉬운 말로 설명한다.",
        question=question,
        evidence=evidence,
        fallback=fallback,
        required_sources={key for key in ("F1", "F2", "F8", "F9", "Q2") if key in evidence},
    )
    return {"reason": reason, "generated_by": generated_by, "citations": evidence}


def explain_all(results: list[dict[str, Any]], slots: Slots, broadened: bool = False) -> list[dict[str, Any]]:
    """여러 결과의 추천 사유를 동시에 만들어 각 결과에 붙인다(순서 유지)."""
    if not results:
        return []
    with ThreadPoolExecutor(max_workers=len(results)) as pool:
        # 새 스레드에는 문맥이 자동 전달되지 않아, 각 호출에 부모 추적을 복사한다.
        futures = [pool.submit(copy_context().run, explain_one, result, slots, broadened) for result in results]
        reasons = [future.result() for future in futures]
    return [{**result, **reason} for result, reason in zip(results, reasons)]

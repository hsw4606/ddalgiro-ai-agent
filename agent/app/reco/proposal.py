"""제안서 초안 — 한 요청의 제안 옵션들을 고객에게 보낼 글 하나로 묶는다 (안건 2). LLM 을 쓰지 않는다.

쓰는 곳: API 의 POST /v1/recommend/{요청번호}/proposal (main.py).
방법:   저장해 둔 제안 옵션(quote_options)의 가장 최근 회차를 순위대로 읽어, 옵션마다 "업체·메뉴 / 견적 / 공급 조건 / 추천 사유" 를 적는다.
        견적 숫자는 추천할 때 코드가 계산해 저장한 값이고, 추천 사유는 이미 근거 검사를 통과한 글이다.
        그래서 이 단계에서 새로 지어낼 내용이 없다 — 담당자는 이 초안을 다듬어 보내면 된다.
기록:   만든 초안은 proposal_logs 에 남기고, 고객이 채택했는지는 나중에 따로 적는다(제안 자동화의 품질을 재는 자료).
"""
from __future__ import annotations

from typing import Any

from app import db
from app.reco import capacity

def option_block(option: dict[str, Any]) -> str:
    """옵션 한 건을 제안서의 한 묶음(여러 줄)으로 만든다.

    option 은 quote_options 한 줄에 shop_name·menu_name 을 붙인 사전이다.
    견적 값이 없으면 지어내지 않고 "담당자 확인 필요" 라고 적는다.
    """
    lines = [f"{option['rank']}. {option['shop_name'] or '업체 정보 없음'} — {option['menu_name'] or '메뉴 정보 없음'}"]
    if option["quantity"] and option["unit_price"] is not None:
        lines.append(f"   견적: {option['quantity']:,}개 × {option['unit_price']:,}원 = {option['total_price']:,}원")
    else:
        lines.append("   견적: 수량 또는 가격이 확인되지 않아 담당자 확인이 필요합니다.")
    if option["capacity_check"]:
        lines.append(f"   공급 조건: {capacity.MESSAGES[option['capacity_check']]}")
    if option["reason"]:
        # 출처 번호를 그대로 유지한다. 같은 F1이라도 옵션별 근거 목록으로 구분할 수 있다.
        lines.append("   추천 사유: " + " ".join(option["reason"].split()))
    lines.append(f"   근거 (옵션 {option.get('id', '미지정')}):")
    for key, evidence in (option.get("citations") or {}).items():
        link = f" — {evidence['url']}" if evidence.get("url") else " — 추천 시점 DB 기록"
        lines.append(f"   [{key}] {evidence['text']}{link}")
    return "\n".join(lines)


def build_body(request_id: int, options: list[dict[str, Any]]) -> str:
    """제안서 초안 전체 글을 만든다. options 는 순위 순이다."""
    blocks = [f"단체주문 제안 (요청 번호 {request_id}, 옵션 {len(options)}개)", *[option_block(option) for option in options]]
    blocks.append("※ 가격·수량·납품 가능일은 제안 시점에 등록된 값입니다. 공급자 확인이 필요한 옵션은 확인한 뒤 확정합니다.")
    return "\n\n".join(blocks)


def create(request_id: int) -> dict[str, Any] | None:
    """요청의 가장 최근 회차 옵션으로 제안서 초안을 만들어 저장한다.

    반환: {"proposal_id", "request_id", "round", "option_ids", "body"}. 없는 요청이면 None.
    실패: 제안 옵션이 하나도 없는 요청(되묻기·결과 없음)이면 ValueError.
    """
    with db.conn() as c:
        if c.execute("SELECT 1 FROM group_order_requests WHERE id = %s", (request_id,)).fetchone() is None:
            return None
        # 업체·메뉴가 바뀌거나 삭제돼도 추천 당시 이름과 근거를 스냅샷에서 읽는다
        options = c.execute(
            """
            SELECT q.id, q.round, q.rank, q.quantity, q.unit_price, q.total_price, q.capacity_check, q.reason, q.citations,
                   q.snapshot->'result'->>'shop_name' AS shop_name, q.snapshot->'result'->>'menu_name' AS menu_name
            FROM quote_options q
            WHERE q.request_id = %s AND q.round = (SELECT max(round) FROM quote_options WHERE request_id = %s)
            ORDER BY q.rank
            """,
            (request_id, request_id),
        ).fetchall()
        if not options:
            raise ValueError("제안할 옵션이 없는 요청입니다.")
        if any(not option["citations"] or not option["shop_name"] for option in options):
            raise ValueError("추천 당시 근거가 없는 옛 제안입니다. 추천을 다시 받아 주세요.")
        body = build_body(request_id, options)
        option_ids = [option["id"] for option in options]
        proposal_id = c.execute(
            "INSERT INTO proposal_logs (request_id, option_ids, body) VALUES (%s, %s, %s) RETURNING id",
            (request_id, option_ids, body),
        ).fetchone()["id"]
    return {"proposal_id": proposal_id, "request_id": request_id, "round": options[0]["round"], "option_ids": option_ids, "body": body}

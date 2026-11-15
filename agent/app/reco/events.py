"""추천 옵션의 최종 의사와 주문·납품을 기록한다.

main.py의 인증된 반응/납품 API가 부른다. 옵션 행을 잠가 동시에 온 요청도 순서대로 처리하고,
전송 식별자(event_key)가 같은 재시도는 기존 결과를 돌려준다. 운영 주문 서버를 직접 호출하지는 않는다.
없는 옵션은 LookupError, 잘못된 순서·충돌한 식별자는 ValueError이며 API가 각각 404·409로 바꾼다.
"""
from __future__ import annotations

from typing import Any

from app import db


def record(option_id: int, action: str, note: str | None, order_ref: str | None,
           event_key: str | None, *, delivery: bool = False) -> dict[str, Any]:
    """옵션 하나의 사건을 저장한다. order_ref는 외부 주문 번호, event_key는 재전송에서도 유지할 식별자다.

    주문은 마지막 공급자 의사가 수락일 때, 납품 사건은 그 수락 이후 주문 번호와 연결될 때만 허용한다.
    같은 최종 의사·같은 주문·같은 납품 완료의 반복은 새 사건으로 늘리지 않는다.
    """
    table = "delivery_cs_logs" if delivery else "order_conversion_logs"
    kind = "event" if delivery else "action"
    memo = "note" if delivery else "reason"
    order_ref = (order_ref or "").strip() or None
    with db.conn() as c:
        # 동일 옵션의 상태 변경을 직렬화해 수락/주문/납품 순서가 서로 엇갈리지 않게 한다.
        option = c.execute("SELECT request_id, snapshot FROM quote_options WHERE id=%s FOR UPDATE", (option_id,)).fetchone()
        if option is None:
            raise LookupError("제안 번호가 없습니다.")
        if event_key:
            old = c.execute(f"SELECT * FROM {table} WHERE quote_option_id=%s AND event_key=%s",
                            (option_id, event_key)).fetchone()
            if old:
                if old[kind] != action or old["order_ref"] != order_ref or old[memo] != note:
                    raise ValueError("같은 event_key에 다른 내용을 보낼 수 없습니다.")
                return {"id": old["id"], "created_at": old["created_at"], "request_id": option["request_id"]}
        # 최종 공급자 의사와 마지막 주문을 읽는다. 과거에 한 번 수락했다는 사실만으로 주문하지 않는다.
        supplier = c.execute("SELECT id, action FROM order_conversion_logs WHERE quote_option_id=%s "
                             "AND action IN ('supplier_accepted','supplier_declined') ORDER BY id DESC LIMIT 1",
                             (option_id,)).fetchone()
        order = c.execute("SELECT id, order_ref FROM order_conversion_logs WHERE quote_option_id=%s "
                          "AND action='ordered' ORDER BY id DESC LIMIT 1", (option_id,)).fetchone()
        if action == "ordered" or delivery:
            if not supplier or supplier["action"] != "supplier_accepted":
                raise ValueError("공급자의 최종 수락을 먼저 기록해야 합니다.")
            if not option["snapshot"].get("constraints_passed"):
                raise ValueError("추천 당시 조건 확인 기록이 없습니다. 추천을 다시 받아 주세요.")
            if not order_ref:
                raise ValueError("실제 주문을 연결할 order_ref가 필요합니다.")
        if delivery and (not order or order["id"] <= supplier["id"] or order["order_ref"] != order_ref):
            raise ValueError("최종 수락 이후 확정된 주문 번호에만 납품 결과를 기록할 수 있습니다.")
        # 고객/공급자의 의사 변화는 같은 범주의 마지막 사건과 비교한다. 반복 전송은 한 번만 센다.
        if not delivery and action != "ordered":
            group = ["selected", "rejected"] if action in ("selected", "rejected") else ["supplier_accepted", "supplier_declined"]
            previous = c.execute("SELECT id, action, created_at FROM order_conversion_logs WHERE quote_option_id=%s "
                                 "AND action=ANY(%s) ORDER BY id DESC LIMIT 1", (option_id, group)).fetchone()
        else:
            previous = c.execute(f"SELECT id, {kind} AS action, created_at FROM {table} "
                                 f"WHERE quote_option_id=%s AND {kind}=%s AND order_ref=%s ORDER BY id DESC LIMIT 1",
                                 (option_id, action, order_ref)).fetchone()
            # 재수락 이후에는 새 주문 확정이 필요하다. 옛 주문을 재시도처럼 취급하지 않는다.
            if previous and not delivery and previous["id"] <= supplier["id"]:
                previous = None
        if previous and previous["action"] == action and (not delivery or action == "delivered") and not event_key:
            return {"id": previous["id"], "created_at": previous["created_at"], "request_id": option["request_id"]}
        saved = c.execute(f"INSERT INTO {table} (quote_option_id,{kind},{memo},order_ref,event_key) "
                          "VALUES (%s,%s,%s,%s,%s) RETURNING id, created_at",
                          (option_id, action, note, order_ref, event_key)).fetchone()
    return {**saved, "request_id": option["request_id"]}

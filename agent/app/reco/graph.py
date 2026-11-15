"""자연어 추천 그래프 — "이해 → (조건이 빠졌으면) 되묻기 / 검색 → (없으면) 넓혀 다시 검색 → 순위 → 추천 사유" (안건 2 · 기획 03 §6.2).

쓰는 곳: API 의 POST /v1/recommend 와 POST /v1/recommend/{요청번호}/reroute (main.py).
왜 그래프인가: 한 줄로 흐르는 처리로는 "되묻기" 와 "다시 검색" 같은 갈림길을 표현하기 어렵다.
              단계와 갈림길을 그래프로 두면 단계별 실행 기록도 따로 남길 수 있다.
책임 나누기: 이해(조건 뽑기)와 추천 사유만 LLM, 검색은 SQL, 순위·견적·공급 조건 확인 상태는 코드(ranking.py·capacity.py).
끝나는 세 가지: ok(결과 있음) / reask(빠진 조건을 되물음) / empty(조건을 모두 지키는 업체 없음 — 어느 조건에서 막혔는지 함께 알린다).
대체 공급자 찾기: 공급자가 거절하면 저장해 둔 조건으로 그 업체를 빼고 검색부터 다시 돈다(reroute). 조건을 다시 뽑지 않으므로 이해 단계를 건너뛴다.
대화 이어가기: 서버는 대화를 기억하지 않는다. 되물음을 받은 쪽이 원래 문의 뒤에 답을 이어 붙여 다시 보낸다.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from app import observability, dates, db
from app.reco import capacity, explain, ranking, search, slots as slot_rules
from app.reco.slots import Slots
from app.settings import settings

# 샘플 키워드의 이름 앞에 붙는 표시(jobs/seed_sample.py). 샘플 메뉴의 이름·설명에는 이 표시가 없어 떼고 견준다
SAMPLE_MARK = "(샘플) "
# 응답의 results 에 내보내는 칸. 임베딩 유사도 같은 내부 값은 score_detail 안에 있다
RESULT_KEYS = (
    "rank", "quote_option_id", "shop_id", "shop_name", "region", "region_match", "delivery_areas", "is_partner",
    "rating", "review_count", "menu_id", "menu_name", "category", "price", "diet_tags", "storage",
    "moq", "max_qty_per_day", "lead_time_days", "individual_packaging", "gift_packaging", "tax_invoice",
    "last_verified_at", "valid_until", "confirmation_status", "capacity_check", "quote",
    "keyword_hits", "score", "score_detail", "reason", "generated_by", "citations",
)


class RecoState(TypedDict, total=False):
    """그래프의 단계들이 주고받는 값."""

    text: str                 # 문의 글 (되물은 뒤에는 추가 답이 이어 붙어 있다)
    slots: Slots              # 뽑은 조건. 대체 공급자를 찾을 때는 저장해 둔 조건이 처음부터 들어 있다
    missing: list[str]        # 빠진 필수 조건 이름
    question: str             # 되물을 말
    exclude_shops: list[int]  # 후보에서 뺄 업체(이미 거절한 공급자)
    candidates: list[dict[str, Any]]
    broadened: bool           # 종류 조건을 풀어 넓혀 찾았는지
    results: list[dict[str, Any]]
    audit: dict[str, Any]     # ok: 입점사 가산 감사 기록, empty: 막힌 조건(blockers)
    status: str               # ok(결과 있음) / reask(되묻기) / empty(조건에 맞는 업체 없음)
    request_id: int           # 대체 공급자를 찾을 때는 원래 요청 번호가 처음부터 들어 있다
    round: int                # 제안 회차. 처음은 1, 대체 공급자를 찾을 때마다 1씩 늘어난다


def _timed(node: str, func: Callable[[RecoState], dict]) -> Callable[[RecoState], dict]:
    """단계 함수를 감싸 걸린 시간과 성공 여부를 agent_runs 에 남긴다. 예외는 그대로 다시 올린다."""

    def wrapper(state: RecoState) -> dict:
        """단계를 실행하고 시간을 잰다. 성공·실패 모두 기록한 뒤, 실패는 다시 올린다."""
        with observability.step("reco." + node):
            started = time.monotonic()
            try:
                result = func(state)
            except Exception as error:
                db.record_run("reco", node, False, int((time.monotonic() - started) * 1000), {"error": str(error)[:300]})
                raise
            db.record_run("reco", node, True, int((time.monotonic() - started) * 1000))
            return result

    return wrapper


def understand(state: RecoState) -> dict:
    """문의에서 조건을 뽑고, 빠진 필수 조건을 규칙으로 찾는다."""
    extracted = slot_rules.extract(state["text"])
    return {"slots": extracted, "missing": slot_rules.missing(extracted)}


def ask(state: RecoState) -> dict:
    """빠진 조건을 되묻는 말을 만든다. 여기서 그래프가 끝나고, 답을 받으면 처음부터 다시 돈다."""
    return {"status": "reask", "question": slot_rules.reask_question(state["missing"])}


def retrieve(state: RecoState) -> dict:
    """조건을 그대로 지켜 후보를 찾는다."""
    found = search.search(state["slots"], state["text"], exclude_shops=state.get("exclude_shops"))
    return {"candidates": found, "broadened": False}


def retrieve_broad(state: RecoState) -> dict:
    """디저트 종류 조건만 풀어 다시 찾는다. 지역·식이·가격·보관·공급 조건은 풀지 않는다."""
    found = search.search(state["slots"], state["text"], broaden=True, exclude_shops=state.get("exclude_shops"))
    return {"candidates": found, "broadened": True}


def no_result(state: RecoState) -> dict:
    """넓혀 찾아도 없을 때. 조건을 어기는 업체를 대신 보여 주지 않고, 어느 조건에서 막혔는지만 알린다."""
    blockers = search.diagnose(state["slots"], state["text"], exclude_shops=state.get("exclude_shops"))
    return {"status": "empty", "results": [], "audit": {"blockers": blockers}}


def rank(state: RecoState) -> dict:
    """코드로 순위를 매기고 위에서부터 RECO_RESULTS 개를 남긴다. 결과마다 공급 조건 확인 상태와 견적을 붙인다."""
    with db.conn() as c:
        # 키워드마다 가장 최근 트렌드 점수 (순위의 트렌드 신호로 쓴다)
        rows = c.execute(
            "SELECT DISTINCT ON (s.keyword_id) k.term, s.score FROM trend_scores s "
            "JOIN keywords k ON k.id = s.keyword_id ORDER BY s.keyword_id, s.score_date DESC"
        ).fetchall()
    trend_scores = {row["term"].removeprefix(SAMPLE_MARK): row["score"] for row in rows}
    today = dates.today()
    ranked = ranking.rank(state["candidates"], state["slots"], trend_scores, today)
    # 입점사 가산이 상위 결과를 얼마나 바꿨는지는 자르기 전의 전체 순위로 잰다
    audit = ranking.boost_audit(ranked, settings.RECO_RESULTS)
    results = [
        {**item, "capacity_check": capacity.check(item, state["slots"], today), "quote": capacity.quote(item, state["slots"])}
        for item in ranked[: settings.RECO_RESULTS]
    ]
    return {"results": results, "audit": audit}


def explain_results(state: RecoState) -> dict:
    """결과마다 근거 안에서만 쓴 추천 사유를 붙인다."""
    return {"results": explain.explain_all(state["results"], state["slots"], state.get("broadened", False)), "status": "ok"}


def save(state: RecoState) -> dict:
    """요청과 제안 내역을 DB 에 남긴다(순위 검증과 나중의 학습 자료). 결과에 제안 번호를 붙여 돌려준다.

    처음 요청이면 group_order_requests 에 새 줄을 만든다. 대체 공급자를 찾은 것이면(request_id 가 이미 있음)
    원래 요청의 감사 기록만 고치고, 새 제안은 다음 회차(round)로 덧붙인다. 한 트랜잭션이라 중간에 실패하면 모두 되돌린다.
    """
    audit = json.dumps(state.get("audit"), ensure_ascii=False) if state.get("audit") else None
    round_no = state.get("round", 1)
    with db.conn() as c:
        if state.get("request_id"):
            request_id = state["request_id"]
            c.execute("UPDATE group_order_requests SET audit = %s WHERE id = %s", (audit, request_id))
        else:
            request_id = c.execute(
                "INSERT INTO group_order_requests (raw_text, slots, status, audit) VALUES (%s, %s, %s, %s) RETURNING id",
                (state["text"], state["slots"].model_dump_json(), state["status"], audit),
            ).fetchone()["id"]
        results = []
        for position, result in enumerate(state.get("results") or [], start=1):
            quote = result.get("quote") or {}
            # 제약 검증이 실패한 결과는 기록·노출하지 않는다. 검색의 가격 필터와 별도로 마지막에 한 번 더 확인한다.
            if quote.get("within_budget") is False:
                raise ValueError("총예산을 넘는 후보는 제안할 수 없습니다.")
            snapshot = {"constraints_passed": True, "slots": state["slots"].model_dump(),
                        "result": {key: result.get(key) for key in RESULT_KEYS if key != "citations"}}
            option_id = c.execute(
                "INSERT INTO quote_options (request_id, round, shop_id, menu_id, rank, score, score_detail, "
                "quantity, unit_price, total_price, capacity_check, reason, generated_by, citations, snapshot) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (request_id, round_no, result["shop_id"], result["menu_id"], position, result["score"],
                 json.dumps(result["score_detail"], ensure_ascii=False),
                 quote.get("quantity"), quote.get("unit_price"), quote.get("total_price"),
                 (result.get("capacity_check") or {}).get("status"), result.get("reason"), result.get("generated_by"),
                 json.dumps(result.get("citations") or {}, ensure_ascii=False),
                 json.dumps(snapshot, ensure_ascii=False, default=str)),
            ).fetchone()["id"]
            results.append({**result, "rank": position, "quote_option_id": option_id})
    return {"request_id": request_id, "results": results}


def build_graph():
    """단계와 갈림길을 이어 실행 가능한 그래프로 만든다."""
    graph = StateGraph(RecoState)
    for name, func in [("understand", understand), ("ask", ask), ("retrieve", retrieve),
                       ("retrieve_broad", retrieve_broad), ("no_result", no_result),
                       ("rank", rank), ("explain", explain_results), ("save", save)]:
        graph.add_node(name, _timed(name, func))
    # 시작점: 조건이 이미 있으면(대체 공급자 찾기) 검색부터, 없으면 문의를 이해하는 단계부터 돈다
    graph.set_conditional_entry_point(lambda s: "retrieve" if s.get("slots") else "understand",
                                      {"retrieve": "retrieve", "understand": "understand"})
    # 갈림길 1: 필수 조건이 빠졌으면 되묻고, 아니면 검색한다
    graph.add_conditional_edges("understand", lambda s: "ask" if s["missing"] else "retrieve", {"ask": "ask", "retrieve": "retrieve"})
    # 갈림길 2: 후보가 없으면 종류 조건을 풀어 한 번만 다시 찾는다
    graph.add_conditional_edges("retrieve", lambda s: "rank" if s["candidates"] else "retrieve_broad", {"rank": "rank", "retrieve_broad": "retrieve_broad"})
    # 갈림길 3: 넓혀 찾아도 없으면 "없음" 으로 끝낸다
    graph.add_conditional_edges("retrieve_broad", lambda s: "rank" if s["candidates"] else "no_result", {"rank": "rank", "no_result": "no_result"})
    graph.add_edge("rank", "explain")
    graph.add_edge("explain", "save")
    graph.add_edge("ask", "save")
    graph.add_edge("no_result", "save")
    graph.add_edge("save", END)
    return graph.compile()


_graph = None


@observability.traced("reco.request")
def _invoke(initial: RecoState) -> dict[str, Any]:
    """그래프를 돌리고 마지막 상태를 API 가 돌려줄 사전으로 바꾼다. 그래프는 처음 불릴 때 한 번만 조립한다."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    state = _graph.invoke(initial)
    # 응답에 필요한 칸만 고른다
    results = [{key: result.get(key) for key in RESULT_KEYS} for result in state.get("results") or []]
    audit = state.get("audit") or {}
    return {
        "status": state["status"],
        "request_id": state["request_id"],
        "round": state.get("round", 1),
        "slots": state["slots"].model_dump(),
        "question": state.get("question"),
        "broadened": state.get("broadened", False),
        "results": results,
        # empty 일 때: 어느 조건 하나만 빼면 몇 곳이 맞는지. ok 일 때: 입점사 가산 감사 기록
        "blockers": audit.get("blockers", []),
        "audit": audit if state["status"] == "ok" else None,
    }


def run(text: str) -> dict[str, Any]:
    """문의 한 건을 처리해 API 가 돌려줄 사전을 만든다.

    반환: status 가 'reask' 면 question(되물을 말), 'ok' 면 results(순위·점수 내역·견적·공급 조건 확인 상태·추천 사유),
          'empty' 면 빈 results 와 blockers(막힌 조건). 어느 경우든 slots(뽑은 조건)와 request_id 가 들어 있다.
    """
    return _invoke({"text": text})


def reroute(request_id: int) -> dict[str, Any] | None:
    """공급자가 거절한 요청의 대체 공급자를 찾는다.

    방법: 저장해 둔 문의 글·조건을 그대로 쓰고, 이 요청에서 공급자가 거절했거나(supplier_declined) 고객이 버린(rejected)
          업체를 후보에서 뺀 뒤 검색 → 순위 → 추천 사유를 다시 돈다. 새 제안은 같은 요청의 다음 회차로 저장한다.
    반환: run 과 같은 모양의 사전(round 가 1 늘어난다). 없는 요청이면 None.
    실패: 결과가 있던 요청(status 가 ok)이 아니면 다시 찾을 조건이 없으므로 ValueError.
    """
    with db.conn() as c:
        request = c.execute("SELECT raw_text, slots, status FROM group_order_requests WHERE id = %s", (request_id,)).fetchone()
        if request is None:
            return None
        if request["status"] != "ok":
            raise ValueError("추천 결과가 있던 요청만 대체 공급자를 찾을 수 있습니다.")
        excluded = c.execute(
            "SELECT DISTINCT shop_id FROM quote_outcomes WHERE request_id = %s AND (supplier_declined OR rejected)",
            (request_id,),
        ).fetchall()
        last_round = c.execute("SELECT max(round) AS n FROM quote_options WHERE request_id = %s", (request_id,)).fetchone()["n"]
    return _invoke({
        "text": request["raw_text"],
        "slots": Slots.model_validate(request["slots"]),
        "exclude_shops": [row["shop_id"] for row in excluded],
        "request_id": request_id,
        "round": (last_round or 1) + 1,
    })

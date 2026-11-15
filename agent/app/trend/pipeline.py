"""트렌드 분석 그래프 — 키워드 하나를 "읽기 → 감성 → 점수 → 브리핑" 순서로 처리한다 (안건 1 · 기획 02 §5.5).

쓰는 곳: 매일 도는 분석 작업(jobs/run_trend.py)과 API 의 "지금 다시 분석" 요청(main.py).
왜 그래프인가: 단계를 나눠 두면 어느 단계가 실패했는지·얼마나 걸렸는지 따로 기록할 수 있고,
              한 단계가 실패해도 나머지를 진행할 수 있다(예: 감성 분류가 실패해도 점수는 계산한다).
책임 나누기: 점수·단계 같은 숫자는 코드(scoring.py)가 계산하고, LLM 은 감성 분류와 브리핑 글쓰기만 한다.
믿을 수 있는 신호만 쓰기: 광고·체험단으로 표시된 글은 감성과 브리핑 근거에서 빼고,
              언급과 검색이 함께 오르는지(교차검증)와 약 3개월 흐름(긴 시간창)을 점수 옆에 함께 남긴다.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from app import db, grounding, llm
from app.trend import signals
from app.trend.signals import metric_series, source_views, family_growth

WINDOW_DAYS = 30        # 화면에 표시할 기간(일)
LONG_WINDOW_DAYS = 91   # 긴 시간창(약 3개월 = 13주)에 읽을 최대 기간(일)
SENTIMENT_BATCH = 20    # LLM 한 번에 분류를 맡기는 글 수
EVIDENCE_DOCS = 6       # 브리핑 근거로 주는 최근 글 수
# 증가율·가속에 쓰는 최근 10일은 선택한 두 계열 이상의 출처가 모두 확인돼야 한다.
# 수집 누락은 0건이 아니므로, 빠진 날이나 출처가 있으면 점수를 매기지 않는다.
MIN_HISTORY_DAYS = 10
NOT_ENOUGH = "자료부족"  # 위 조건에 걸린 키워드의 단계 표시
# 브리핑을 만드는 단계. 나머지 단계는 아직 추천할 때가 아니라 글을 만들지 않는다
INSIGHT_STAGES = {"성장기", "정점"}


class TrendState(TypedDict, total=False):
    """그래프의 단계들이 주고받는 값. 각 단계는 자기가 채운 칸만 돌려준다."""

    keyword_id: int
    force_insight: bool       # True 면 단계와 상관없이 브리핑을 만든다(API 의 요청)
    term: str
    as_of: date | None        # 기준일 = 이 키워드의 지표가 있는 가장 최근 날짜. 지표가 없으면 None
    history_days: int         # 선택 출처가 모두 연속 완료된 날수(두 계열 미만이면 0)(최대 WINDOW_DAYS)
    mentions: list[float | None]  # 주 계열의 정규화 지수. 10일 완료 출처가 없으면 빈 목록
    search: list[float | None]    # 보조 계열의 정규화 지수. 실제 계열은 basis에 기록
    search_long: list[float]      # 기준일까지 빠진 날 없이 이어진 장기 계열 지수(최대 LONG_WINDOW_DAYS 일). 긴 시간창의 재료
    by_source: dict[str, dict]    # 출처별 최근 10일 값과 증가율(source_views 의 결과). 날짜별 원값은 내부 계산용이며 저장할 때 제외한다
    n_sources: int
    raw_mentions_7d: int
    basis: dict[str, Any]
    sentiment_pos: float | None
    score: dict[str, Any]
    insight: dict[str, Any] | None


def _timed(node: str, func: Callable[[TrendState], dict]) -> Callable[[TrendState], dict]:
    """단계 함수를 감싸 걸린 시간과 성공 여부를 agent_runs 에 남긴다. 예외는 그대로 다시 올린다."""

    def wrapper(state: TrendState) -> dict:
        """단계를 실행하고 시간을 잰다. 성공·실패 모두 기록한 뒤, 실패는 다시 올린다."""
        started = time.monotonic()
        try:
            result = func(state)
        except Exception as error:
            db.record_run("trend", node, False, int((time.monotonic() - started) * 1000), {"error": str(error)[:300]})
            raise
        db.record_run("trend", node, True, int((time.monotonic() - started) * 1000), {"keyword_id": state["keyword_id"]})
        return result

    return wrapper


# ───────────────────────────── 단계 1: 읽기 ─────────────────────────────

def load(state: TrendState) -> dict:
    """최근 91일 출처별 원자료를 읽고 공통 계산기로 보낸다. 없는 키워드는 오류, 지표가 없으면 분석을 끝낸다."""
    with db.conn() as c:
        keyword = c.execute("SELECT term, is_sample FROM keywords WHERE id=%s", (state["keyword_id"],)).fetchone()
        if keyword is None:
            raise ValueError(f"키워드 {state['keyword_id']} 가 없습니다.")
        latest = c.execute("SELECT max(metric_date) AS d FROM daily_metrics WHERE keyword_id=%s",
                           (state["keyword_id"],)).fetchone()["d"]
        if latest is None:
            return {"term": keyword["term"], "as_of": None}
        # 아직 모으지 않은 오늘을 0으로 세지 않도록 실제 마지막 지표 날짜를 기준으로 삼는다.
        rows = c.execute("SELECT metric_date,source,mention_count,search_ratio,is_complete FROM daily_metrics "
                         "WHERE keyword_id=%s AND metric_date > %s AND metric_date <= %s",
                         (state["keyword_id"], latest - timedelta(days=LONG_WINDOW_DAYS), latest)).fetchall()
    return {"term": keyword["term"], **signals.prepare(rows, latest, keyword["is_sample"])}


# ───────────────────────────── 단계 2: 감성 ─────────────────────────────

class _Label(BaseModel):
    """글 하나의 감성 분류 결과. id 는 documents 의 번호, label 은 pos(호감)·neg(불만)·neu(중립)."""

    id: int
    label: Literal["pos", "neg", "neu"]


class _Labels(BaseModel):
    """여러 글의 분류 결과 묶음. LLM 이 이 양식으로 답한다."""

    items: list[_Label]


def sentiment(state: TrendState) -> dict:
    """아직 분류하지 않은 글의 감성을 LLM 으로 분류하고, 최근 14일의 긍정 비율을 계산한다.

    광고·체험단으로 표시된 글(is_ad)은 분류하지도, 비율에 넣지도 않는다. 대가를 받고 쓴 호평이 호감도를 부풀리기 때문이다.
    LLM 호출이 실패해도 그래프를 멈추지 않는다 — 이미 분류된 글만으로 비율을 낸다.
    """
    keyword_id = state["keyword_id"]
    with db.conn() as c:
        pending = c.execute(
            "SELECT id, title, snippet FROM documents WHERE keyword_id = %s AND sentiment IS NULL AND NOT is_ad "
            "ORDER BY posted_at DESC NULLS LAST LIMIT %s",
            (keyword_id, SENTIMENT_BATCH),
        ).fetchall()
    if pending:
        listing = "\n".join(f"{row['id']}: {row['title']} — {row['snippet'] or ''}" for row in pending)
        try:
            labels = llm.ask_structured(
                _Labels,
                "너는 디저트 관련 글의 감성을 분류한다. 글마다 pos(호감·추천), neg(불만·비추천), neu(정보·중립) 중 하나를 고른다.",
                f"키워드: {state['term']}\n아래 각 줄은 '번호: 제목 — 요약' 이다. 모든 번호에 label 을 붙여라.\n{listing}",
            )
            known = {row["id"] for row in pending}
            with db.conn() as c:
                for item in labels.items:
                    # 모델이 목록에 없는 번호를 지어내면 무시한다
                    if item.id in known:
                        c.execute("UPDATE documents SET sentiment = %s WHERE id = %s", (item.label, item.id))
        except Exception:  # noqa: BLE001 - 분류 실패는 다음 실행에서 다시 시도된다
            pass
    with db.conn() as c:
        row = c.execute(
            """
            SELECT count(*) FILTER (WHERE sentiment = 'pos') AS pos, count(*) AS total
            FROM documents
            WHERE keyword_id = %s AND sentiment IS NOT NULL AND NOT is_ad AND posted_at > %s
            """,
            (keyword_id, state["as_of"] - timedelta(days=14)),
        ).fetchone()
    return {"sentiment_pos": (row["pos"] / row["total"]) if row["total"] else None}


# ───────────────────────────── 단계 3: 점수 ─────────────────────────────

def score(state: TrendState) -> dict:
    """트렌드 점수와 확산 단계를 코드로 계산해 trend_scores 에 저장한다. LLM 을 쓰지 않는다."""
    result = signals.compute(state)
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO trend_scores
                (keyword_id, score_date, score, stage, growth_rate, momentum, sentiment_pos, n_sources, mentions_7d,
                 search_growth, signal_check, windows, by_source)
            VALUES (%(keyword_id)s, %(score_date)s, %(score)s, %(stage)s, %(growth_rate)s, %(momentum)s,
                    %(sentiment_pos)s, %(n_sources)s, %(mentions_7d)s,
                    %(search_growth)s, %(signal_check)s, %(windows_json)s, %(by_source_json)s)
            ON CONFLICT (keyword_id, score_date) DO UPDATE SET
                score = EXCLUDED.score, stage = EXCLUDED.stage, growth_rate = EXCLUDED.growth_rate,
                momentum = EXCLUDED.momentum, sentiment_pos = EXCLUDED.sentiment_pos,
                n_sources = EXCLUDED.n_sources, mentions_7d = EXCLUDED.mentions_7d,
                search_growth = EXCLUDED.search_growth, signal_check = EXCLUDED.signal_check, windows = EXCLUDED.windows,
                by_source = EXCLUDED.by_source
            """,
            {**result, "keyword_id": state["keyword_id"], "score_date": state["as_of"],
             "windows_json": json.dumps(result["windows"], ensure_ascii=False) if result["windows"] else None,
             "by_source_json": json.dumps(result["by_source"], ensure_ascii=False) if result["by_source"] else None},
        )
    return {"score": result}


# ───────────────────────────── 단계 4: 브리핑 ─────────────────────────────

def build_evidence(term: str, as_of: date, score_row: dict[str, Any], docs: list[dict]) -> grounding.Evidence:
    """브리핑에 쓸 근거 목록을 만든다. M 으로 시작하는 번호는 코드가 계산한 지표, D 는 수집한 글이다.

    여기 적힌 숫자가 브리핑에 나올 수 있는 숫자의 전부다(grounding.check 가 그 밖의 숫자를 막는다).
    M8(신호 불일치)·M9(약 3개월 흐름)는 해당 값이 있을 때만 들어간다.
    """
    # 실제로 선택한 계열을 문장에 적는다. SNS 가속을 검색 가속이라고 설명하지 않는다.
    basis = (score_row.get("by_source") or {}).get("basis") or {}
    primary = basis.get("primary_family", "글")
    secondary = basis.get("secondary_family", "검색")
    # 증가율은 "몇 % 늘었다 / 줄었다" 로 풀어 쓴다(음수 기호를 문장에 두지 않는다)
    change = round(score_row["growth_rate"] * 100)
    change_text = f"{abs(change)}% {'늘었다' if change >= 0 else '줄었다'}"
    evidence: grounding.Evidence = {
        "M1": {"text": f"'{term}' 의 트렌드 점수는 {score_row['score']}점이다 (기준일 {as_of.isoformat()}).", "url": None},
        "M2": {"text": f"확산 단계는 {score_row['stage']}다.", "url": None},
        "M3": {"text": f"최근 3일 평균 {primary} 지수는 그 직전 4일 평균보다 {change_text}.", "url": None},
        "M4": {
            "text": f"{secondary} 지수의 증가는 빨라지는 중이다." if score_row["momentum"] > 0 else f"{secondary} 지수의 증가는 빨라지고 있지 않다.",
            "url": None,
        },
        "M5": {"text": f"최근 7일 언급 수 합계는 {score_row['mentions_7d']}건이다.", "url": None},
        "M6": {"text": f"신호가 잡힌 출처는 {score_row['n_sources']}곳이다.", "url": None},
    }
    if score_row.get("sentiment_pos") is not None:
        evidence["M7"] = {"text": f"최근 글의 긍정 비율은 {round(score_row['sentiment_pos'] * 100)}%다.", "url": None}
    # 교차검증 결과는 '불일치' 일 때만 알린다. 업체가 서둘러 준비하지 않도록 조심할 이유이기 때문이다
    if score_row.get("signal_check") == "불일치":
        evidence["M8"] = {"text": "글·검색·SNS 신호 가운데 한 가지만 늘고 있어, 진짜 인기인지 더 지켜볼 필요가 있다.", "url": None}
    long_view = (score_row.get("windows") or {}).get("3m")
    if long_view:
        evidence["M9"] = {"text": f"주 단위로 본 최근 약 3개월 흐름의 확산 단계는 {long_view['stage']}다.", "url": None}
    for doc in docs:
        posted = doc["posted_at"].isoformat() if doc["posted_at"] else "날짜 모름"
        evidence[f"D{doc['id']}"] = {
            "text": f"({doc['source']}, {posted}) {doc['title']} — {doc['snippet'] or ''}",
            "url": doc["url"],
        }
    return evidence


def insight(state: TrendState) -> dict:
    """근거 안에서만 쓰는 브리핑을 만들어 trend_insights 에 저장한다.

    성장기·정점이 아니면(그리고 강제 요청이 아니면) 만들지 않는다. 자료가 부족한 키워드는 강제 요청이어도 만들지 않는다.
    """
    score_row = state["score"]
    if score_row["stage"] == NOT_ENOUGH:
        # 같은 기준일을 다시 분석한 결과가 자료부족이면 이전 브리핑도 공개되지 않게 지운다
        with db.conn() as c:
            c.execute("DELETE FROM trend_insights WHERE keyword_id = %s AND score_date = %s",
                      (state["keyword_id"], state["as_of"]))
        return {"insight": None}
    if score_row["stage"] not in INSIGHT_STAGES and not state.get("force_insight"):
        return {"insight": None}
    with db.conn() as c:
        docs = c.execute(
            # 광고·체험단으로 표시된 글은 근거로 주지 않는다
            "SELECT id, source, title, snippet, url, posted_at FROM documents WHERE keyword_id = %s AND NOT is_ad "
            "ORDER BY posted_at DESC NULLS LAST LIMIT %s",
            (state["keyword_id"], EVIDENCE_DOCS),
        ).fetchall()
    evidence = build_evidence(state["term"], state["as_of"], score_row, docs)

    def fallback() -> grounding.GroundedText:
        """LLM 글을 쓸 수 없을 때 대신 쓸 글: 지표 근거(M…)를 문장 그대로 나열한다. 지어낼 여지가 없다."""
        return grounding.GroundedText(
            sentences=[grounding.Sentence(text=str(item["text"]), sources=[key]) for key, item in evidence.items() if key.startswith("M")]
        )

    briefing, generated_by = grounding.write_grounded(
        system="너는 디저트 업체 사장님에게 트렌드를 설명하는 분석가다. 쉬운 말로 쓴다.",
        question=(
            f"'{state['term']}' 이(가) 지금 어떤 상태인지, 왜 주목할 만한지(또는 조심할지), "
            "업체가 무엇을 준비하면 좋을지를 4~6문장으로 브리핑하라."
        ),
        evidence=evidence,
        fallback=fallback,
        required_sources={key for key in ("M1", "M2", "M8") if key in evidence},
    )
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO trend_insights (keyword_id, score_date, briefing, citations, generated_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (keyword_id, score_date) DO UPDATE SET
                briefing = EXCLUDED.briefing, citations = EXCLUDED.citations,
                generated_by = EXCLUDED.generated_by, created_at = now()
            """,
            (state["keyword_id"], state["as_of"], briefing, json.dumps(evidence, ensure_ascii=False), generated_by),
        )
    return {"insight": {"briefing": briefing, "generated_by": generated_by}}


# ───────────────────────────── 그래프 조립 ─────────────────────────────

def _has_data(state: TrendState) -> str:
    """지표가 하나도 없는 키워드는 읽기 단계에서 끝낸다."""
    return "sentiment" if state.get("as_of") else "end"


def build_graph():
    """단계들을 이어 실행 가능한 그래프로 만든다: load → sentiment → score → insight."""
    graph = StateGraph(TrendState)
    graph.add_node("load", _timed("load", load))
    graph.add_node("sentiment", _timed("sentiment", sentiment))
    graph.add_node("score", _timed("score", score))
    graph.add_node("insight", _timed("insight", insight))
    graph.set_entry_point("load")
    graph.add_conditional_edges("load", _has_data, {"sentiment": "sentiment", "end": END})
    graph.add_edge("sentiment", "score")
    graph.add_edge("score", "insight")
    graph.add_edge("insight", END)
    return graph.compile()


_graph = None


def run(keyword_id: int, force_insight: bool = False) -> TrendState:
    """키워드 하나를 분석한다.

    반환: 마지막 상태. 지표가 없던 키워드는 score 칸이 없다.
    """
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph.invoke({"keyword_id": keyword_id, "force_insight": force_insight})

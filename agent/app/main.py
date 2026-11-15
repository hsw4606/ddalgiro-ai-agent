"""딸기로 AI 에이전트의 HTTP API (FastAPI).

실행: uvicorn app.main:app --host 0.0.0.0 --port 8000
부르는 쪽: 딸기로 백엔드(ddalgiro-backend)가 관리자 화면의 요청을 받아 이 API 로 전달한다.
          브라우저가 직접 부르지 않는다. 그래서 여기서는 로그인 대신 "약속한 키" 만 확인한다.
확인 방법: /healthz·/readyz 를 뺀 모든 주소는 요청 헤더 X-AI-Key 가 설정값 AI_API_KEY 와 같아야 한다.
주소 묶음:
  /v1/trends…     트렌드 랭킹·상세·다시 분석 (안건 1 · 기획 02)
  /v1/vendors…    업체 프로필·맞춤 추천·채택 기록 (안건 1 · 기획 02 §6)
  /v1/keywords    감시 키워드 목록·추가
  /v1/recommend…  자연어 추천, 대체 공급자 찾기, 제안서 초안, 반응·납품 기록 (안건 2 · 기획 03)
  /v1/suppliers…  공급 조건 확인 표시 (안건 2)
  /v1/status      수집·분석 상태
  /v1/kpi         쌓인 기록으로 계산한 운영 지표
"""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import dates, db
from app.reco import graph as reco_graph, proposal, events
from app.settings import settings
from app.trend import pipeline, recommend


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """서버가 요청을 받기 전에 한 번 실행된다: DB 가 뜰 때까지 기다리고, DB 테이블이 없으면 만든다."""
    db.wait_for_db()
    db.apply_schema()
    yield


app = FastAPI(title="딸기로 AI 에이전트", version="0.3.0", lifespan=lifespan)


def require_key(x_ai_key: str | None = Header(default=None)) -> None:
    """요청 헤더 X-AI-Key 를 확인한다. 틀리면 401 을 돌려준다.

    설정 AI_API_KEY 가 비어 있으면 확인하지 않는다(내 컴퓨터 시험용). 서버에서는 반드시 값을 넣는다.
    compare_digest 는 글자를 끝까지 비교해, 걸린 시간으로 키를 짐작하는 공격을 막는다.
    """
    if not settings.AI_API_KEY:
        return
    if not x_ai_key or not hmac.compare_digest(x_ai_key, settings.AI_API_KEY):
        raise HTTPException(status_code=401, detail="X-AI-Key 가 없거나 틀렸습니다.")


protected = [Depends(require_key)]


# ───────────────────────────── 상태 확인 ─────────────────────────────

@app.get("/healthz")
def healthz() -> dict[str, str]:
    """프로세스가 살아 있는지만 답한다(쿠버네티스 liveness 용). DB 를 건드리지 않는다."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """DB 에 실제로 물어보고 답한다(쿠버네티스 readiness 용). DB 가 안 되면 503."""
    try:
        with db.conn() as c:
            c.execute("SELECT 1")
    except Exception as error:  # noqa: BLE001 - 어떤 DB 오류든 "아직 준비 안 됨" 이다
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {error}") from error
    return {"status": "ready"}


@app.get("/v1/whoami", dependencies=protected)
def whoami(request: Request) -> dict[str, Any]:
    """이 서버가 본 "요청을 보낸 쪽의 IP" 를 돌려준다. 접근 제한(IP 허용 목록)이 실제 IP 로 동작하는지 확인할 때 쓴다."""
    return {
        "client": request.client.host if request.client else None,
        "x_forwarded_for": request.headers.get("x-forwarded-for"),
        "x_real_ip": request.headers.get("x-real-ip"),
    }


@app.get("/v1/status", dependencies=protected)
def status() -> dict[str, Any]:
    """자료가 얼마나 쌓였는지와 최근 24시간의 단계별 실행 결과를 돌려준다."""
    with db.conn() as c:
        counts = c.execute(
            """
            SELECT (SELECT count(*) FROM keywords) AS keywords,
                   (SELECT count(*) FROM keywords WHERE is_sample) AS sample_keywords,
                   (SELECT max(metric_date) FROM daily_metrics) AS last_metric_date,
                   (SELECT max(score_date) FROM trend_scores) AS last_score_date,
                   (SELECT count(*) FROM shops WHERE is_open) AS open_shops,
                   (SELECT count(*) FROM menus) AS menus,
                   (SELECT count(*) FROM menus WHERE embedding IS NULL) AS menus_without_embedding
            """
        ).fetchone()
        runs = c.execute(
            """
            SELECT kind, node, count(*) AS total, count(*) FILTER (WHERE ok) AS succeeded,
                   round(avg(elapsed_ms))::int AS avg_ms
            FROM agent_runs WHERE created_at > now() - interval '24 hours'
            GROUP BY kind, node ORDER BY kind, node
            """
        ).fetchall()
        # 브리핑 가운데 LLM 이 쓴 글이 검사를 통과한 비율을 보기 위한 숫자
        written = c.execute("SELECT generated_by, count(*) AS n FROM trend_insights GROUP BY generated_by").fetchall()
    return {"counts": counts, "runs_24h": runs, "insights_by_writer": {row["generated_by"]: row["n"] for row in written}}


# ───────────────────────── 트렌드 (안건 1 · 기획 02) ─────────────────────────

@app.get("/v1/trends", dependencies=protected)
def list_trends(limit: int = 20, stage: str | None = None) -> list[dict[str, Any]]:
    """키워드별 최신 트렌드 점수를 점수 높은 순으로 돌려준다. stage 를 주면 그 단계만."""
    with db.conn() as c:
        return c.execute(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (s.keyword_id)
                       k.id AS keyword_id, k.term, k.category, k.is_sample,
                       s.score_date, s.score, s.stage, s.growth_rate, s.momentum,
                       s.sentiment_pos, s.n_sources, s.mentions_7d, s.search_growth, s.signal_check, s.windows
                FROM trend_scores s JOIN keywords k ON k.id = s.keyword_id
                ORDER BY s.keyword_id, s.score_date DESC
            ) latest
            WHERE %(stage)s::text IS NULL OR stage = %(stage)s::text
            ORDER BY score DESC
            LIMIT %(limit)s
            """,
            {"stage": stage, "limit": max(1, min(limit, 200))},
        ).fetchall()


@app.get("/v1/trends/{keyword_id}", dependencies=protected)
def trend_detail(keyword_id: int) -> dict[str, Any]:
    """키워드 하나의 최신 점수, 30일 추이, 브리핑(출처 포함), 최근 글을 돌려준다. 없는 키워드면 404."""
    with db.conn() as c:
        keyword = c.execute("SELECT id, term, category, is_sample FROM keywords WHERE id = %s", (keyword_id,)).fetchone()
        if keyword is None:
            raise HTTPException(status_code=404, detail="키워드가 없습니다.")
        score = c.execute(
            "SELECT * FROM trend_scores WHERE keyword_id = %s ORDER BY score_date DESC LIMIT 1", (keyword_id,)
        ).fetchone()
        series = []
        insight = None
        if score:
            # 점수 기준일까지 30일의 날짜별 언급 수와 검색 관심도
            series = c.execute(
                """
                SELECT metric_date, source, mention_count, search_ratio, is_complete
                FROM daily_metrics WHERE keyword_id = %s AND metric_date > %s AND metric_date <= %s
                ORDER BY metric_date
                """,
                (keyword_id, score["score_date"] - timedelta(days=pipeline.WINDOW_DAYS), score["score_date"]),
            ).fetchall()
            # 화면 추이도 점수 계산과 같은 규칙을 쓴다. 누락은 null, 확인된 0건은 0 으로 표시한다
            series = pipeline.metric_series(series, score["score_date"], keyword["is_sample"])
            insight = c.execute(
                "SELECT briefing, citations, generated_by, created_at FROM trend_insights WHERE keyword_id = %s AND score_date = %s",
                (keyword_id, score["score_date"]),
            ).fetchone()
        documents = c.execute(
            "SELECT id, source, title, url, posted_at, sentiment, is_ad FROM documents WHERE keyword_id = %s "
            "ORDER BY posted_at DESC NULLS LAST LIMIT 10",
            (keyword_id,),
        ).fetchall()
    return {"keyword": keyword, "score": score, "series": series, "insight": insight, "documents": documents}


@app.post("/v1/trends/{keyword_id}/analyze", dependencies=protected)
def analyze_trend(keyword_id: int) -> dict[str, Any]:
    """키워드 하나를 지금 다시 분석한다(확산 단계와 상관없이 브리핑도 만든다). LLM 을 부르므로 수십 초 걸릴 수 있다."""
    try:
        state = pipeline.run(keyword_id, force_insight=True)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if "score" not in state:
        raise HTTPException(status_code=409, detail="이 키워드에는 아직 수집된 지표가 없습니다.")
    return {"keyword_id": keyword_id, "score": state["score"], "insight": state.get("insight")}


class KeywordIn(BaseModel):
    """감시 키워드 추가 요청."""

    term: str = Field(min_length=1, max_length=50, description="검색어")
    category: str | None = Field(None, max_length=30, description="분류 (예: 빵, 케이크)")
    aliases: list[str] | None = Field(None, max_length=10, description="다른 표기 (예: Dubai chocolate). 매체 피드를 읽을 때 함께 찾는다. 보내지 않으면 그대로 둔다")


@app.get("/v1/keywords", dependencies=protected)
def list_keywords() -> list[dict[str, Any]]:
    """감시 키워드 전체 목록."""
    with db.conn() as c:
        return c.execute("SELECT id, term, category, aliases, is_sample, created_at FROM keywords ORDER BY term").fetchall()


@app.post("/v1/keywords", dependencies=protected)
def add_keyword(body: KeywordIn) -> dict[str, Any]:
    """감시 키워드를 추가한다. 이미 있으면 보낸 칸(분류·다른 표기)만 고친다. 다음 수집 작업부터 지표가 쌓인다."""
    # 빈 글자는 버리고, 다른 표기를 보내지 않았으면(None) 기존 값을 그대로 둔다
    aliases = [alias.strip() for alias in body.aliases if alias.strip()] if body.aliases is not None else None
    with db.conn() as c:
        return c.execute(
            "INSERT INTO keywords (term, category, aliases) VALUES (%s, %s, COALESCE(%s::text[], '{}')) "
            "ON CONFLICT (term) DO UPDATE SET category = COALESCE(EXCLUDED.category, keywords.category), "
            "aliases = COALESCE(%s::text[], keywords.aliases) "
            "RETURNING id, term, category, aliases",
            (body.term.strip(), body.category, aliases, aliases),
        ).fetchone()


# ───────────────────── 업체 맞춤 추천 (안건 1 · 기획 02 §6) ─────────────────────

class VendorProfileIn(BaseModel):
    """업체 프로필 저장 요청."""

    categories: list[str] = Field(default_factory=list, description="만드는 분류. 키워드의 분류와 같은 말을 쓴다")
    region: str | None = None
    note: str | None = Field(None, max_length=500)


@app.put("/v1/vendors/profile", dependencies=protected)
def save_vendor(body: VendorProfileIn, vendor_key: str = Query(alias="vendor", min_length=1, max_length=200)) -> dict[str, Any]:
    """업체 프로필을 저장한다. 업체명은 쿼리 인자 vendor 로 받아 '/' 같은 문자가 경로로 나뉘지 않게 한다."""
    with db.conn() as c:
        return c.execute(
            """
            INSERT INTO vendor_profiles (vendor_key, categories, region, note) VALUES (%s, %s, %s, %s)
            ON CONFLICT (vendor_key) DO UPDATE SET
                categories = EXCLUDED.categories, region = EXCLUDED.region, note = EXCLUDED.note, updated_at = now()
            RETURNING vendor_key, categories, region, note, updated_at
            """,
            (vendor_key, body.categories, body.region, body.note),
        ).fetchone()


@app.get("/v1/vendors/recommendations", dependencies=protected)
def vendor_recommendations(vendor_key: str = Query(alias="vendor", min_length=1, max_length=200), top_n: int = 5) -> dict[str, Any]:
    """업체에 맞는 트렌드 아이템을 순서대로 돌려준다. 프로필이 없는 업체도 결과는 나온다(적합도를 중간값으로 본다)."""
    return recommend.recommend(vendor_key, max(1, min(top_n, 20)))


class AdoptionIn(BaseModel):
    """업체 맞춤 추천에 대한 업체의 반응 기록 요청."""

    keyword_id: int
    action: Literal["adopted", "dismissed", "launched", "sold"] = Field(
        description="adopted(상품화하기로 함) / dismissed(하지 않기로 함) / launched(출시함) / sold(판매 성과)")
    quantity: int | None = Field(None, ge=0, description="sold 일 때 판매 수량(개)")
    note: str | None = Field(None, max_length=300)


@app.post("/v1/vendors/adoptions", dependencies=protected)
def save_adoption(body: AdoptionIn, vendor_key: str = Query(alias="vendor", min_length=1, max_length=200)) -> dict[str, Any]:
    """업체가 추천 아이템을 채택했는지·팔았는지를 남긴다. "추천 → 채택 → 성과" 를 다시 배우는 자료다. 없는 키워드면 404.

    그때의 점수·단계를 함께 적어 둔다. 점수는 매일 바뀌므로 나중에 보면 "어떤 추천을 보고 채택했는지" 알 수 없기 때문이다.
    """
    with db.conn() as c:
        if c.execute("SELECT 1 FROM keywords WHERE id = %s", (body.keyword_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="키워드가 없습니다.")
        latest = c.execute(
            "SELECT score_date, stage, score FROM trend_scores WHERE keyword_id = %s ORDER BY score_date DESC LIMIT 1",
            (body.keyword_id,),
        ).fetchone() or {"score_date": None, "stage": None, "score": None}
        return c.execute(
            "INSERT INTO trend_adoptions (vendor_key, keyword_id, action, score_date, stage, trend_score, quantity, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, score_date, stage, trend_score, created_at",
            (vendor_key, body.keyword_id, body.action, latest["score_date"], latest["stage"], latest["score"],
             body.quantity, body.note),
        ).fetchone()


# ───────────────────── 자연어 추천 (안건 2 · 기획 03) ─────────────────────

class RecommendIn(BaseModel):
    """자연어 추천 요청. 되물음에 답할 때는 원래 문의 뒤에 답을 이어 붙여 다시 보낸다."""

    text: str = Field(min_length=2, max_length=1000, description="문의 글")


@app.post("/v1/recommend", dependencies=protected)
def recommend_shops(body: RecommendIn) -> dict[str, Any]:
    """문의에 맞는 업체를 순위와 추천 사유와 함께 돌려준다.

    status: 'ok'(results 에 결과) / 'reask'(question 에 되물을 말) / 'empty'(조건에 맞는 업체 없음, blockers 에 막힌 조건).
    LLM 이 문의를 이해하지 못하면 502 를 돌려준다.
    """
    try:
        return reco_graph.run(body.text)
    except ValueError as error:
        raise HTTPException(status_code=502, detail=f"문의를 이해하지 못했습니다: {error}") from error


@app.post("/v1/recommend/{request_id}/reroute", dependencies=protected)
def reroute_request(request_id: int) -> dict[str, Any]:
    """공급자가 거절한 요청의 대체 공급자를 찾는다(거절·버림 기록이 있는 업체를 빼고 다시 검색).

    응답은 POST /v1/recommend 와 같은 모양이고 round(제안 회차)가 1 늘어난다. 추천 사유를 다시 쓰므로 LLM 을 부른다.
    없는 요청이면 404, 추천 결과가 있던 요청이 아니면 409.
    """
    try:
        result = reco_graph.reroute(request_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="요청 번호가 없습니다.")
    return result


@app.post("/v1/recommend/{request_id}/proposal", dependencies=protected)
def create_proposal(request_id: int) -> dict[str, Any]:
    """요청의 가장 최근 제안 옵션들을 제안서 초안 한 편으로 묶어 저장하고 돌려준다. LLM 을 부르지 않는다.

    없는 요청이면 404, 제안 옵션이 없는 요청(되묻기·결과 없음)이면 409.
    """
    try:
        created = proposal.create(request_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if created is None:
        raise HTTPException(status_code=404, detail="요청 번호가 없습니다.")
    return created


class ProposalOutcomeIn(BaseModel):
    """제안서에 대한 고객의 결정 기록 요청."""

    outcome: Literal["adopted", "not_adopted"]
    reason: str | None = Field(None, max_length=300)


@app.post("/v1/recommend/proposals/{proposal_id}/outcome", dependencies=protected)
def proposal_outcome(proposal_id: int, body: ProposalOutcomeIn) -> dict[str, Any]:
    """제안서가 채택됐는지와 그 이유를 남긴다. 다시 부르면 마지막 값으로 바뀐다. 없는 제안서 번호면 404."""
    with db.conn() as c:
        row = c.execute(
            "UPDATE proposal_logs SET outcome = %s, outcome_reason = %s, decided_at = now() WHERE id = %s "
            "RETURNING id, request_id, outcome, decided_at",
            (body.outcome, body.reason, proposal_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="제안서 번호가 없습니다.")
    return row


class FeedbackIn(BaseModel):
    """제안 옵션에 대한 반응 기록 요청."""

    quote_option_id: int
    action: Literal["selected", "rejected", "supplier_accepted", "supplier_declined", "ordered"] = Field(
        description="selected·rejected(고객의 선택·거절) / supplier_accepted·supplier_declined(공급자의 수락·거절) / ordered(주문 확정)")
    reason: str | None = Field(None, max_length=300)
    order_ref: str | None = Field(None, min_length=1, max_length=120, description="ordered 때 실제 주문 번호")
    event_key: str | None = Field(None, min_length=1, max_length=120, description="재시도에서도 같은 전송 식별자")


@app.post("/v1/recommend/feedback", dependencies=protected)
def recommend_feedback(body: FeedbackIn) -> dict[str, Any]:
    """제안 옵션 한 건에 대한 반응을 남긴다. 나중에 순위를 학습할 자료다. 없는 제안 번호면 404.

    응답의 request_id 는 그 옵션이 속한 요청 번호다. 공급자가 거절했으면 이 번호로 대체 공급자를 찾는다(reroute).
    """
    try:
        return events.record(body.quote_option_id, body.action, body.reason, body.order_ref, body.event_key)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


class DeliveryIn(BaseModel):
    """주문 뒤의 납품·CS 결과 기록 요청."""

    quote_option_id: int
    event: Literal["delivered", "delayed", "claim", "reorder_intent"] = Field(
        description="delivered(납품 완료) / delayed(지연) / claim(클레임) / reorder_intent(재주문 의향)")
    note: str | None = Field(None, max_length=300)
    order_ref: str | None = Field(None, min_length=1, max_length=120, description="확정한 실제 주문 번호")
    event_key: str | None = Field(None, min_length=1, max_length=120, description="재시도에서도 같은 전송 식별자")


@app.post("/v1/recommend/delivery", dependencies=protected)
def recommend_delivery(body: DeliveryIn) -> dict[str, Any]:
    """주문으로 이어진 옵션의 납품·클레임·재주문 의향을 남긴다. 없는 제안 번호면 404.

    "조건을 지켜 추천됐고, 공급자가 수락했고, 납품까지 끝난" 옵션이 실제로 거래 가능한 공급자의 정답 자료가 된다(quote_outcomes).
    """
    try:
        return events.record(body.quote_option_id, body.event, body.note, body.order_ref, body.event_key, delivery=True)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/v1/recommend/options/{option_id}", dependencies=protected)
def quote_evidence(option_id: int) -> dict[str, Any]:
    """추천 시점의 조건·문장·근거를 다시 읽는다. 카탈로그의 현재 값으로 과거 근거를 덮어쓰지 않는다."""
    with db.conn() as c:
        row = c.execute("SELECT id, request_id, reason, citations, snapshot, created_at FROM quote_options WHERE id=%s",
                        (option_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="제안 번호가 없습니다.")
    return row


# ───────────────────────── 공급 조건 확인 (안건 2) ─────────────────────────

class CapacityConfirmIn(BaseModel):
    """공급 조건 확인 표시 요청."""

    shop_id: int
    menu_id: int | None = Field(None, description="메뉴 하나만 표시할 때. 비우면 그 업체의 모든 메뉴")
    status: Literal["confirmed", "pending"] = Field("confirmed", description="confirmed(공급자가 확인해 줌) / pending(재확인을 요청해 둠)")
    valid_days: int | None = Field(None, ge=1, le=365, description="확인한 값을 믿을 날수. 비우면 설정 CAPACITY_VALID_DAYS")


@app.post("/v1/suppliers/capacity/confirm", dependencies=protected)
def confirm_capacity(body: CapacityConfirmIn) -> dict[str, Any]:
    """공급자에게 확인한 결과를 공급 조건에 표시한다. 등록된 공급 조건이 없으면 404.

    confirmed: 확인한 날짜를 오늘(한국 날짜)로, 유효기간을 오늘 + valid_days 로 적는다. 그 기간에는 바로 제안할 수 있다.
    pending:   상태만 바꾼다. 유효기간은 그대로라 추천에서는 계속 "공급자 재확인" 으로 표시된다.
    생산량·준비 기간 같은 값 자체를 고치는 것은 카탈로그 CSV 넣기(jobs/import_catalog.py)가 맡는다.
    """
    today = dates.today()
    valid_until = today + timedelta(days=body.valid_days or settings.CAPACITY_VALID_DAYS)
    with db.conn() as c:
        # COALESCE: pending 이면 %s 가 NULL 이라 확인 날짜·유효기간을 원래 값 그대로 둔다
        rows = c.execute(
            """
            UPDATE supplier_capacity SET
                confirmation_status = %(status)s,
                last_verified_at = COALESCE(%(verified)s::date, last_verified_at),
                valid_until = COALESCE(%(valid_until)s::date, valid_until),
                updated_at = now()
            WHERE shop_id = %(shop_id)s AND (%(menu_id)s::bigint IS NULL OR menu_id = %(menu_id)s::bigint)
            RETURNING menu_id, confirmation_status, last_verified_at, valid_until
            """,
            {"status": body.status, "shop_id": body.shop_id, "menu_id": body.menu_id,
             "verified": today if body.status == "confirmed" else None,
             "valid_until": valid_until if body.status == "confirmed" else None},
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="등록된 공급 조건이 없습니다. 카탈로그 CSV 로 먼저 넣으세요.")
    return {"shop_id": body.shop_id, "updated": rows}


# ───────────────────────────── 운영 지표 ─────────────────────────────

def _rate(part: int, whole: int) -> float | None:
    """비율을 소수 넷째 자리까지 돌려준다. 분모가 0 이면 계산할 수 없어 None."""
    return round(part / whole, 4) if whole else None


@app.get("/v1/kpi", dependencies=protected)
def kpi(days: int = 90) -> dict[str, Any]:
    """최근 days 일 동안 쌓인 기록으로 운영 지표를 계산해 돌려준다. LLM 을 부르지 않고 DB 만 읽는다.

    reco:  요청 결과 분포, 추천 사유가 근거 검사를 통과한 비율, 입점사 가산 뒤 상위 보존율, 공급 조건 확인 상태 분포,
           고객 선택·공급자 수락·주문·납품 수와 비율, 제안서 채택 수.
    trend: 브리핑을 만든 방법별 수, 업체의 채택·보류·출시 수와 채택률.
    nodes: 그래프 단계별 실행 수·성공 수·평균 시간.
    비율의 분모가 0 이면 null 이다. 기록이 적을 때의 비율은 참고만 한다.
    """
    period = {"days": max(1, min(days, 3650))}
    with db.conn() as c:
        requests = c.execute(
            "SELECT status, count(*) AS n FROM group_order_requests "
            "WHERE created_at > now() - make_interval(days => %(days)s) GROUP BY status", period).fetchall()
        preservation = c.execute(
            "SELECT avg((audit->>'preservation')::float) AS average, count(*) AS n FROM group_order_requests "
            "WHERE audit ? 'preservation' AND created_at > now() - make_interval(days => %(days)s)", period).fetchone()
        options = c.execute(
            """
            SELECT count(*) AS options,
                   count(*) FILTER (WHERE generated_by = 'llm') AS reasons_by_llm,
                   count(*) FILTER (WHERE generated_by = 'template') AS reasons_by_template,
                   count(*) FILTER (WHERE capacity_check = 'auto') AS capacity_auto,
                   count(*) FILTER (WHERE capacity_check = 'reconfirm') AS capacity_reconfirm,
                   count(*) FILTER (WHERE capacity_check = 'confirm') AS capacity_confirm
            FROM quote_options WHERE created_at > now() - make_interval(days => %(days)s)
            """, period).fetchone()
        outcomes = c.execute(
            """
            SELECT count(*) FILTER (WHERE selected) AS selected, count(*) FILTER (WHERE rejected) AS rejected,
                   count(*) FILTER (WHERE supplier_accepted) AS supplier_accepted,
                   count(*) FILTER (WHERE supplier_declined) AS supplier_declined,
                   count(*) FILTER (WHERE ordered) AS ordered, count(*) FILTER (WHERE delivered) AS delivered,
                   count(*) FILTER (WHERE claimed) AS claimed, count(*) FILTER (WHERE reorder_intent) AS reorder_intent,
                   count(*) FILTER (WHERE tradable) AS tradable,
                   count(DISTINCT request_id) FILTER (WHERE ordered) AS ordered_requests
            FROM quote_outcomes WHERE created_at > now() - make_interval(days => %(days)s)
            """, period).fetchone()
        proposals = c.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE outcome = 'adopted') AS adopted, "
            "count(*) FILTER (WHERE outcome = 'not_adopted') AS not_adopted FROM proposal_logs "
            "WHERE created_at > now() - make_interval(days => %(days)s)", period).fetchone()
        insights = c.execute(
            "SELECT generated_by, count(*) AS n FROM trend_insights "
            "WHERE created_at > now() - make_interval(days => %(days)s) GROUP BY generated_by", period).fetchall()
        adoptions = c.execute(
            "SELECT action, count(*) AS n FROM trend_adoptions "
            "WHERE created_at > now() - make_interval(days => %(days)s) GROUP BY action", period).fetchall()
        nodes = c.execute(
            """
            SELECT kind, node, count(*) AS total, count(*) FILTER (WHERE ok) AS succeeded, round(avg(elapsed_ms))::int AS avg_ms
            FROM agent_runs WHERE created_at > now() - make_interval(days => %(days)s)
            GROUP BY kind, node ORDER BY kind, node
            """, period).fetchall()
    by_status = {row["status"]: row["n"] for row in requests}
    by_action = {row["action"]: row["n"] for row in adoptions}
    return {
        "period_days": period["days"],
        "reco": {
            "requests": by_status,
            "options": options["options"],
            # 추천 사유가 근거 검사를 통과해 모델의 글 그대로 나간 비율
            "reason_llm_rate": _rate(options["reasons_by_llm"], options["reasons_by_llm"] + options["reasons_by_template"]),
            # 입점사 가산 뒤에도 가산 전 상위 결과가 남은 비율의 평균(목표 0.95 이상)
            "preservation_avg": round(preservation["average"], 4) if preservation["average"] is not None else None,
            "preservation_requests": preservation["n"],
            "capacity_check": {"auto": options["capacity_auto"], "reconfirm": options["capacity_reconfirm"],
                               "confirm": options["capacity_confirm"]},
            "outcomes": outcomes,
            "supplier_accept_rate": _rate(outcomes["supplier_accepted"], outcomes["supplier_accepted"] + outcomes["supplier_declined"]),
            # 결과가 나간 요청 가운데 주문으로 이어진 요청의 비율
            "request_to_order_rate": _rate(outcomes["ordered_requests"], by_status.get("ok", 0)),
            "proposals": proposals,
        },
        "trend": {
            "insights_by_writer": {row["generated_by"]: row["n"] for row in insights},
            "adoptions": by_action,
            # 채택하기로 한 수 ÷ (채택 + 하지 않기로 한 수)
            "adoption_rate": _rate(by_action.get("adopted", 0), by_action.get("adopted", 0) + by_action.get("dismissed", 0)),
        },
        "nodes": nodes,
    }

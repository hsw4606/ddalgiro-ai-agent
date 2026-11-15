"""운영 지표(Prometheus)와 AI 처리 추적(Langfuse)을 모은다.

API 의 /metrics/ 는 숫자만 내주고, span() 은 처리 단계·모델·토큰 수만 보낸다.
문의·근거·응답 원문과 예외 메시지는 보내지 않는다. 관측 실패는 원래 요청을 중단하지 않는다.
Langfuse 는 LANGFUSE_ENABLED 를 켜고 별도 서버 주소와 키를 넣었을 때만 연결한다.
"""
from __future__ import annotations

import atexit
import re
import threading
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Iterator

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, make_asgi_app

from app.settings import settings

RELEASE = "0.4.0"
REGISTRY = CollectorRegistry()
# 라벨에는 정해진 경로·단계 이름만 넣는다. 고객 글이나 번호마다 새 시계열을 만들지 않는다.
HTTP_REQUESTS = Counter("ddalgiro_http_requests_total", "HTTP 요청 수", ["method", "route", "status"], registry=REGISTRY)
HTTP_SECONDS = Histogram("ddalgiro_http_request_duration_seconds", "HTTP 전체 처리 시간", ["method", "route"],
                         buckets=(.1, .5, 1, 2, 5, 10, 30, 45, 55, 120, 300), registry=REGISTRY)
STEPS = Counter("ddalgiro_steps_total", "AI 단계 실행 수", ["stage", "outcome"], registry=REGISTRY)
STEP_SECONDS = Histogram("ddalgiro_step_duration_seconds", "AI 단계 처리 시간", ["stage"],
                         buckets=(.01, .1, .5, 1, 5, 10, 30, 60, 120, 300), registry=REGISTRY)
LLM_CALLS = Counter("ddalgiro_llm_calls_total", "클라이언트 모델 호출 수", ["alias", "method", "outcome"], registry=REGISTRY)
TOKENS = Counter("ddalgiro_llm_tokens_total", "응답에서 확인한 토큰 수", ["alias", "direction"], registry=REGISTRY)
OBS_ERRORS = Counter("ddalgiro_observability_errors_total", "관측 자체의 오류", ["operation"], registry=REGISTRY)
TRACE_ENABLED = Gauge("ddalgiro_langfuse_enabled", "추적 설정 활성화 여부", registry=REGISTRY)
TRACE_ENABLED.set_function(lambda: int(settings.LANGFUSE_ENABLED))
TRACE_READY = Gauge("ddalgiro_langfuse_initialized", "추적 SDK 초기화 성공 여부", registry=REGISTRY)

_client: Any = None
_initialized = False
_lock = threading.Lock()
_META_KEYS = {"schema", "attempt", "request_id", "round", "keyword_id", "result_status", "results_count", "generated_by"}


def _mask_spans(*, params):
    """SDK 가 내보내기 직전에 허용한 숫자·식별값 이외의 속성을 지운다.

    나중에 다른 자동 계측을 붙여도 본문·헤더·오류 원문이 뜻하지 않게 전송되지 않게 한다.
    """
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    allowed = {
        "langfuse.observation.type", "langfuse.observation.level", "langfuse.observation.status_message",
        "langfuse.observation.model.name", "langfuse.observation.usage_details",
        "langfuse.observation.cost_details",
        "langfuse.environment", "langfuse.release", "langfuse.version", "langfuse.trace.name",
    }
    allowed.update("langfuse.observation.metadata." + key for key in _META_KEYS)
    patches = {key: OtelSpanPatch(delete_attributes=tuple(k for k in span.attributes if k not in allowed))
               for key, span in params.spans.items()}
    return MaskOtelSpansResult(span_patches=patches)


def _get_client():
    """설정이 켜졌을 때 SDK 를 한 번 만든다. 초기화 실패는 숫자로 남기고 추적만 끈다."""
    global _client, _initialized
    if not settings.LANGFUSE_ENABLED:
        return None
    with _lock:
        if not _initialized:
            _initialized = True
            try:
                if not all((settings.LANGFUSE_BASE_URL, settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY)):
                    raise ValueError("Langfuse 설정 누락")
                from langfuse import Langfuse

                _client = Langfuse(
                    base_url=settings.LANGFUSE_BASE_URL, public_key=settings.LANGFUSE_PUBLIC_KEY,
                    secret_key=settings.LANGFUSE_SECRET_KEY, release=RELEASE, environment="production",
                    timeout=3, mask_otel_spans=_mask_spans,
                    should_export_span=lambda s: s.name.startswith("ddalgiro."),
                )
                TRACE_READY.set(1)
                # 정기 작업은 API 의 종료 훅을 쓰지 않으므로 프로세스 종료 때 남은 추적을 보낸다.
                atexit.register(flush)
            except Exception:
                TRACE_READY.set(0)
                OBS_ERRORS.labels("initialize").inc()
    return _client


def initialize() -> None:
    """API 기동 때 설정을 점검한다. 인증·실제 저장 확인은 별도 --check 명령으로 한다."""
    _get_client()


def update(observation, **values) -> None:
    """내부에서 고른 지표만 SDK 에 반영한다. 실패해도 본래 응답은 유지한다."""
    if observation is not None:
        try:
            observation.update(**values)
        except Exception:
            OBS_ERRORS.labels("update").inc()


@contextmanager
def span(name: str, *, kind: str = "span", model: str | None = None, metadata: dict | None = None) -> Iterator[Any]:
    """호출 묶음·단계를 같은 추적으로 잇는다. 입력/출력 원문은 인자로 받지 않는다.

    예외는 원래 호출자에게 전달하되 SDK 에는 예외 종류만 남긴다.
    SDK 의 종료 함수에 예외 객체를 넘기면 메시지·스택이 자동 수집될 수 있어 정상 종료 인자로 닫는다.
    """
    context, observation = None, None
    client = _get_client()
    if client is not None:
        try:
            safe_meta = {k: v for k, v in (metadata or {}).items() if k in _META_KEYS and isinstance(v, (str, int, bool))}
            context = client.start_as_current_observation(name="ddalgiro." + name, as_type=kind, model=model, metadata=safe_meta)
            observation = context.__enter__()
        except Exception:
            context = None
            OBS_ERRORS.labels("start").inc()
    try:
        yield observation
    except BaseException as error:
        update(observation, level="ERROR", status_message=type(error).__name__)
        raise
    finally:
        if context is not None:
            try:
                context.__exit__(None, None, None)
            except Exception:
                OBS_ERRORS.labels("end").inc()


def record_usage(observation, alias: str, message) -> None:
    """사용량 정보가 없거나 형식이 달라도 모델의 정상 응답을 유지한다."""
    try:
        _record_usage(observation, alias, message)
    except Exception:
        OBS_ERRORS.labels("usage").inc()


def _record_usage(observation, alias: str, message) -> None:
    """모델 응답의 부가 정보에서 토큰 수만 읽는다. 본문은 기록하지 않는다."""
    usage = getattr(message, "usage_metadata", None) or {}
    details = {}
    for direction in ("input", "output"):
        value = usage.get(direction + "_tokens")
        if isinstance(value, int) and value >= 0:
            details[direction] = value
            TOKENS.labels(alias, direction).inc(value)
    # 실제 모델 ID 가 있어야 Langfuse 의 단가표와 연결된다. 별칭만 알면 요금을 추측하지 않는다.
    actual = (getattr(message, "response_metadata", None) or {}).get("model_name", "")
    values: dict[str, Any] = {"usage_details": details}
    if isinstance(actual, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,150}", actual):
        values["model"] = actual
    update(observation, **values)


@contextmanager
def step(name: str):
    """단계의 시간·성공 수를 세고 같은 Langfuse 추적 안에 넣는다."""
    started, outcome = time.monotonic(), "ok"
    with span(name):
        try:
            yield
        except BaseException:
            outcome = "error"
            raise
        finally:
            STEPS.labels(name, outcome).inc()
            STEP_SECONDS.labels(name).observe(time.monotonic() - started)


@contextmanager
def generation(alias: str, method: str, schema: str = "", *, kind: str = "generation"):
    """양식 강제·대체 요청을 따로 센다. SDK·게이트웨이 내부 HTTP 재시도 수와는 다르다."""
    outcome = "ok"
    with span("llm." + method, kind=kind, model=alias, metadata={"schema": schema}) as observation:
        try:
            yield observation
        except BaseException:
            outcome = "error"
            raise
        finally:
            LLM_CALLS.labels(alias, method, outcome).inc()


def traced(name: str):
    """추천 한 건·키워드 분석 한 건을 묶는다. 함수의 입력·출력을 자동 수집하지 않는다."""
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with span(name, kind="agent") as observation:
                result = function(*args, **kwargs)
                if isinstance(result, dict):
                    metadata = {key: result[key] for key in ("request_id", "keyword_id", "round")
                                if type(result.get(key)) is int}
                    if isinstance(result.get("status"), str) and result["status"] in {"ok", "empty", "reask"}:
                        metadata["result_status"] = result["status"]
                    update(observation, metadata=metadata)
                return result
        return run
    return decorate


def flush() -> None:
    """API·정기 작업이 끝날 때 SDK 의 남은 기록을 보낸다. 관측 서버 장애는 삼킨다."""
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            OBS_ERRORS.labels("flush").inc()


class MetricsMiddleware:
    """FastAPI 요청의 횟수와 시간을 센다. 숫자·문의가 섞인 실제 URL 은 라벨에 넣지 않는다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(("/metrics", "/healthz", "/readyz")):
            return await self.app(scope, receive, send)
        started, status = time.monotonic(), 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = getattr(scope.get("route"), "path", "__unmatched__")
            method = scope["method"] if scope["method"] in {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"} else "OTHER"
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_SECONDS.labels(method, route).observe(time.monotonic() - started)


metrics_app = make_asgi_app(registry=REGISTRY)


def check_connection() -> None:
    """모델을 부르지 않고 별도 서버의 인증·추적 저장·원문 비수집을 확인한다.

    실행: 서버 파드에서 python -m app.observability --check
    자체 설치한 Langfuse v4 의 실시간 관측 조회 API 를 쓴다.
    """
    client = _get_client()
    if client is None or not client.auth_check():
        raise RuntimeError("추적 활성화 설정·주소·프로젝트 키를 확인하세요")
    with span("installation.check"):
        trace_id = client.get_current_trace_id()
        with span("installation.child"):
            pass
    client.flush()
    if not trace_id:
        raise RuntimeError("추적 ID가 없습니다. SDK의 추적 비활성화 설정을 확인하세요")
    for _ in range(30):
        rows = client.api.observations.get_many(trace_id=trace_id, fields="core,basic,io", limit=10).data
        if len(rows) >= 2:
            assert all(row.input in (None, "") and row.output in (None, "") for row in rows)
            print(f"인증·저장·원문 비수집 확인: 추적 {trace_id}, 관측 {len(rows)}개")
            return
        time.sleep(1)
    raise RuntimeError("추적을 저장하지 못했습니다. worker·Redis·ClickHouse·객체 저장소 로그를 확인하세요")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Langfuse 설정과 실제 추적 저장 확인(모델 요금 없음)")
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    check_connection()

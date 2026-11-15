"""관측이 업무 응답을 망치거나 원문을 외부로 보내지 않는지 검증한다.

실행: python -m pytest tests/test_observability.py -q
실제 Langfuse SDK 의 메모리 수신기를 사용한다. 외부 서버·모델·메일에는 연결하지 않는다.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
import pytest

from app import observability as obs, llm
from app.reco import explain
from app.reco.slots import Slots
from app.settings import settings


@pytest.fixture
def sdk(monkeypatch):
    """실제 SDK 의 내보내기 직전 필터까지 실행하되 HTTP 대신 메모리에 받는다."""
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key="pk-lf-" + str(uuid4()), secret_key="test-only",
                      base_url="http://127.0.0.1:1", span_exporter=exporter,
                      mask_otel_spans=obs._mask_spans, should_export_span=lambda s: s.name.startswith("ddalgiro."))
    monkeypatch.setattr(obs, "_get_client", lambda: client)
    yield client, exporter
    client.shutdown()


def test_request_labels_exclude_raw_path_query_body():
    """번호·개인정보가 달라도 경로 틀 하나로 집계하고, 원문은 지표에 나타나지 않는다."""
    app = FastAPI()
    app.add_middleware(obs.MetricsMiddleware)
    app.mount("/metrics", obs.metrics_app)

    @app.post("/v1/test-items/{item_id}")
    def item(item_id: str):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/v1/test-items/SECRET-CUSTOMER?phone=SECRET-PHONE", json={"text": "SECRET-BODY"}).status_code == 200
        assert client.get("/v1/SECRET-MISSING").status_code == 404
        metrics = client.get("/metrics/").text
    assert 'route="/v1/test-items/{item_id}"' in metrics
    assert 'route="__unmatched__"' in metrics
    assert "SECRET-" not in metrics


def test_failed_request_is_counted_without_error_message():
    """처리 중 예외가 나도 500 횟수를 세며 오류 원문을 라벨에 넣지 않는다."""
    app = FastAPI()
    app.add_middleware(obs.MetricsMiddleware)

    @app.get("/v1/test-error")
    def fail():
        raise RuntimeError("SECRET-EXCEPTION")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/v1/test-error").status_code == 500
    assert obs.HTTP_REQUESTS.labels("GET", "/v1/test-error", "500")._value.get() >= 1


def test_disabled_tracing_never_initializes_client(monkeypatch):
    """기본 설정에서는 SDK 를 만들지도, 외부로 연결하지도 않는다."""
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False)
    monkeypatch.setattr(obs, "_initialized", False)
    with obs.span("disabled") as observation:
        assert observation is None
    assert obs._initialized is False


def test_invalid_usage_metadata_does_not_raise():
    """제공자의 사용량 정보가 예상 형식과 달라도 업무 결과를 실패로 바꾸지 않는다."""
    before = obs.OBS_ERRORS.labels("usage")._value.get()
    obs.record_usage(None, "chat-fast", SimpleNamespace(usage_metadata="invalid", response_metadata={}))
    assert obs.OBS_ERRORS.labels("usage")._value.get() == before + 1


def test_embedding_without_usage_still_returns_vectors(monkeypatch):
    """사용량 정보를 생략하는 임베딩 서버도 벡터 결과는 그대로 받는다."""
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False)
    expected = [0.1] * settings.EMBED_DIM
    response = SimpleNamespace(data=[SimpleNamespace(embedding=expected)])
    monkeypatch.setattr(llm, "OpenAI", lambda **kwargs: SimpleNamespace(
        embeddings=SimpleNamespace(create=lambda **options: response)))
    assert llm.embed(["test only"]) == [expected]


def test_initialization_failure_preserves_business_result(monkeypatch):
    """주소·키 설정 오류가 있어도 AI 작업은 계속하며 관측 오류만 센다."""
    import langfuse

    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(settings, "LANGFUSE_BASE_URL", "https://test.invalid")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "test")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "test")
    monkeypatch.setattr(obs, "_initialized", False)
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.setattr(langfuse, "Langfuse", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("SECRET-KEY")))
    before = obs.OBS_ERRORS.labels("initialize")._value.get()
    with obs.span("business"):
        value = 42
    assert value == 42
    assert obs.OBS_ERRORS.labels("initialize")._value.get() == before + 1


def test_actual_sdk_export_removes_sensitive_attributes_and_errors(sdk):
    """원문을 실수로 SDK 에 넣어도 내보내기 필터가 지우며, 예외 메시지도 자동 수집하지 않는다."""
    client, exporter = sdk
    with pytest.raises(ValueError, match="SECRET-EXCEPTION"):
        with obs.span("root"):
            with obs.span("llm.test", kind="generation") as observation:
                observation.update(input="SECRET-INPUT", output="SECRET-OUTPUT",
                                   metadata={"raw_text": "SECRET-METADATA", "request_id": 17})
                raise ValueError("SECRET-EXCEPTION")
    client.flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    serialized = json.dumps([{ "attributes": dict(s.attributes), "events": [str(e) for e in s.events]} for s in spans])
    assert "SECRET-" not in serialized
    assert "ValueError" in serialized
    assert "request_id" in serialized
    assert spans[0].parent.span_id == spans[1].context.span_id


def test_parallel_reasons_keep_parent_trace(sdk, monkeypatch):
    """추천 사유를 만드는 병렬 스레드도 같은 부모 요청의 추적을 유지한다."""
    client, exporter = sdk

    def fake_reason(*args):
        with obs.span("parallel-reason"):
            return {"reason": "stub"}

    monkeypatch.setattr(explain, "explain_one", fake_reason)
    with obs.span("request"):
        results = explain.explain_all([{}, {}, {}], Slots())
    client.flush()
    spans = exporter.get_finished_spans()
    assert len(results) == 3 and len(spans) == 4
    assert len({s.context.trace_id for s in spans}) == 1
    root = next(s for s in spans if s.name == "ddalgiro.request")
    assert all(s.parent.span_id == root.context.span_id for s in spans if s is not root)


@pytest.mark.parametrize("fallback", [False, True])
def test_model_usage_and_fallback_are_traced_without_original_text(sdk, monkeypatch, fallback):
    """양식 강제 성공·대체 요청을 구분해 세고 원문 대신 토큰·모델만 보낸다."""
    client, exporter = sdk

    class Answer(BaseModel):
        ok: bool

    raw = SimpleNamespace(content='{"ok":true}', usage_metadata={"input_tokens": 13, "output_tokens": 7},
                          response_metadata={"model_name": "claude-haiku-5-5"})

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return SimpleNamespace(invoke=lambda messages: {"raw": raw,
                "parsed": None if fallback else Answer(ok=True), "parsing_error": ValueError() if fallback else None})

        def invoke(self, messages):
            return raw

    monkeypatch.setattr(llm, "chat_model", lambda smart: Model())
    with obs.span("request"):
        assert llm.ask_structured(Answer, "SECRET-SYSTEM", "SECRET-USER").ok
    client.flush()
    generations = [s for s in exporter.get_finished_spans() if s.name.startswith("ddalgiro.llm.")]
    assert len(generations) == (2 if fallback else 1)
    serialized = json.dumps([dict(s.attributes) for s in generations])
    assert "SECRET-" not in serialized
    assert "claude-haiku-5-5" in serialized
    assert '"input": 13' in serialized.replace('\\"', '"')

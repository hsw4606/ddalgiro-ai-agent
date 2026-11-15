"""LLM 게이트웨이(LiteLLM) 호출 모음 — 글 쓰기, 구조화 출력, 임베딩.

쓰는 곳: trend/ 와 reco/ 의 LLM 단계, jobs/ 의 임베딩 작업.
원칙:   에이전트는 모델(지금은 Claude API)을 직접 부르지 않는다. "이름표"(chat-fast 등)만 붙여 게이트웨이에 보내고,
        이름표가 어느 모델로 가는지는 게이트웨이 설정(k8s/22-litellm.yaml)이 정한다. 그래서 모델을 바꿔도 이 코드는 그대로다.
규격:   게이트웨이는 OpenAI 호환 API 를 쓴다. 그래서 OpenAI 용 클라이언트에 주소만 바꿔서 쓴다.
        게이트웨이가 이 요청을 Claude API 형식으로 바꿔 보내고, 답을 다시 OpenAI 형식으로 돌려준다.
비용:   대화 호출(chat_model·ask_structured)은 Claude API 요금이 든다. 임베딩(embed)은 서버 안에서 돌아 요금이 없다.
"""
from __future__ import annotations

import json
import re
from typing import TypeVar

from langchain_openai import ChatOpenAI
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app.settings import settings

T = TypeVar("T", bound=BaseModel)


def _base_url() -> str:
    """게이트웨이의 OpenAI 호환 주소(…/v1)를 만든다."""
    return settings.LITELLM_BASE_URL.rstrip("/") + "/v1"


def _extra_body() -> dict | None:
    """설정에 추가 옵션(JSON 글자)이 있으면 사전으로 바꿔 돌려준다. 없으면 None."""
    return json.loads(settings.LLM_EXTRA_BODY) if settings.LLM_EXTRA_BODY.strip() else None


def chat_model(smart: bool = False) -> ChatOpenAI:
    """대화 모델 객체를 만든다.

    smart=False: 빠른 이름표(분류·조건 뽑기). smart=True: 글을 쓰는 이름표(브리핑·추천 사유).
    temperature=0 은 같은 입력에 되도록 같은 답이 나오게 하는 설정이다(검증·재현에 유리).
    Claude 의 최신 모델은 이 옵션을 받지 않으므로 게이트웨이가 빼고 보낸다(22-litellm.yaml 의 drop_params).
    직접 서빙하는 모델(추후 도입하는 DGX Spark)로 바꾸면 그대로 전달된다.
    """
    return ChatOpenAI(
        base_url=_base_url(),
        api_key=settings.LITELLM_API_KEY,
        model=settings.MODEL_CHAT_SMART if smart else settings.MODEL_CHAT_FAST,
        temperature=0,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=1,
        extra_body=_extra_body(),
    )


def _extract_json(text: str) -> str:
    """모델의 답에서 JSON 부분만 잘라낸다.

    모델이 JSON 앞뒤에 생각 과정(<think>…</think>)이나 코드 울타리(```json)를 붙이는 경우가 있어
    그것을 걷어 내고 첫 '{' 부터 마지막 '}' 까지를 돌려준다. '{' 가 없으면 원문을 그대로 돌려준다.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def ask_structured(schema: type[T], system: str, user: str, smart: bool = False) -> T:
    """LLM 의 답을 정해진 양식(schema)의 객체로 받는다.

    입력: schema 는 pydantic 모델 클래스, system 은 역할·규칙, user 는 실제 질문.
    반환: schema 의 인스턴스.
    방법: 1) 모델의 "JSON 양식 강제" 기능으로 요청한다(양식을 벗어난 답이 나올 수 없다).
             Claude API 에서는 게이트웨이가 이것을 구조화 출력(structured outputs) 요청으로 바꿔 보낸다.
          2) 모델이나 게이트웨이가 그 기능을 지원하지 않아 실패하면, 양식을 글로 설명해 요청하고 답에서 JSON 을 잘라 검사한다.
    실패: 두 방법 모두 양식에 맞는 답을 얻지 못하면 예외를 올린다. 부르는 쪽이 대체 처리를 한다.
    """
    messages = [("system", system), ("human", user)]
    model = chat_model(smart)
    try:
        return model.with_structured_output(schema, method="json_schema").invoke(messages)
    except Exception:  # noqa: BLE001 - 모델·게이트웨이마다 실패 모양이 달라 넓게 잡고 2번 방법으로 넘어간다
        pass

    # 2번 방법: 양식(JSON Schema)을 지시문에 넣고, 답에서 JSON 만 잘라 pydantic 으로 검사한다
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    fallback_system = (
        f"{system}\n\n반드시 아래 JSON Schema 에 맞는 JSON 객체 하나만 출력하라. 설명·코드 울타리는 쓰지 마라.\n{schema_text}"
    )
    answer = model.invoke([("system", fallback_system), ("human", user)]).content
    try:
        return schema.model_validate_json(_extract_json(str(answer)))
    except ValidationError as error:
        raise ValueError(f"LLM 이 양식에 맞는 답을 내지 못했습니다: {error}") from error


def embed(texts: list[str]) -> list[list[float]]:
    """글 목록을 벡터 목록으로 바꾼다(입력과 같은 순서).

    반환: 글마다 길이 EMBED_DIM 의 숫자 목록.
    실패: 벡터 길이가 설정과 다르면 예외를 올린다 — DB 칸 크기와 맞지 않아 저장이 깨지기 때문이다.
    """
    if not texts:
        return []
    client = OpenAI(base_url=_base_url(), api_key=settings.LITELLM_API_KEY, timeout=settings.LLM_TIMEOUT_SECONDS)
    response = client.embeddings.create(model=settings.MODEL_EMBED, input=texts)
    vectors = [item.embedding for item in response.data]
    if vectors and len(vectors[0]) != settings.EMBED_DIM:
        raise ValueError(f"임베딩 길이 {len(vectors[0])} 가 설정 EMBED_DIM={settings.EMBED_DIM} 과 다릅니다.")
    return vectors

"""AI 가 쓴 글이 "주어진 근거 안에서만" 쓰였는지 코드로 검사한다 (환각 통제).

쓰는 곳: 트렌드 브리핑(trend/pipeline.py)과 추천 사유(reco/explain.py). 두 기획의 공통 원칙이다.
원칙 세 가지:
  1) 숫자·사실은 코드와 DB 가 정하고, LLM 은 그것을 문장으로 옮기기만 한다.
  2) LLM 에게는 번호가 붙은 근거 목록만 주고, 문장마다 어느 근거를 썼는지 번호를 적게 한다.
  3) 근거 문장을 그대로 선택한 경우만 허용한다. 숫자·속성·부정을 바꾸는 자유 서술은 내보내지 않는다.
     (다시 한 번 쓰게 하고, 그래도 어기면 코드가 근거만으로 만든 글로 바꾼다)
"""
from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel, Field

from app import llm

# 근거 한 건: {"text": 근거 내용, "url": 원문 주소 또는 None}
Evidence = dict[str, dict[str, str | None]]

# 글 속의 숫자를 찾는 식. 1,240 / 25.3 / 80 같은 모양을 한 덩어리로 잡는다.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class Sentence(BaseModel):
    """출처가 붙은 문장 하나."""

    text: str = Field(description="한 문장. 근거에 있는 사실만 쓴다.")
    sources: list[str] = Field(description="이 문장이 그대로 선택한 근거 번호 하나. 예: ['M1']")


class GroundedText(BaseModel):
    """출처가 붙은 문장들의 목록. LLM 이 이 양식으로 답한다."""

    sentences: list[Sentence]


def _numbers(text: str) -> set[str]:
    """글에서 숫자를 뽑아 비교하기 쉬운 모양으로 맞춘다. 예: '1,240.0' → '1240'."""
    found = set()
    for raw in _NUMBER.findall(text):
        number = raw.replace(",", "")
        # 소수점 아래가 0 뿐이면 떼어 '25.0' 과 '25' 를 같은 숫자로 본다
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        found.add(number)
    return found


def check(answer: GroundedText, evidence: Evidence) -> list[str]:
    """글을 검사해 문제 목록을 돌려준다. 빈 목록이면 통과다.

    검사: ① 문장이 하나 이상인가 ② 문장마다 존재하는 출처 하나가 있는가
          ③ 선택한 근거와 문장이 같은가(줄바꿈·연속 공백만 정리한다).
    숫자 집합만 비교하면 가격과 생산량의 숫자를 바꾸거나 '불가'를 '가능'으로 바꿔도 통과한다.
    검증된 한국어 근거 문장을 선택하게 제한해 속성·값·단위와 부정 표현을 함께 보존한다.
    """
    if not answer.sentences:
        return ["문장이 없음"]
    problems: list[str] = []
    for index, sentence in enumerate(answer.sentences, start=1):
        if not sentence.sources:
            problems.append(f"{index}번 문장에 출처가 없음")
            continue
        unknown = [source for source in sentence.sources if source not in evidence]
        if unknown:
            problems.append(f"{index}번 문장이 없는 출처 {unknown} 를 가리킴")
            continue
        if len(sentence.sources) != 1 or " ".join(sentence.text.split()) != " ".join(str(evidence[sentence.sources[0]]["text"]).split()):
            problems.append(f"{index}번 문장이 선택한 근거 원문과 다름")
        # 문장이 가리킨 근거들의 숫자를 모아, 문장 속 숫자가 모두 그 안에 있는지 본다
        allowed: set[str] = set()
        for source in sentence.sources:
            allowed |= _numbers(str(evidence[source]["text"]))
        invented = _numbers(sentence.text) - allowed
        if invented:
            problems.append(f"{index}번 문장에 근거에 없는 숫자 {sorted(invented)}")
    return problems


def render(answer: GroundedText) -> str:
    """검사를 통과한 글을 '문장 [출처:M1,D3]' 형식의 여러 줄 글로 만든다."""
    return "\n".join(f"{s.text.strip()} [출처:{','.join(s.sources)}]" for s in answer.sentences)


def evidence_block(evidence: Evidence) -> str:
    """근거 목록을 LLM 에게 보여 줄 글로 만든다. 한 줄에 '[번호] 내용'."""
    return "\n".join(f"[{key}] {item['text']}" for key, item in evidence.items())


def write_grounded(
    system: str,
    question: str,
    evidence: Evidence,
    fallback: Callable[[], GroundedText],
    required_sources: set[str] | None = None,
) -> tuple[str, str]:
    """근거 안에서만 글을 쓰게 하고, 검사를 통과한 글만 돌려준다.

    입력: system 은 역할 지시, question 은 무엇을 쓸지, evidence 는 번호 붙은 근거,
          fallback 은 LLM 글을 쓸 수 없을 때 코드가 근거만으로 글을 만드는 함수.
    반환: (글, 만든 방법). 만든 방법은 'llm'(모델이 쓰고 검사 통과) 또는 'template'(코드가 만든 글).
    흐름: 쓰기 → 검사 → 문제가 있으면 문제를 알려 주고 한 번 더 쓰기 → 그래도 문제면 fallback.
    """
    rules = (
        "아래 근거 중 질문에 필요한 문장을 선택하고 순서를 정하라. "
        "각 text는 근거 문장 전체를 글자 그대로 복사하고 sources에는 그 근거 번호 하나만 넣어라. "
        "바꿔 쓰기·문장 합치기·요약·추측을 하지 마라."
    )
    user = f"{question}\n\n근거:\n{evidence_block(evidence)}"
    problems: list[str] = []
    for _attempt in range(2):
        hint = f"\n\n앞선 답의 문제: {'; '.join(problems)}. 이 문제를 고쳐 다시 써라." if problems else ""
        try:
            answer = llm.ask_structured(GroundedText, f"{system}\n{rules}", user + hint, smart=True)
        except Exception:  # noqa: BLE001 - LLM 장애·양식 실패는 모두 대체 글로 넘긴다
            break
        problems = check(answer, evidence)
        if not problems:
            # 가격·견적·공급자 재확인 등 필수 안내는 모델이 선택하지 않아도 코드가 반드시 넣는다.
            used = {source for sentence in answer.sentences for source in sentence.sources}
            for source in sorted(required_sources or set()):
                if source not in used:
                    answer.sentences.append(Sentence(text=str(evidence[source]["text"]), sources=[source]))
            return render(answer), "llm"
    answer = fallback()
    used = {source for sentence in answer.sentences for source in sentence.sources}
    for source in sorted(required_sources or set()):
        if source not in used:
            answer.sentences.append(Sentence(text=str(evidence[source]["text"]), sources=[source]))
    # 코드가 만든 대체 문장도 같은 검사를 거친다. 근거가 깨졌으면 잘못된 글을 내보내지 않는다.
    problems = check(answer, evidence)
    if problems:
        raise ValueError("대체 문장의 근거 검증 실패: " + "; ".join(problems))
    return render(answer), "template"

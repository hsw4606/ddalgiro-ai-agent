"""수집·추천·샘플 자료가 공통으로 쓰는 한국 업무 날짜.

컨테이너나 개발 컴퓨터의 시간대와 관계없이 날짜가 바뀌는 기준을 Asia/Seoul 로 맞춘다.
서버 운영체제의 시간대를 바꾸는 부작용은 없으며, 날짜가 필요한 모듈이 today() 를 부른다.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


def today(now: datetime | None = None) -> date:
    """한국 날짜를 반환한다. now 는 시험에서 지정할 수 있는 시간대가 포함된 시각이다.

    now 를 생략하면 현재 시각을 쓴다. 시간대 없는 시각은 컴퓨터 설정에 따라 뜻이 달라져 ValueError 로 거절한다.
    """
    instant = now if now is not None else datetime.now(SEOUL)
    if instant.utcoffset() is None:
        raise ValueError("업무 날짜 계산에는 시간대가 포함된 시각이 필요합니다.")
    return instant.astimezone(SEOUL).date()

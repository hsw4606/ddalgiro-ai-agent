"""수집 출처 목록 — 어떤 출처가 있고, 어느 신호 계열에 속하며, 점수 계산에 어떻게 쓰이는지를 한곳에 적는다 (안건 1).

쓰는 곳: trend/signals.py(점수에 쓸 출처 고르기, 출처별·계열별 증가율), 수집 작업(jobs/collect_*.py)이 DB 에 적는 출처 이름.
계열(family): 같은 종류의 신호끼리 묶은 것이다. 글(블로그·뉴스·RSS) / 검색(데이터랩) / SNS(인스타그램).
            서로 다른 계열이 함께 오르는지를 보는 것이 교차검증이다(scoring.agreement).
계산 출처: 최근 10일 연속 수집 완료된 출처를 고정하고, 두 계열 이상일 때 판정한다.
            각 출처를 평균 100으로 정규화한 뒤 계열별로 평균한다(signals.py).
            네이버가 없어도 RSS(글)+인스타그램(SNS)이 충분하면 판정한다. 원래 단위와 계열은 보존한다.
core 표시는 기본 설치의 수집 출처 안내이며 점수 계산의 필수 조건이 아니다.
출처를 더할 때: 여기에 한 줄을 더하고, 수집 작업이 같은 이름으로 daily_metrics 에 넣으면 된다. pipeline.py 는 고치지 않는다.
            계열은 글·검색·SNS 가운데 하나여야 한다. 새 계열을 만들면 signals.py 의 계열 순서와 읽는 칸도 고쳐야 판정에 쓰인다.
"""
from __future__ import annotations

from dataclasses import dataclass

TEXT, SEARCH, SOCIAL = "글", "검색", "SNS"
SAMPLE = "sample"      # 연습용 샘플 자료의 출처 이름. 글 수와 검색 관심도를 한 줄에 함께 담는다
RSS_PREFIX = "rss:"    # RSS·웹 피드는 피드마다 "rss:매체이름" 으로 적는다. 모두 글 계열이다


@dataclass(frozen=True)
class Source:
    """출처 하나."""

    name: str     # daily_metrics.source 에 적는 이름
    family: str   # 신호 계열: TEXT / SEARCH / SOCIAL
    core: bool    # 기본 설치에서 활성화하는 출처 안내
    label: str    # 화면·문서에 보여 줄 이름
    method: str   # 받는 방법(약관 확인의 기준)


SOURCES: tuple[Source, ...] = (
    Source("naver_blog", TEXT, True, "네이버 블로그", "공식 검색 API"),
    Source("naver_news", TEXT, True, "네이버 뉴스", "공식 검색 API"),
    Source("naver_datalab", SEARCH, True, "네이버 데이터랩", "공식 API"),
    Source("instagram", SOCIAL, False, "인스타그램 해시태그", "공식 Graph API"),
)


def family_of(name: str) -> str | None:
    """출처 이름의 계열을 돌려준다. "rss:…" 은 글 계열이다. 목록에 없는 이름이면 None(계산에 쓰지 않는다)."""
    if name.startswith(RSS_PREFIX):
        return TEXT
    return next((source.family for source in SOURCES if source.name == name), None)


def core_sources(family: str) -> tuple[str, ...]:
    """그 계열의 기본 출처 이름들. 기본 설치에 포함된 출처를 안내한다. 계산 가능 여부는 signals.py가 정한다."""
    return tuple(source.name for source in SOURCES if source.family == family and source.core)

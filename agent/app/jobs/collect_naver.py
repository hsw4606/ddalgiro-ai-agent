"""네이버 검색 API 와 데이터랩 API 에서 트렌드 지표를 모은다 (안건 1 · 기획 02 §4).

실행: python -m app.jobs.collect_naver        (쿠버네티스에서는 CronJob 이 매일 실행)
모으는 것:
  - 검색 API(블로그·뉴스): 키워드로 최신 글을 받아 "날짜별 새 글 수" 와 글의 제목·주소·짧은 요약을 저장한다.
  - 데이터랩: 키워드의 최근 약 3개월(91일) 검색 관심도(0~100)를 저장한다. 긴 시간창 판정에 쓴다.
저장하지 않는 것: 글의 본문. 약관·저작권 때문에 메타 정보와 집계 숫자만 둔다.
광고 거르기: 제목·요약에 체험단·협찬 같은 표기가 있는 블로그 글은 글 수에서 빼고 따로 센다(is_ad).
            대가를 받고 쓴 글이 한꺼번에 늘어난 것을 인기로 착각하지 않기 위한 첫 방어선이다.
            표기가 본문 끝에만 있는 글은 요약에 드러나지 않아 잡지 못한다.
전제: 네이버 개발자 센터에서 받은 Client ID·Secret 이 환경 변수에 있어야 한다. 없으면 아무것도 하지 않고 끝난다.
한도: 검색 API 는 하루 25,000회, 데이터랩은 하루 1,000회다(네이버 개발자 센터 기준, 바뀔 수 있음).
      키워드 하나에 검색 6회 + 데이터랩 1회를 쓴다.
다른 출처: 인스타그램은 collect_instagram.py, RSS·웹 피드는 collect_rss.py 가 모은다. 하루치 집계와 DB 넣기는 app/collecting.py 를 함께 쓴다.
"""
from __future__ import annotations

import html
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app import collecting, dates, db
from app.collecting import complete_day_counts
from app.settings import settings

SEARCH_URL = "https://openapi.naver.com/v1/search/{kind}.json"
DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"
# 검색 종류 → DB 에 적는 출처 이름. 카페 글은 응답에 날짜가 없어 날짜별 집계를 할 수 없으므로 뺐다
KINDS = {"blog": "naver_blog", "news": "naver_news"}
PAGES = 3            # 한 번에 100건씩 몇 쪽을 받을지 (최신순)
DOCS_PER_SOURCE = 20  # 출처마다 저장할 글 수(브리핑의 근거로 쓴다)
PAUSE_SECONDS = 0.15  # 호출 사이 쉬는 시간(초). 짧은 시간에 몰아 부르지 않기 위한 것이다
DATALAB_DAYS = 91     # 데이터랩에서 받는 기간(일). trend/pipeline.py 의 LONG_WINDOW_DAYS 와 같아야 한다
# 블로그 글의 제목·요약에 이런 말이 있으면 광고·체험단 글로 본다. 뉴스에는 적용하지 않는다(기사 내용에 '광고' 가 흔히 나온다)
AD_MARKS = ("체험단", "협찬", "원고료", "제공받", "제공 받", "소정의", "유료광고", "유료 광고", "#광고", "[광고]", "(광고)")


@dataclass(frozen=True)
class SearchResults:
    """검색 글과 조회 범위. exhausted 는 다음 쪽이 없어 전체 검색 결과를 다 받았다는 뜻이다."""

    items: list[dict]
    exhausted: bool


def _headers() -> dict[str, str]:
    """네이버 API 가 요구하는 인증 헤더 두 개를 만든다. 값은 환경 변수에서 온다."""
    return {"X-Naver-Client-Id": settings.NAVER_CLIENT_ID, "X-Naver-Client-Secret": settings.NAVER_CLIENT_SECRET}


def _clean(text: str) -> str:
    """검색 결과의 강조 표시(<b>)와 HTML 기호(&quot; 등)를 걷어 낸다."""
    return html.unescape(re.sub(r"</?b>", "", text or "")).strip()


def is_ad(title: str, snippet: str) -> bool:
    """제목·요약에 광고·체험단 표기(AD_MARKS)가 하나라도 있으면 True."""
    text = f"{title} {snippet}"
    return any(mark in text for mark in AD_MARKS)


def _posted_date(item: dict, kind: str) -> date | None:
    """글이 올라온 날짜를 읽는다. 블로그는 postdate(20261110), 뉴스는 pubDate(영문 날짜 글자)다. 읽지 못하면 None."""
    try:
        if kind == "blog":
            return datetime.strptime(item["postdate"], "%Y%m%d").date()
        # 뉴스의 시차가 한국과 달라도 글을 센 날짜는 한국 날짜로 맞춘다
        return dates.today(parsedate_to_datetime(item["pubDate"]))
    except (KeyError, ValueError, TypeError):
        return None


def fetch_search(client: httpx.Client, kind: str, query: str) -> SearchResults:
    """최신 글 최대 PAGES×100 건과 전체 조회 여부를 돌려준다. 글 한 건은 title·url·snippet·posted·ad 사전이다.

    ad 는 광고·체험단 표기가 있는 블로그 글이면 True 다(뉴스는 항상 False).

    HTTP 오류(인증 실패 401, 한도 초과 429 등)는 예외로 올린다.
    """
    items: list[dict] = []
    for page in range(PAGES):
        response = client.get(
            SEARCH_URL.format(kind=kind),
            params={"query": query, "display": 100, "start": page * 100 + 1, "sort": "date"},
            headers=_headers(),
        )
        response.raise_for_status()
        batch = response.json().get("items", [])
        for it in batch:
            title, snippet = _clean(it.get("title", "")), _clean(it.get("description", ""))
            items.append({"title": title, "url": it.get("link", ""), "snippet": snippet,
                          "posted": _posted_date(it, kind), "ad": kind == "blog" and is_ad(title, snippet)})
        time.sleep(PAUSE_SECONDS)
        if len(batch) < 100:   # 더 받을 글이 없다
            return SearchResults(items, exhausted=True)
    return SearchResults(items, exhausted=False)


def fetch_datalab(client: httpx.Client, keyword: str, start: date, end: date, time_unit: str = "date") -> dict[date, float]:
    """키워드의 날짜별 검색 관심도를 받는다.

    time_unit 은 한 칸의 길이다: 'date'(하루) 또는 'week'(한 주, 날짜는 그 주의 시작일). 지금은 수집과 백테스팅 모두 'date' 만 쓴다.
    값은 실제 검색 수가 아니라 "요청한 기간 안의 최댓값을 100 으로 본 상대값" 이다. 증가율·가속 계산에는 충분하다.
    키워드를 하나씩 따로 요청하는 이유: 여러 개를 함께 넣으면 그중 가장 큰 키워드 기준으로 값이 정해지기 때문이다.
    """
    response = client.post(
        DATALAB_URL,
        json={"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": time_unit,
              "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}]},
        headers={**_headers(), "Content-Type": "application/json"},
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    points = results[0]["data"] if results else []
    return {date.fromisoformat(point["period"]): float(point["ratio"]) for point in points}


def collect_keyword(client: httpx.Client, keyword_id: int, term: str, today: date) -> None:
    """키워드 하나의 지표와 글 메타 정보를 모아 저장한다."""
    for kind, source in KINDS.items():
        result = fetch_search(client, kind, term)
        items = result.items
        counts = complete_day_counts(items, today, exhausted=result.exhausted)
        ad_counts = complete_day_counts(items, today, exhausted=result.exhausted, ads=True)
        with db.conn() as c:
            # 광고로 빠진 글 수는 날짜마다 extra 에 남긴다
            collecting.save_day_counts(c, keyword_id, source, counts, {day: {"ad_count": ad_counts.get(day, 0)} for day in counts})
            collecting.save_documents(c, keyword_id, source, items[:DOCS_PER_SOURCE])
    # 데이터랩은 어제까지의 값만 있다. 기간 안 최댓값이 100 이 되므로 받은 기간 전체를 매번 덮어쓴다
    ratios = fetch_datalab(client, term, today - timedelta(days=DATALAB_DAYS), today - timedelta(days=1))
    with db.conn() as c:
        collecting.save_search_ratios(c, keyword_id, "naver_datalab", ratios)
    time.sleep(PAUSE_SECONDS)


def main() -> int:
    """모든 키워드(샘플 제외)를 차례로 모은다. 반환값은 종료 코드: 실패한 키워드가 있으면 1."""
    if not (settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET):
        print("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 없어 수집을 건너뜁니다.")
        return 0
    db.wait_for_db()
    db.apply_schema()
    with db.conn() as c:
        keywords = c.execute("SELECT id, term FROM keywords WHERE NOT is_sample ORDER BY id").fetchall()
    today = dates.today()
    failed = 0
    with httpx.Client(timeout=10) as client:
        for keyword in keywords:
            try:
                collect_keyword(client, keyword["id"], keyword["term"], today)
                print(f"수집 완료: {keyword['term']}")
            except httpx.HTTPError as error:
                # 한 키워드의 실패가 나머지를 막지 않게 하고, 끝에 실패 수로 알린다
                failed += 1
                print(f"수집 실패: {keyword['term']} — {error}")
    print(f"키워드 {len(keywords)}개 중 실패 {failed}개")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

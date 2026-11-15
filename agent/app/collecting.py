"""수집 작업들이 함께 쓰는 것 — 하루치 집계 방법, DB 에 넣는 방법, 남의 사이트를 규칙을 지켜 읽는 방법 (안건 1).

쓰는 곳: jobs/collect_naver.py · collect_instagram.py · collect_rss.py. 새 수집 작업도 여기 함수를 쓴다.
지키는 원칙(연구개발계획서의 수집 거버넌스):
  - 공식 API 가 있으면 그것만 쓴다. 웹 페이지·피드를 직접 읽을 때는 그 사이트의 robots.txt 를 따르고 천천히 부른다(PoliteFetcher).
  - 글의 본문은 저장하지 않는다. 제목·주소·짧은 요약과 날짜별 집계 숫자만 둔다.
  - "하루치를 온전히 받은 날" 만 완료로 적는다. 누락은 0건이 아니다(complete_day_counts).
용어: robots.txt 는 사이트가 "자동 프로그램은 이 주소를 읽지 마시오" 를 적어 둔 파일이다. User-Agent 는 요청을 보내는 프로그램의 이름표다.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import psycopg

WINDOW_DAYS = 30   # 점수와 같은 기간 안에서만 하루치 집계를 보관한다


def complete_day_counts(items: list[dict], today: date, *, exhausted: bool = False, ads: bool = False) -> dict[date, int]:
    """받은 글을 날짜별로 세되, "하루치가 온전히 들어온 날" 만 돌려준다.

    items 의 글 한 건은 posted(올라온 날짜)와, 있으면 ad(광고·체험단 표시)·skip(이 키워드와 무관한 글) 을 가진 사전이다.
    ads=False 면 광고 표시가 없는 글 수(점수에 쓰는 값), True 면 표시가 있는 글 수를 센다. skip 인 글은 어느 쪽에도 세지 않는다.
    어느 쪽이든 "어느 날까지 온전히 받았는가" 는 받은 글 전체로 판단한다.
    exhausted=False 면 가장 오래된 날은 잘렸을 수 있어 제외한다. True 면 결과가 없는 날도 확인된 0건이다.
    오늘은 끝나지 않아 제외한다. 날짜를 못 읽은 글이 있으면 어느 날이 덜 집계됐는지 몰라 모두 판단을 보류한다.
    """
    if any(item["posted"] is None for item in items):
        return {}
    covered = {item["posted"] for item in items}
    counts = Counter(item["posted"] for item in items if bool(item.get("ad")) == ads and not item.get("skip"))
    start = today - timedelta(days=WINDOW_DAYS)
    if not exhausted:
        if not covered:
            return {}
        start = max(start, min(covered) + timedelta(days=1))
    # 조회 범위 안의 빈 날만 0건으로 기록한다. 범위 밖·잘린 날은 행을 만들지 않는다
    return {start + timedelta(days=offset): counts[start + timedelta(days=offset)]
            for offset in range(max(0, (today - start).days))}


def save_day_counts(c: psycopg.Connection, keyword_id: int, source: str, counts: dict[date, int],
                    extra: dict[date, dict[str, Any]] | None = None) -> None:
    """날짜별 글 수를 daily_metrics 에 "완료" 로 넣는다. 같은 날을 다시 모으면 값을 덮어쓴다(더 온전한 값으로 갱신).

    입력: source 는 trend/sources.py 에 있는 출처 이름, counts 는 {날짜: 글 수}, extra 는 {날짜: 부가 정보}(없으면 비운다).
    """
    for day, count in counts.items():
        c.execute(
            "INSERT INTO daily_metrics (keyword_id, metric_date, source, mention_count, is_complete, extra) "
            "VALUES (%s, %s, %s, %s, TRUE, %s) "
            "ON CONFLICT (keyword_id, metric_date, source) DO UPDATE SET "
            "mention_count = EXCLUDED.mention_count, is_complete = TRUE, extra = EXCLUDED.extra",
            (keyword_id, day, source, count, json.dumps((extra or {}).get(day)) if (extra or {}).get(day) is not None else None),
        )


def save_search_ratios(c: psycopg.Connection, keyword_id: int, source: str, ratios: dict[date, float]) -> None:
    """날짜별 검색 관심도(0~100)를 daily_metrics 에 "완료" 로 넣는다. 받은 기간 전체를 덮어쓴다."""
    for day, ratio in ratios.items():
        c.execute(
            "INSERT INTO daily_metrics (keyword_id, metric_date, source, search_ratio, is_complete) VALUES (%s, %s, %s, %s, TRUE) "
            "ON CONFLICT (keyword_id, metric_date, source) DO UPDATE SET search_ratio = EXCLUDED.search_ratio, is_complete = TRUE",
            (keyword_id, day, source, ratio),
        )


def save_documents(c: psycopg.Connection, keyword_id: int, source: str, items: list[dict]) -> None:
    """글의 메타 정보(제목·주소·요약·날짜·광고 표시)를 documents 에 넣는다. 브리핑의 근거로 쓰인다.

    같은 키워드 안에서만 중복을 막는다(다른 키워드의 근거로도 남겨야 한다). 주소가 없는 글은 넣지 않는다.
    """
    for item in items:
        if not item.get("url"):
            continue
        c.execute(
            "INSERT INTO documents (keyword_id, source, title, url, snippet, posted_at, is_ad) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (keyword_id, url) DO NOTHING",
            (keyword_id, source, item["title"], item["url"], item.get("snippet"), item.get("posted"), bool(item.get("ad"))),
        )


class PoliteFetcher:
    """공식 API 가 아닌 웹 주소(피드·페이지)를 규칙을 지켜 읽는다.

    하는 일: ① 사이트의 robots.txt 가 막은 주소는 읽지 않는다 ② 같은 사이트는 min_interval 초에 한 번만 부른다
            ③ 요청에 이 프로그램의 이름표(User-Agent)를 붙여, 사이트 관리자가 누구의 요청인지 알 수 있게 한다.
    robots.txt 를 받지 못했을 때: 파일이 없을 때(404·410)만 허용하고, 인증·한도·서버 오류는 읽지 않는다.
    이동 응답도 자동으로 따라가지 않고, 도착 주소의 규칙과 호출 간격을 먼저 확인한다.
    """

    def __init__(self, client: httpx.Client, user_agent: str, min_interval: float) -> None:
        self.client = client
        self.user_agent = user_agent
        self.min_interval = min_interval
        self._robots: dict[str, RobotFileParser | None] = {}   # 사이트별 robots.txt. None 은 "읽으면 안 됨"
        self._last_call: dict[str, float] = {}                 # 사이트별 마지막 호출 시각

    def _wait(self, host: str) -> None:
        """같은 사이트를 너무 빨리 다시 부르지 않게 남은 시간만큼 쉰다."""
        elapsed = time.monotonic() - self._last_call.get(host, float("-inf"))
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        """그 주소를 읽어도 되는지 robots.txt 로 확인한다. 사이트마다 한 번만 받아 기억해 둔다."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
            return False
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in self._robots:
            self._wait(parts.netloc)
            parser: RobotFileParser | None = RobotFileParser()
            try:
                response = self.client.get(f"{site}/robots.txt", headers={"User-Agent": self.user_agent}, follow_redirects=False)
                if response.status_code in (404, 410):
                    parser.parse([])                    # 파일 없음: 막은 규칙이 없다
                elif 200 <= response.status_code < 300:
                    parser.parse(response.text.splitlines())
                else:
                    parser = None                       # 이동·인증·한도 오류도 허용 근거로 삼지 않는다
            except httpx.HTTPError:
                parser = None
            self._robots[site] = parser
        rules = self._robots[site]
        return rules is not None and rules.can_fetch(self.user_agent, url)

    def get(self, url: str) -> httpx.Response | None:
        """각 이동 주소의 robots.txt를 확인하고 읽는다. 차단이면 None, 순환·6회 이상 이동·HTTP 오류면 예외다."""
        visited: set[str] = set()
        for _hop in range(6):
            if url in visited:
                raise httpx.TooManyRedirects("수집 주소가 순환 이동합니다.")
            visited.add(url)
            if not self.allowed(url):
                return None
            # 실제 페이지 요청 직전에 속도 제한을 적용한다. httpx의 자동 이동은 사용하지 않는다.
            self._wait(urlsplit(url).netloc)
            response = self.client.get(url, headers={"User-Agent": self.user_agent}, follow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                url = str(response.url.join(response.headers["location"]))
                continue
            response.raise_for_status()
            return response
        raise httpx.TooManyRedirects("수집 주소의 이동 횟수가 한도를 넘었습니다.")

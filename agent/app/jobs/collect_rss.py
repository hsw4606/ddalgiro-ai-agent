"""RSS·Atom 피드에서 키워드의 날짜별 글 수와 글 메타 정보를 모은다 (안건 1, 글 계열의 출처).

실행: python -m app.jobs.collect_rss          (쿠버네티스에서는 CronJob 이 매일 03:20 에 실행)
      python -m app.jobs.collect_rss --check 주소 [주소 …]   (피드 후보 점검 — DB 를 쓰지 않고 화면에만 결과를 찍는다)
피드 목록: 설정 RSS_FEEDS(JSON) — [{"name": "매체이름", "url": "https://…"}].
  - 주소에 {keyword} 가 있으면 "검색 피드" 다. 키워드마다 그 자리에 넣어 부르고, 받은 글을 모두 그 키워드의 글로 센다.
  - 없으면 "매체 피드" 다. 한 번만 받아, 제목·요약에 키워드(또는 키워드의 다른 표기)가 들어 있는 글만 그 키워드의 글로 센다.
    띄어쓰기와 영문 대소문자는 무시하고 견준다('두바이 초콜릿' 도 '두바이초콜릿' 의 글이다).
    다른 표기(keywords.aliases)는 외국 매체를 읽을 때 쓴다(예: 'Dubai chocolate'). 검색 피드에는 키워드 자체만 넣어 부른다.
  비어 있으면 아무것도 하지 않고 끝난다. DB 에는 출처 이름 "rss:매체이름" 으로 들어간다.
지키는 것: 사이트의 robots.txt 가 막은 주소는 읽지 않고, 같은 사이트는 몇 초에 한 번만 부른다(app/collecting.py 의 PoliteFetcher).
          글의 본문은 저장하지 않는다. 제목·주소·요약 앞부분과 날짜별 글 수만 둔다.
피드를 더하기 전에: 그 사이트의 이용 약관이 자동 수집을 허용하는지, 상업적 이용 조건이 있는지 사람이 확인한다.
                  robots.txt 는 기계가 확인하지만 약관은 확인하지 못한다.
피드가 아닌 웹 페이지(목록 화면 등)를 읽어야 하면: 같은 PoliteFetcher 로 받고 그 사이트에 맞는 해석 함수를 따로 만든다.
                  이 파일의 parse_feed 자리에 그 함수를 두면 나머지(집계·저장)는 그대로 쓸 수 있다.
"""
from __future__ import annotations

import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from app import collecting, dates, db
from app.settings import settings
from app.trend.sources import RSS_PREFIX

MAX_BYTES = 2_000_000   # 이보다 큰 응답은 피드로 보지 않는다(잘못된 주소나 지나치게 큰 문서 방지)
DOCS_PER_FEED = 20      # 키워드·피드마다 저장할 글 수(브리핑의 근거)
SNIPPET_CHARS = 200     # 요약으로 보관하는 글자 수
ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"            # RSS 1.0(RDF) 피드의 항목 이름 앞에 붙는 표시
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"   # pubDate 대신 날짜를 담는 칸(dc:date)


def load_feeds() -> list[dict[str, str]]:
    """설정 RSS_FEEDS 를 읽어 [{"name", "url"}] 로 돌려준다. 형식이 틀리면 ValueError."""
    feeds = json.loads(settings.RSS_FEEDS or "[]")
    if not isinstance(feeds, list) or any(not isinstance(feed, dict) or not feed.get("name") or not feed.get("url") for feed in feeds):
        raise ValueError('RSS_FEEDS 는 [{"name": "매체이름", "url": "https://…"}] 모양의 JSON 이어야 합니다.')
    return feeds


def _text(value: str | None) -> str:
    """피드의 글자에서 HTML 표시와 기호(&amp; 등)를 걷어 내고 공백을 정리한다."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def _posted(value: str | None) -> date | None:
    """피드의 날짜 글자를 한국 날짜로 바꾼다. RSS 는 'Fri, 13 Nov 2026 18:00:00 +0000', Atom 은 '2026-11-13T18:00:00Z' 모양이다.

    시간대가 없는 날짜('2026-11-13 19:09:19')는 한국 시각으로 본다. 국내 매체의 피드가 이렇게 적는 일이 많기 때문이다.
    (시간대 없이 적는 외국 피드라면 하루까지 어긋날 수 있다.)
    읽지 못하면 None — 어느 날의 글인지 모르는 글이 섞이면 그 피드의 하루치 집계를 보류한다.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        instant = datetime.fromisoformat(text) if text[:4].isdigit() else parsedate_to_datetime(text)
        if instant.utcoffset() is None:
            instant = instant.replace(tzinfo=dates.SEOUL)
        return dates.today(instant)
    except (ValueError, TypeError):
        return None


def parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0·RSS 1.0(RDF)·Atom 글자를 글 목록으로 바꾼다. 글 한 건은 title·url·snippet·posted 사전이다.

    XML 이 아니거나 글 항목이 없으면 빈 목록. 요약은 앞 SNIPPET_CHARS 글자만 남긴다(본문을 보관하지 않기 위해서다).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter("item"):   # RSS 2.0. 날짜는 pubDate, 없으면 dc:date
        items.append({"title": _text(node.findtext("title")), "url": (node.findtext("link") or "").strip(),
                      "snippet": _text(node.findtext("description"))[:SNIPPET_CHARS],
                      "posted": _posted(node.findtext("pubDate") or node.findtext(DC_DATE))})
    for node in root.iter(f"{RSS1}item"):   # RSS 1.0(RDF). 칸 이름마다 표시가 붙고 날짜는 dc:date 에 있다
        items.append({"title": _text(node.findtext(f"{RSS1}title")), "url": (node.findtext(f"{RSS1}link") or "").strip(),
                      "snippet": _text(node.findtext(f"{RSS1}description"))[:SNIPPET_CHARS], "posted": _posted(node.findtext(DC_DATE))})
    for node in root.iter(f"{ATOM}entry"):   # Atom
        link = node.find(f"{ATOM}link")
        items.append({"title": _text(node.findtext(f"{ATOM}title")), "url": (link.get("href") if link is not None else "") or "",
                      "snippet": _text(node.findtext(f"{ATOM}summary"))[:SNIPPET_CHARS],
                      "posted": _posted(node.findtext(f"{ATOM}published") or node.findtext(f"{ATOM}updated"))})
    return items


def _squash(text: str) -> str:
    """견주기 쉽게 띄어쓰기를 모두 빼고 영문을 소문자로 바꾼다."""
    return re.sub(r"\s+", "", text).lower()


def mark_for_keyword(items: list[dict], terms: list[str]) -> list[dict]:
    """매체 피드의 글에 "이 키워드와 무관함"(skip) 표시를 붙인다.

    terms 는 키워드와 그 다른 표기들이다. 제목·요약에 그 가운데 하나도 없으면 무관한 글이다(띄어쓰기·대소문자 무시).
    무관한 글을 지우지 않고 표시만 하는 이유: "이 피드를 어느 날까지 온전히 받았는가" 는 피드의 글 전체로 판단해야 하기 때문이다.
    """
    wanted = [_squash(term) for term in terms if term.strip()]
    return [{**item, "skip": not any(term in _squash(f"{item['title']} {item['snippet']}") for term in wanted)} for item in items]


def fetch_feed(fetcher: collecting.PoliteFetcher, url: str) -> list[dict] | None:
    """피드 주소 하나를 받아 글 목록으로 돌려준다. robots.txt 가 막았으면 None. HTTP 오류는 예외로 올린다."""
    response = fetcher.get(url)
    if response is None:
        return None
    if len(response.content) > MAX_BYTES:
        return []
    return parse_feed(response.text)


def check_feed(fetcher: collecting.PoliteFetcher, url: str, today: date) -> dict:
    """피드 후보 하나를 점검한다 — 목록에 넣기 전에 "기계가 읽어도 되는가, 하루치를 셀 수 있는가" 를 본다.

    반환: {"url", "robots_allowed", "items"(글 수), "undated"(날짜를 읽지 못한 글 수), "oldest"·"newest"(글 날짜 범위),
           "complete_days"(온전한 하루로 셀 수 있는 날수), "per_day"(그 날들의 하루 평균 글 수), "usable", "note"(판정 이유)}.
    usable 은 robots.txt 가 허용하고, 글이 있고, 모든 글의 날짜를 읽을 수 있고, 온전한 날에 든 글이 하나 이상일 때만 True 다.
    확인하지 못하는 것: 약관, 그리고 피드가 "최신순으로 빠짐없이" 내주는지(검색 결과를 관련도순으로 일부만 주는 피드는
    날짜가 띄엄띄엄해 하루치를 셀 수 없다 — 결과의 날짜 범위가 유난히 길면 의심한다). HTTP 오류는 예외로 올린다.
    """
    result: dict = {"url": url, "robots_allowed": fetcher.allowed(url), "items": 0, "undated": 0, "oldest": None,
                    "newest": None, "complete_days": 0, "per_day": None, "usable": False, "note": ""}
    if not result["robots_allowed"]:
        result["note"] = "robots.txt 가 자동 수집을 막았거나 robots.txt 를 받지 못했다"
        return result
    items = fetch_feed(fetcher, url) or []
    posted = [item["posted"] for item in items if item["posted"]]
    counts = collecting.complete_day_counts(items, today, exhausted=False)
    result.update(items=len(items), undated=len(items) - len(posted), oldest=min(posted, default=None), newest=max(posted, default=None),
                  complete_days=len(counts), per_day=round(sum(counts.values()) / len(counts), 1) if counts else None)
    if not items:
        result["note"] = "피드로 읽을 수 없거나 글이 없다"
    elif result["undated"]:
        result["note"] = "날짜를 읽을 수 없는 글이 있어 하루치를 셀 수 없다"
    elif not counts or sum(counts.values()) == 0:
        # 온전한 날에 든 글이 하나도 없으면, 글이 너무 많아 오늘 것만 담겼거나 오래된 글 몇 개가 끼어 기간만 길어 보이는 피드다
        result["note"] = "온전한 하루의 글이 없다(글이 너무 많아 최근 몇 시간만 담기는 피드일 수 있다). 하루 한 번 수집으로는 셀 수 없다"
    else:
        result["usable"] = True
        # 피드가 며칠 치를 담고 있는지에 따라 얼마나 자주 모아야 하는지가 정해진다
        result["note"] = ("하루 한 번 수집으로 충분하다" if result["newest"] - result["oldest"] >= timedelta(days=2)
                          else "담긴 기간이 이틀이 안 된다 — 하루라도 수집을 놓치면 그날이 빈다")
    return result


def run_check(urls: list[str]) -> int:
    """피드 후보들을 점검해 한 줄씩 찍는다. DB 를 쓰지 않는다. 반환값은 종료 코드: 쓸 수 없는 후보가 있으면 1."""
    today = dates.today()
    unusable = 0
    with httpx.Client(timeout=15) as client:
        fetcher = collecting.PoliteFetcher(client, settings.CRAWL_USER_AGENT, settings.CRAWL_MIN_INTERVAL_SECONDS)
        for url in urls:
            try:
                result = check_feed(fetcher, url, today)
            except httpx.HTTPError as error:
                result = {"url": url, "usable": False, "note": f"받지 못했다 — {type(error).__name__}: {str(error)[:80]}"}
            unusable += 0 if result["usable"] else 1
            detail = (f"글 {result['items']}건({result['oldest']}~{result['newest']}), 온전한 날 {result['complete_days']}일, "
                      f"하루 평균 {result['per_day']}건 — " if result.get("items") else "")
            print(f"[{'사용 가능' if result['usable'] else '사용 불가'}] {url}\n    {detail}{result['note']}")
    print(f"후보 {len(urls)}개 중 사용 불가 {unusable}개. 약관(자동 수집·사업 목적 이용 허용 여부)은 사람이 따로 확인한다.")
    return 1 if unusable else 0


def save(keyword_id: int, source: str, items: list[dict], today: date) -> None:
    """키워드의 하루치 글 수와 글 메타 정보를 저장한다. 피드는 최신 글만 주므로 가장 오래된 날은 잘린 것으로 본다."""
    counts = collecting.complete_day_counts(items, today, exhausted=False)
    matched = [item for item in items if not item.get("skip")]
    with db.conn() as c:
        collecting.save_day_counts(c, keyword_id, source, counts)
        collecting.save_documents(c, keyword_id, source, matched[:DOCS_PER_FEED])


def main() -> int:
    """모든 피드를 키워드(샘플 제외)마다 모은다. 반환값은 종료 코드: 설정이 틀리면 2, 실패한 피드가 있으면 1.

    인자에 --check 가 있으면 그 뒤의 주소들을 점검만 하고 끝낸다(run_check).
    """
    if "--check" in sys.argv:
        urls = [arg for arg in sys.argv[sys.argv.index("--check") + 1 :] if not arg.startswith("--")]
        if not urls:
            print("사용법: python -m app.jobs.collect_rss --check 주소 [주소 …]")
            return 2
        return run_check(urls)
    try:
        feeds = load_feeds()
    except ValueError as error:
        print(f"설정 오류: {error}")
        return 2
    if not feeds:
        print("RSS_FEEDS 가 비어 있어 피드 수집을 건너뜁니다.")
        return 0
    db.wait_for_db()
    db.apply_schema()
    with db.conn() as c:
        keywords = c.execute("SELECT id, term, aliases FROM keywords WHERE NOT is_sample ORDER BY id").fetchall()
    today = dates.today()
    failed = 0
    with httpx.Client(timeout=15) as client:
        fetcher = collecting.PoliteFetcher(client, settings.CRAWL_USER_AGENT, settings.CRAWL_MIN_INTERVAL_SECONDS)
        for feed in feeds:
            source = RSS_PREFIX + feed["name"]
            try:
                if "{keyword}" in feed["url"]:
                    # 검색 피드: 키워드마다 주소를 만들어 부른다
                    for keyword in keywords:
                        items = fetch_feed(fetcher, feed["url"].replace("{keyword}", quote(keyword["term"])))
                        if items is None:
                            raise PermissionError("robots.txt 가 이 주소의 자동 수집을 막았습니다.")
                        save(keyword["id"], source, items, today)
                else:
                    # 매체 피드: 한 번 받아 키워드마다 해당 글만 센다
                    items = fetch_feed(fetcher, feed["url"])
                    if items is None:
                        raise PermissionError("robots.txt 가 이 주소의 자동 수집을 막았습니다.")
                    for keyword in keywords:
                        save(keyword["id"], source, mark_for_keyword(items, [keyword["term"], *keyword["aliases"]]), today)
                print(f"수집 완료: {source}")
            except (httpx.HTTPError, PermissionError) as error:
                # 한 피드의 실패가 나머지를 막지 않게 하고, 끝에 실패 수로 알린다
                failed += 1
                print(f"수집 실패: {source} — {error}")
    print(f"피드 {len(feeds)}개 중 실패 {failed}개")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

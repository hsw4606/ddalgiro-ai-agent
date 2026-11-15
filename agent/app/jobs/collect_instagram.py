"""인스타그램 해시태그의 하루치 게시물 수를 모은다 — Meta 의 공식 Graph API 만 쓴다 (안건 1, SNS 계열의 출처).

실행: python -m app.jobs.collect_instagram     (쿠버네티스에서는 CronJob 이 6시간마다 10분에 실행)
모으는 것: 최근 24시간 공개 게시물을 겹쳐 조회하고, 게시물 번호 해시·시각·반응 수를 8일 보관한다.
          중복은 해시로 합치며 원문·작성자·사진·주소는 저장하지 않는다. 일별 집계 숫자는 계속 보관한다.
완료 판단: 모든 페이지와 식별자·시각을 확인한 조회 구간들의 합집합이 한국 날짜 하루 전체를 덮어야 한다.
          첫 조회, 페이지 한도, 시각 누락, 장시간 장애로 생긴 구멍은 0건이나 완료로 처리하지 않는다.
전제: 인스타그램 비즈니스·크리에이터 계정, 그 계정과 이어진 페이스북 페이지, Meta 앱 심사(Instagram Public Content Access 기능과
      instagram_basic 권한)를 통과한 앱의 사용자 접근 토큰. 계정 번호와 토큰이 환경 변수에 없으면 아무것도 하지 않고 끝난다.
한도(Meta 문서 기준, 바뀔 수 있음): 계정 하나가 7일 동안 조회할 수 있는 해시태그는 30개다. 한 번 조회한 해시태그는 7일 동안 자리를 차지한다.
      그래서 이미 조회 중인 키워드를 먼저 잇고, 남은 자리만 트렌드 점수가 높은 키워드로 채운다(instagram_hashtags 에 기록).
      한 쪽에 50건까지 받고 MAX_PAGES 쪽까지만 넘긴다. 다 받지 못한 날은 "완료" 로 적지 않는다.
토큰: 주소에 넣지 않고 Authorization 헤더로 보낸다. 주소에 넣으면 오류 글에 토큰이 찍혀 로그에 남기 때문이다.
⚠️ 실제 계정·토큰으로는 확인하지 못했다. 요청·응답의 모양은 Meta 개발자 문서(IG Hashtag Search, recent_media)를 따랐다.
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from datetime import date, datetime, time as day_time, timedelta

import httpx

from app import dates, db
from app.settings import settings

SOURCE = "instagram"   # trend/sources.py 의 출처 이름
PAGE_SIZE = 50         # 한 쪽에 받는 게시물 수(Meta 의 최댓값)
MAX_PAGES = 6          # 해시태그 하나에 넘길 쪽 수. 하루 300건을 넘는 해시태그는 온전한 하루를 세지 못한다
QUOTA_DAYS = 7         # 한 번 조회한 해시태그가 한도의 자리를 차지하는 날수
PAUSE_SECONDS = 0.3    # 호출 사이 쉬는 시간(초)
# 해시태그로 쓸 수 있는 글자(한글·영문·숫자·밑줄). 이모지나 기호가 든 말은 조회할 수 없다
_HASHTAG = re.compile(r"[0-9A-Za-z가-힣_]+")


def hashtag_for(term: str) -> str | None:
    """키워드를 해시태그로 바꾼다(띄어쓰기 제거). 해시태그로 쓸 수 없는 글자가 있으면 None."""
    tag = re.sub(r"\s+", "", term)
    return tag if _HASHTAG.fullmatch(tag) else None


def pick_keywords(active: list[int], candidates: list[int], limit: int) -> list[int]:
    """오늘 조회할 키워드 번호를 고른다.

    입력: active 는 최근 7일 안에 조회해 이미 한도의 자리를 차지한 키워드, candidates 는 조회하고 싶은 순서(점수 높은 순),
          limit 은 7일 한도(해시태그 수).
    반환: active 전부 + 남은 자리만큼의 새 후보. 이미 자리를 차지한 것은 다시 조회해도 자리가 늘지 않으므로 계속 잇는다.
    """
    room = max(0, limit - len(active))
    fresh = [keyword_id for keyword_id in candidates if keyword_id not in active]
    return active + fresh[:room]


def _base() -> str:
    """Graph API 주소의 앞부분(버전 포함)."""
    return f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_VERSION}"


def _headers() -> dict[str, str]:
    """접근 토큰을 담은 인증 헤더."""
    return {"Authorization": f"Bearer {settings.INSTAGRAM_ACCESS_TOKEN}"}


def fetch_hashtag_id(client: httpx.Client, hashtag: str) -> str | None:
    """해시태그의 번호를 받는다. 없는 해시태그면 None. HTTP 오류(토큰 만료·한도 초과·민감한 해시태그 등)는 예외로 올린다."""
    response = client.get(f"{_base()}/ig_hashtag_search", params={"user_id": settings.INSTAGRAM_USER_ID, "q": hashtag},
                          headers=_headers())
    response.raise_for_status()
    data = response.json().get("data", [])
    return data[0]["id"] if data else None


def fetch_recent_media(client: httpx.Client, hashtag_id: str) -> tuple[list[dict], bool]:
    """해시태그의 최근 24시간 게시물을 받는다.

    반환: (게시물 목록, 끝까지 받았는지). 게시물 한 건은 timestamp·like_count·comments_count 를 가진 사전이다
          (좋아요 수를 숨긴 게시물은 like_count 가 없다). MAX_PAGES 쪽을 넘겨도 다음 쪽이 남아 있으면 끝까지 받지 못한 것이다.
    """
    items: list[dict] = []
    after: str | None = None
    for _page in range(MAX_PAGES):
        params = {"user_id": settings.INSTAGRAM_USER_ID, "fields": "id,timestamp,like_count,comments_count", "limit": PAGE_SIZE}
        if after:
            params["after"] = after
        response = client.get(f"{_base()}/{hashtag_id}/recent_media", params=params, headers=_headers())
        response.raise_for_status()
        body = response.json()
        items += body.get("data", [])
        # 다음 쪽의 위치표. 응답의 next 주소에는 토큰이 들어 있을 수 있어 쓰지 않고 위치표만 쓴다
        paging = body.get("paging") or {}
        after = paging.get("cursors", {}).get("after") if paging.get("next") else None
        time.sleep(PAUSE_SECONDS)
        if not paging.get("next"):
            return items, True
        if not after:
            return items, False  # 다음 쪽이 있는데 위치표가 없으면 미완료

    return items, False


def day_summary(items: list[dict], day: date) -> dict[str, int]:
    """게시물 가운데 한국 날짜가 day 인 것의 수·좋아요 합·댓글 합을 낸다. 시각을 읽지 못한 게시물은 세지 않는다."""
    summary = {"count": 0, "likes": 0, "comments": 0}
    for item in items:
        try:
            posted = dates.today(datetime.strptime(item["timestamp"], "%Y-%m-%dT%H:%M:%S%z"))
        except (KeyError, ValueError, TypeError):
            continue
        if posted == day:
            summary["count"] += 1
            summary["likes"] += int(item.get("like_count") or 0)
            summary["comments"] += int(item.get("comments_count") or 0)
    return summary


def parse_observations(items: list[dict]) -> tuple[list[tuple], bool]:
    """API 응답을 (해시, 시각, 좋아요, 댓글)로 바꾼다. 한 건이라도 식별자·시각·수가 잘못되면 완료를 허용하지 않는다."""
    observations = {}
    valid = True
    for item in items:
        try:
            media_id = str(item["id"]).strip()
            posted = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            likes, comments = int(item.get("like_count") or 0), int(item.get("comments_count") or 0)
            if not media_id or media_id == "None" or posted.utcoffset() is None or min(likes, comments) < 0:
                raise ValueError("식별자·시각·반응 수 확인 필요")
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
            continue
        # 원본 번호는 저장하지 않는다. 겹친 조회에서도 같은 게시물은 한 번만 집계한다.
        digest = hashlib.sha256(media_id.encode()).hexdigest()
        observations[digest] = (digest, posted, likes, comments)
    return list(observations.values()), valid


def covers_day(windows: list[tuple[datetime, datetime]], day: date) -> bool:
    """확인한 시간 구간의 합집합이 한국 날짜 day의 자정부터 다음 자정까지 끊김 없이 덮는지 검사한다."""
    cursor = datetime.combine(day, day_time.min, dates.SEOUL)
    end = cursor + timedelta(days=1)
    for start, stop in sorted(windows):
        if stop <= cursor:
            continue
        if start > cursor:
            return False
        cursor = max(cursor, stop)
        if cursor >= end:
            return True
    return False


def collect_keyword(client: httpx.Client, keyword_id: int, hashtag: str, hashtag_id: str | None,
                    today: date, *, now: datetime | None = None) -> bool:
    """중복 관측값과 안전 조회 구간을 저장하고 최근 이틀을 다시 집계한다. now는 시험용 고정 시각이다.

    HTTP·DB 실패는 호출자에 전파한다. 해시태그가 없으면 False, 저장했으면 True이며 완료 여부는 DB에 별도로 기록한다.
    """
    started = now or datetime.now(dates.SEOUL)
    hashtag_id = hashtag_id or fetch_hashtag_id(client, hashtag)
    if hashtag_id is None:
        return False
    with db.conn() as c:
        # 뒤의 호출이 실패해도 조회한 해시태그는 7일 한도의 자리를 차지한다.
        c.execute(
            "INSERT INTO instagram_hashtags (keyword_id, hashtag, hashtag_id, last_queried) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (keyword_id) DO UPDATE SET hashtag = EXCLUDED.hashtag, hashtag_id = EXCLUDED.hashtag_id, "
            "last_queried = EXCLUDED.last_queried", (keyword_id, hashtag, hashtag_id, today))
    items, exhausted = fetch_recent_media(client, hashtag_id)
    finished = now or datetime.now(dates.SEOUL)
    observations, valid = parse_observations(items)
    # 여러 페이지 조회 중 흐른 시간도 제외한다. 모든 페이지의 24시간 창이 겹치는 부분만 보증한다.
    start, end = finished - timedelta(hours=24), started
    with db.conn() as c:
        for digest, posted, likes, comments in observations:
            c.execute("INSERT INTO instagram_observations VALUES (%s,%s,%s,%s,%s) "
                      "ON CONFLICT (keyword_id,media_hash) DO UPDATE SET likes=EXCLUDED.likes, comments=EXCLUDED.comments",
                      (keyword_id, digest, posted, likes, comments))
        if exhausted and valid and start <= end:
            c.execute("INSERT INTO instagram_windows VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (keyword_id, start, end))
        windows = c.execute("SELECT start_at,end_at FROM instagram_windows WHERE keyword_id=%s", (keyword_id,)).fetchall()
        # 두 번 겹쳐 받은 게시물도 관측 테이블의 기본 키 때문에 한 건이다. 오늘은 아직 끝나지 않아 집계하지 않는다.
        for offset in (2, 1):
            day = today - timedelta(days=offset)
            day_start = datetime.combine(day, day_time.min, dates.SEOUL)
            summary = c.execute("SELECT count(*) AS count, coalesce(sum(likes),0)::int AS likes, "
                                "coalesce(sum(comments),0)::int AS comments FROM instagram_observations "
                                "WHERE keyword_id=%s AND posted_at >= %s AND posted_at < %s",
                                (keyword_id, day_start, day_start + timedelta(days=1))).fetchone()
            complete = covers_day([(w["start_at"], w["end_at"]) for w in windows], day)
            c.execute("INSERT INTO daily_metrics (keyword_id,metric_date,source,mention_count,is_complete,extra) "
                      "VALUES (%s,%s,%s,%s,%s,jsonb_build_object('likes',%s::int,'comments',%s::int,'coverage_version','v2')) "
                      "ON CONFLICT (keyword_id,metric_date,source) DO UPDATE SET mention_count=EXCLUDED.mention_count, "
                      "is_complete=EXCLUDED.is_complete, extra=EXCLUDED.extra",
                      (keyword_id, day, SOURCE, summary["count"], complete, summary["likes"], summary["comments"]))
        # 일별 집계는 남기고 중복 제거용 최소 관측값과 구간만 8일 뒤 지운다.
        cutoff = started - timedelta(days=8)
        c.execute("DELETE FROM instagram_observations WHERE posted_at < %s", (cutoff,))
        c.execute("DELETE FROM instagram_windows WHERE end_at < %s", (cutoff,))
    return True


def main() -> int:
    """7일 한도 안에서 키워드를 골라 차례로 모은다. 반환값은 종료 코드: 실패한 키워드가 있으면 1."""
    if not (settings.INSTAGRAM_USER_ID and settings.INSTAGRAM_ACCESS_TOKEN):
        print("INSTAGRAM_USER_ID / INSTAGRAM_ACCESS_TOKEN 이 없어 인스타그램 수집을 건너뜁니다.")
        return 0
    db.wait_for_db()
    db.apply_schema()
    today = dates.today()
    with db.conn() as c:
        tracked = {row["keyword_id"]: row for row in c.execute(
            "SELECT keyword_id, hashtag_id, last_queried FROM instagram_hashtags").fetchall()}
        # 조회하고 싶은 순서: 가장 최근 트렌드 점수가 높은 키워드부터(점수가 아직 없으면 뒤로). 샘플은 뺀다
        keywords = c.execute(
            """
            SELECT k.id, k.term, (SELECT s.score FROM trend_scores s WHERE s.keyword_id = k.id ORDER BY s.score_date DESC LIMIT 1) AS score
            FROM keywords k WHERE NOT k.is_sample ORDER BY score DESC NULLS LAST, k.id
            """
        ).fetchall()
    hashtags = {row["id"]: hashtag_for(row["term"]) for row in keywords}
    candidates = [row["id"] for row in keywords if hashtags[row["id"]]]
    active = [keyword_id for keyword_id in candidates
              if keyword_id in tracked and tracked[keyword_id]["last_queried"] > today - timedelta(days=QUOTA_DAYS)]
    chosen = pick_keywords(active, candidates, settings.INSTAGRAM_MAX_HASHTAGS)
    failed = 0
    with httpx.Client(timeout=20) as client:
        for keyword_id in chosen:
            hashtag = hashtags[keyword_id]
            try:
                saved = collect_keyword(client, keyword_id, hashtag, (tracked.get(keyword_id) or {}).get("hashtag_id"), today)
                print(f"수집 완료: #{hashtag}" if saved else f"해시태그 없음(건너뜀): #{hashtag}")
            except httpx.HTTPStatusError as error:
                # 응답 본문의 오류 설명만 찍는다(토큰 만료·한도 초과·민감한 해시태그). 요청 주소·헤더는 찍지 않는다
                failed += 1
                print(f"수집 실패: #{hashtag} — {error.response.status_code} {error.response.text[:200]}")
            except httpx.HTTPError as error:
                failed += 1
                print(f"수집 실패: #{hashtag} — {type(error).__name__}")
    print(f"해시태그 {len(chosen)}개 조회(한도 {settings.INSTAGRAM_MAX_HASHTAGS}개/7일, 후보 {len(candidates)}개), 실패 {failed}개")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

-- 딸기로 AI 에이전트 DB 테이블 정의 (PostgreSQL + pgvector)
--
-- 쓰는 곳: agent-api 가 시작할 때마다 이 파일을 실행한다(app/db.py 의 apply_schema).
--          새 칸·색인은 없을 때만 만들고, 옛 제약은 있을 때만 지워 여러 번 실행해도 같은 상태가 된다.
-- 전제:   vector 확장은 슈퍼유저만 만들 수 있어 PostgreSQL 첫 기동 스크립트(k8s/10-postgres.yaml)가 미리 만든다.
--          이 파일은 일반 계정(agent)으로 실행된다.
-- 묶음:   1) 트렌드 분석(안건 1 · 기획 02)  2) 자연어 추천·랭킹(안건 2 · 기획 03)  3) 실행 기록
--          안건 2 의 단체주문 DB 테이블 6종(요청·공급 조건·제안 옵션·제안서·반응·납품)은 연구개발계획서의 데이터 모델을 따른다.
-- 주의:   embedding 칸의 1024 는 임베딩 모델 bge-m3 의 출력 차원이다.
--          모델을 바꾸면 이 숫자와 app/settings.py 의 EMBED_DIM 을 함께 바꾸고 임베딩을 다시 만들어야 한다.

-- ───────────────────── 1) 트렌드 분석 (안건 1 · 기획 02) ─────────────────────

-- 감시할 디저트 키워드(아이템). 수집·점수 계산의 기준 목록이다.
CREATE TABLE IF NOT EXISTS keywords (
    id          SERIAL PRIMARY KEY,
    term        TEXT UNIQUE NOT NULL,           -- 검색어 (예: 두바이초콜릿)
    category    TEXT,                           -- 분류 (예: 초콜릿·빵·케이크). 업체 맞춤 추천의 적합도 계산에 쓴다
    aliases     TEXT[] NOT NULL DEFAULT '{}',   -- 다른 표기(예: 'Dubai chocolate'). 매체 피드에서 이 말이 든 글도 이 키워드의 글로 센다
    is_sample   BOOLEAN NOT NULL DEFAULT FALSE, -- 연습용 샘플 데이터면 TRUE (seed_sample 작업이 넣고 지운다)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS aliases TEXT[] NOT NULL DEFAULT '{}';

-- 키워드·날짜·출처별 집계 지표. 원문은 두지 않고 숫자만 둔다. 트렌드 점수 계산의 입력이다.
CREATE TABLE IF NOT EXISTS daily_metrics (
    keyword_id     INT  NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    metric_date    DATE NOT NULL,
    source         TEXT NOT NULL,   -- naver_blog / naver_news / naver_datalab / instagram / rss:매체이름 / sample (목록: app/trend/sources.py)
    mention_count  INT  NOT NULL DEFAULT 0,  -- 그날 새로 올라온 글 수. 광고·체험단으로 표시된 글은 세지 않는다
    search_ratio   REAL,            -- 검색 관심도 0~100 (데이터랩: 기간 안 최댓값을 100 으로 본 상대값)
    sentiment_pos  REAL,            -- 긍정 글 비율 0~1 (LLM 이 글 제목·요약을 분류한 결과)
    is_complete    BOOLEAN NOT NULL DEFAULT FALSE, -- 이 출처의 하루치가 온전히 수집됐을 때만 TRUE. 누락은 0건과 다르다
    extra          JSONB,           -- 출처별 부가 정보. 블로그는 {"ad_count": 그날 광고·체험단으로 빠진 글 수}, 인스타그램은 {"likes": …, "comments": …}
    PRIMARY KEY (keyword_id, metric_date, source)
);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON daily_metrics (metric_date);
-- 기존 DB 의 지표는 수집 완료 여부를 알 수 없어 FALSE 로 둔다. 다시 수집한 날짜부터 점수에 쓴다
ALTER TABLE daily_metrics ADD COLUMN IF NOT EXISTS is_complete BOOLEAN NOT NULL DEFAULT FALSE;

-- 인스타그램에서 조회 중인 해시태그. 계정마다 "7일 동안 해시태그 30개" 한도가 있어, 어떤 키워드가 자리를 차지하는지 적어 둔다.
-- 해시태그 번호(hashtag_id)는 바뀌지 않으므로 한 번 받은 것을 다시 쓴다.
CREATE TABLE IF NOT EXISTS instagram_hashtags (
    keyword_id    INT PRIMARY KEY REFERENCES keywords(id) ON DELETE CASCADE,
    hashtag       TEXT NOT NULL,     -- 조회에 쓴 해시태그(키워드에서 띄어쓰기를 뺀 말)
    hashtag_id    TEXT NOT NULL,     -- 인스타그램이 준 해시태그 번호
    last_queried  DATE NOT NULL      -- 마지막으로 조회한 날짜. 7일 안이면 한도의 한 자리를 차지하고 있는 것이다
);

-- 수집한 글의 메타 정보(제목·주소·짧은 요약). 본문 전체는 저장하지 않는다(저작권·약관).
-- AI 브리핑이 "출처"로 가리키는 근거가 이 테이블의 행이다.
CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    keyword_id  INT  NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,                  -- 한 글이 여러 키워드의 근거가 될 수 있다
    snippet     TEXT,                           -- 검색 API 가 준 짧은 요약
    posted_at   DATE,
    sentiment   TEXT,                           -- pos / neg / neu. 아직 분류 전이면 NULL
    is_ad       BOOLEAN NOT NULL DEFAULT FALSE, -- 제목·요약에 광고·체험단 표기가 있는 글. 감성·브리핑 근거로 쓰지 않는다
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_ad BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_documents_keyword ON documents (keyword_id, posted_at DESC);
-- 키워드 안에서만 같은 글을 한 번 저장한다. 기존 행·번호는 유지하고 전체 URL 중복 금지만 해제한다
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_keyword_url ON documents (keyword_id, url);
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_url_key;

-- 인스타그램 중복 조회를 합치기 위한 최소 관측값. 작성자·원문·주소 없이 게시물 번호의 해시만 8일 보관한다.
CREATE TABLE IF NOT EXISTS instagram_observations (
    keyword_id INT NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    media_hash TEXT NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL,
    likes INT NOT NULL DEFAULT 0,
    comments INT NOT NULL DEFAULT 0,
    PRIMARY KEY (keyword_id, media_hash)
);
-- 모든 페이지·시각·식별자가 확인된 조회의 안전 구간. 구간 합집합이 하루 전체를 덮어야 완료다.
CREATE TABLE IF NOT EXISTS instagram_windows (
    keyword_id INT NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (keyword_id, start_at, end_at),
    CHECK (start_at <= end_at)
);
-- 관측 구간을 증명하지 못하는 집계는 숫자를 남기되 점수 입력에서 제외한다. 새 수집은 coverage_version을 남긴다.
UPDATE daily_metrics SET is_complete = FALSE
WHERE source = 'instagram' AND is_complete AND (extra->>'coverage_version') IS DISTINCT FROM 'v2';

-- 날짜별 트렌드 점수(코드가 계산). 화면의 랭킹·단계 표시는 이 테이블을 읽는다.
CREATE TABLE IF NOT EXISTS trend_scores (
    keyword_id     INT  NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    score_date     DATE NOT NULL,
    score          REAL NOT NULL,   -- 0~100
    stage          TEXT NOT NULL,   -- 태동기 / 성장기 / 정점 / 쇠퇴기 / 자료부족(최근 10일 수집 미완료)
    growth_rate    REAL NOT NULL,   -- 언급 증가율 (0.25 = 25% 증가)
    momentum       REAL NOT NULL,   -- 검색 증가율의 변화(가속). 양수면 가속 중
    sentiment_pos  REAL,            -- 최근 긍정 비율 0~1
    n_sources      INT  NOT NULL,   -- 최근 신호가 잡힌 출처 수
    mentions_7d    INT  NOT NULL,   -- 최근 7일 언급 합계
    search_growth  REAL,            -- 검색 관심도 증가율. 언급 증가율과 견주는 교차검증에 쓴다
    signal_check   TEXT,            -- 일치(언급·검색이 함께 오름) / 불일치(한쪽만 오름) / 약함(둘 다 잠잠). 자료부족이면 NULL
    windows        JSONB,           -- 시간창별 판정 {"7d": {...}, "3m": {...} 또는 null}. 3m 은 주 단위로 본 약 3개월 흐름
    PRIMARY KEY (keyword_id, score_date)
);
ALTER TABLE trend_scores ADD COLUMN IF NOT EXISTS search_growth REAL;
ALTER TABLE trend_scores ADD COLUMN IF NOT EXISTS signal_check TEXT;
ALTER TABLE trend_scores ADD COLUMN IF NOT EXISTS windows JSONB;
-- 출처별·계열별 증가율 {"families": {"글": …, "검색": …, "SNS": …}, "sources": {"instagram": {"family", "growth_rate", "total_7d"}, …}}
ALTER TABLE trend_scores ADD COLUMN IF NOT EXISTS by_source JSONB;

-- 날짜별 AI 브리핑. 문장마다 붙은 출처 ID 가 citations 에 풀려 있다.
CREATE TABLE IF NOT EXISTS trend_insights (
    keyword_id   INT  NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    score_date   DATE NOT NULL,
    briefing     TEXT NOT NULL,     -- "문장 [출처:M1]" 형식의 글
    citations    JSONB NOT NULL,    -- {"M1": {"text": ..., "url": ...}, ...}
    generated_by TEXT NOT NULL,     -- llm(모델이 작성, 검사 통과) / template(검사 실패로 코드가 작성)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (keyword_id, score_date)
);

-- 입점업체 프로필. 딸기로 시스템에는 업체 계정이 없어 상품명 앞 [업체명] 을 그대로 키로 쓴다.
CREATE TABLE IF NOT EXISTS vendor_profiles (
    vendor_key    TEXT PRIMARY KEY,             -- 상품명 앞 [업체명] 의 업체명
    categories    TEXT[] NOT NULL DEFAULT '{}', -- 만드는 분류 (keywords.category 와 같은 말을 쓴다)
    region        TEXT,                         -- 소재 지역
    note          TEXT,                         -- 설비·특징 메모(브리핑 근거로 그대로 전달)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 업체 맞춤 추천에 대한 업체의 반응(채택·보류·출시·판매 성과). "추천 → 채택 → 성과" 를 다시 배우는 자료다.
-- 추천을 본 때의 단계·점수를 함께 적어 두어, 나중에 점수가 바뀌어도 "어떤 추천이 채택됐는지" 를 알 수 있게 한다.
CREATE TABLE IF NOT EXISTS trend_adoptions (
    id           BIGSERIAL PRIMARY KEY,
    vendor_key   TEXT NOT NULL,                 -- 업체명 (vendor_profiles 에 없어도 기록할 수 있다)
    keyword_id   INT  NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    action       TEXT NOT NULL,                 -- adopted(상품화하기로 함) / dismissed(하지 않기로 함) / launched(출시함) / sold(판매 성과)
    score_date   DATE,                          -- 그때의 점수 기준일. 점수가 아직 없던 키워드면 NULL
    stage        TEXT,                          -- 그때의 확산 단계
    trend_score  REAL,                          -- 그때의 트렌드 점수
    quantity     INT,                           -- sold 일 때 판매 수량(개)
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trend_adoptions_keyword ON trend_adoptions (keyword_id, created_at DESC);

-- 백테스팅 결과 보관. 같은 자료·같은 설정으로 다시 계산해 볼 수 있게 받은 시계열까지 함께 둔다.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          BIGSERIAL PRIMARY KEY,
    params      JSONB NOT NULL,     -- 시간 단위(date/week), 단계 판정 기준, 비교 기준(이동평균 창·임계값)
    rows        JSONB NOT NULL,     -- 아이템별 결과와 받은 시계열 [{"term":…, "signal":…, "peak":…, "lead_days":…, "series":[…]}]
    summary     JSONB NOT NULL,     -- 평균 리드타임(우리·비교 기준), 헛신호 수
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ────────────────── 2) 자연어 추천·랭킹 (안건 2 · 기획 03) ──────────────────

-- 디저트 업체 카탈로그. 입점사와 비입점 업체를 한 테이블에 두고 is_partner 로 구분한다.
CREATE TABLE IF NOT EXISTS shops (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    region        TEXT NOT NULL,                -- 배송·방문 기준 지역 (예: 서울 강남구)
    address       TEXT,
    is_partner    BOOLEAN NOT NULL DEFAULT FALSE, -- 딸기로 입점사 여부
    is_open       BOOLEAN NOT NULL DEFAULT TRUE,  -- 영업 중 여부. FALSE 면 추천 후보에서 뺀다
    rating        REAL,                         -- 평점 0~5
    review_count  INT NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'manual', -- 데이터 출처 (manual / sample / csv)
    source_url    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, region)
);

-- 업체의 메뉴. 의미 검색은 이 테이블의 embedding 으로 한다.
CREATE TABLE IF NOT EXISTS menus (
    id           BIGSERIAL PRIMARY KEY,
    shop_id      BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    description  TEXT,
    category     TEXT,                          -- 케이크 / 쿠키 / 빵 ...
    price        INT,                           -- 1개 가격(원)
    diet_tags    TEXT[] NOT NULL DEFAULT '{}',  -- 비건 / 글루텐프리 / 저당 / 견과류없음 ...
    storage      TEXT,                          -- 보관 방법: 상온 / 냉장 / 냉동. 모르면 NULL
    embedding    vector(1024),
    UNIQUE (shop_id, name)
);
ALTER TABLE menus ADD COLUMN IF NOT EXISTS storage TEXT;
-- 코사인 거리(<=>) 기준으로 비슷한 벡터를 빨리 찾는 색인
CREATE INDEX IF NOT EXISTS idx_menus_embedding ON menus USING hnsw (embedding vector_cosine_ops);

-- 상품(메뉴)별 단체주문 공급 조건. 단체주문 추천에서 "절대 어기면 안 되는 조건" 의 기준이다.
-- 공급 조건은 수시로 바뀌므로 "언제 확인한 값인지" 를 함께 둔다(최신성 규칙은 app/reco/capacity.py).
--
-- 이전 구조(업체 단위, 기본 키 shop_id)의 DB 는 아래 두 DO 묶음이 한 번만 상품 단위로 옮긴다:
--   ① 옛 테이블의 이름을 바꿔 비켜 두고  ② 새 테이블을 만든 뒤  ③ 업체의 조건을 그 업체의 메뉴마다 복사하고 옛 테이블을 지운다.
-- 옮긴 값은 확인 날짜를 알 수 없어 '확인 전(unverified)' 이 된다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'supplier_capacity' AND column_name = 'shop_id')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = current_schema() AND table_name = 'supplier_capacity' AND column_name = 'menu_id') THEN
        ALTER TABLE supplier_capacity RENAME TO supplier_capacity_by_shop;
        -- 기본 키 이름도 비켜 두어야 새 테이블이 같은 이름을 쓸 수 있다
        ALTER INDEX IF EXISTS supplier_capacity_pkey RENAME TO supplier_capacity_by_shop_pkey;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS supplier_capacity (
    menu_id               BIGINT PRIMARY KEY REFERENCES menus(id) ON DELETE CASCADE,
    shop_id               BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    moq                   INT,       -- 최소 주문 수량
    max_qty_per_day       INT,       -- 하루 최대 생산량
    lead_time_days        INT,       -- 주문 뒤 준비에 필요한 날수
    individual_packaging  BOOLEAN,   -- 개별 포장 가능
    gift_packaging        BOOLEAN,   -- 선물 포장 가능
    tax_invoice           BOOLEAN,   -- 세금계산서 발행 가능
    delivery_areas        TEXT[] NOT NULL DEFAULT '{}',  -- 업체 지역 밖으로 보내 줄 수 있는 지역 낱말. '전국' 이면 어디든
    last_verified_at      DATE,      -- 공급자가 이 조건을 확인해 준 날짜. 확인한 적이 없으면 NULL
    valid_until           DATE,      -- 이 날짜까지는 다시 묻지 않고 믿는다. 지나면 "공급자 재확인" 대상이다
    confirmation_status   TEXT NOT NULL DEFAULT 'unverified',  -- confirmed(확인됨) / unverified(확인 전) / pending(재확인 요청 중)
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_supplier_capacity_shop ON supplier_capacity (shop_id);

DO $$
BEGIN
    IF to_regclass('supplier_capacity_by_shop') IS NOT NULL THEN
        INSERT INTO supplier_capacity (menu_id, shop_id, moq, max_qty_per_day, lead_time_days,
                                       individual_packaging, tax_invoice, updated_at)
        SELECT m.id, old.shop_id, old.moq, old.max_qty_per_day, old.lead_time_days,
               old.individual_packaging, old.tax_invoice, old.updated_at
        FROM supplier_capacity_by_shop old
        JOIN menus m ON m.shop_id = old.shop_id
        ON CONFLICT (menu_id) DO NOTHING;
        DROP TABLE supplier_capacity_by_shop;
    END IF;
END $$;

-- 들어온 추천 요청(원문과 구조화한 조건). 나중에 추천 품질을 학습·평가할 자료다.
CREATE TABLE IF NOT EXISTS group_order_requests (
    id          BIGSERIAL PRIMARY KEY,
    raw_text    TEXT NOT NULL,
    slots       JSONB NOT NULL,     -- 구조화한 조건(목적·인원·예산·수령일·지역·포장·보관·세금계산서 …)
    status      TEXT NOT NULL,      -- ok / reask / empty
    audit       JSONB,              -- ok: 순위 감사 기록(가중치 묶음·입점사 가산 뒤 상위 보존율), empty: 어느 조건에서 막혔는지
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE group_order_requests ADD COLUMN IF NOT EXISTS audit JSONB;

-- 요청마다 제안한 후보(견적 옵션)와 점수 내역. 순위가 왜 그렇게 나왔는지 나중에 검증하는 감사 기록이다.
CREATE TABLE IF NOT EXISTS quote_options (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT NOT NULL REFERENCES group_order_requests(id) ON DELETE CASCADE,
    round           INT  NOT NULL DEFAULT 1,   -- 제안 회차. 공급자가 거절해 대체 공급자를 다시 찾으면 2, 3 … 으로 늘어난다
    shop_id         BIGINT NOT NULL,
    menu_id         BIGINT NOT NULL,
    rank            INT  NOT NULL,
    score           REAL NOT NULL,
    score_detail    JSONB NOT NULL,   -- 신호별 점수와 입점사 가산
    quantity        INT,              -- 견적 수량(개). 수량·인원을 말하지 않은 문의면 NULL
    unit_price      INT,              -- 1개 가격(원). 제안한 때의 메뉴 가격
    total_price     INT,              -- 합계(원) = 수량 × 1개 가격. 코드가 계산한다
    capacity_check  TEXT,             -- 단체주문의 공급 조건 확인 상태: auto / reconfirm / confirm. 단체주문이 아니면 NULL
    reason          TEXT,             -- 추천 사유 글
    generated_by    TEXT,             -- 추천 사유를 만든 방법: llm(검사 통과) / template(코드가 만든 글)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS round INT NOT NULL DEFAULT 1;
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS quantity INT;
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS unit_price INT;
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS total_price INT;
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS capacity_check TEXT;
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS generated_by TEXT;
-- 추천 당시의 근거와 조건을 보존한다. 옛 행의 빈 객체는 근거를 복원할 수 없다는 표시다.
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS citations JSONB NOT NULL DEFAULT '{}';
ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS snapshot JSONB NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_quote_options_request ON quote_options (request_id, round, rank);

-- 고객에게 보낸 제안서(여러 옵션을 묶은 글)와 그 결과. 제안 자동화의 품질을 재는 자료다.
CREATE TABLE IF NOT EXISTS proposal_logs (
    id              BIGSERIAL PRIMARY KEY,
    request_id      BIGINT NOT NULL REFERENCES group_order_requests(id) ON DELETE CASCADE,
    option_ids      BIGINT[] NOT NULL,   -- 제안서에 담은 quote_options 번호(순위 순)
    body            TEXT NOT NULL,       -- 제안서 초안 글. 견적 줄은 코드가 계산한 값이다
    outcome         TEXT,                -- adopted(채택) / not_adopted(미채택). 아직 모르면 NULL
    outcome_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ
);

-- 제안 옵션에 대한 반응. 고객의 선택·거절, 공급자의 수락·거절, 주문 확정을 남긴다. 순위 학습의 정답 자료가 된다.
CREATE TABLE IF NOT EXISTS order_conversion_logs (
    id               BIGSERIAL PRIMARY KEY,
    quote_option_id  BIGINT NOT NULL REFERENCES quote_options(id) ON DELETE CASCADE,
    action           TEXT NOT NULL,   -- selected / rejected (고객) · supplier_accepted / supplier_declined (공급자) · ordered (주문 확정)
    reason           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_conversion_option ON order_conversion_logs (quote_option_id);
-- 주문 번호는 외부 주문과 연결하는 값, event_key는 같은 전송의 재시도 중복을 막는 값이다.
ALTER TABLE order_conversion_logs ADD COLUMN IF NOT EXISTS order_ref TEXT;
ALTER TABLE order_conversion_logs ADD COLUMN IF NOT EXISTS event_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_event_key ON order_conversion_logs (quote_option_id, event_key)
    WHERE event_key IS NOT NULL;

-- 주문 뒤의 납품·CS 결과. "실제로 거래가 끝까지 됐는가" 를 가리는 자료다.
CREATE TABLE IF NOT EXISTS delivery_cs_logs (
    id               BIGSERIAL PRIMARY KEY,
    quote_option_id  BIGINT NOT NULL REFERENCES quote_options(id) ON DELETE CASCADE,
    event            TEXT NOT NULL,   -- delivered(납품 완료) / delayed(지연) / claim(클레임) / reorder_intent(재주문 의향)
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_delivery_cs_option ON delivery_cs_logs (quote_option_id);
ALTER TABLE delivery_cs_logs ADD COLUMN IF NOT EXISTS order_ref TEXT;
ALTER TABLE delivery_cs_logs ADD COLUMN IF NOT EXISTS event_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_event_key ON delivery_cs_logs (quote_option_id, event_key)
    WHERE event_key IS NOT NULL;

-- 제안 옵션별 거래 결과를 한 줄로 모은 보기(view). 순위 학습과 KPI 집계가 이것을 읽는다.
-- 최종 의사를 기준으로 집계한다. 수락 후 거절·거절 후 수락을 과거 사건의 존재 여부와 혼동하지 않는다.
-- 거래 가능 정답에는 제약 통과 스냅샷·최종 수락 뒤의 주문·그 주문의 납품 기록이 모두 필요하다.
CREATE OR REPLACE VIEW quote_outcomes AS
SELECT q.id AS quote_option_id, q.request_id, q.round, q.rank, q.shop_id, q.menu_id, q.score, q.capacity_check,
       COALESCE(customer.action = 'selected', FALSE)          AS selected,
       COALESCE(customer.action = 'rejected', FALSE)          AS rejected,
       COALESCE(supplier.action = 'supplier_accepted', FALSE) AS supplier_accepted,
       COALESCE(supplier.action = 'supplier_declined', FALSE) AS supplier_declined,
       COALESCE(supplier.action = 'supplier_accepted' AND ordered.id > supplier.id
                AND ordered.order_ref IS NOT NULL, FALSE)   AS ordered,
       delivery.completed AS delivered,
       EXISTS (SELECT 1 FROM delivery_cs_logs d WHERE d.quote_option_id = q.id AND d.event = 'claim')          AS claimed,
       EXISTS (SELECT 1 FROM delivery_cs_logs d WHERE d.quote_option_id = q.id AND d.event = 'reorder_intent') AS reorder_intent,
       (q.snapshot @> '{"constraints_passed":true}'::jsonb
        AND COALESCE(supplier.action = 'supplier_accepted' AND ordered.id > supplier.id, FALSE)
        AND delivery.completed) AS tradable,
       q.created_at
FROM quote_options q
LEFT JOIN LATERAL (SELECT action FROM order_conversion_logs WHERE quote_option_id = q.id
                  AND action IN ('selected','rejected') ORDER BY id DESC LIMIT 1) customer ON TRUE
LEFT JOIN LATERAL (SELECT id, action FROM order_conversion_logs WHERE quote_option_id = q.id
                  AND action IN ('supplier_accepted','supplier_declined') ORDER BY id DESC LIMIT 1) supplier ON TRUE
LEFT JOIN LATERAL (SELECT id, order_ref, created_at FROM order_conversion_logs WHERE quote_option_id = q.id
                  AND action = 'ordered' ORDER BY id DESC LIMIT 1) ordered ON TRUE
CROSS JOIN LATERAL (SELECT EXISTS (
    SELECT 1 FROM delivery_cs_logs d WHERE d.quote_option_id = q.id AND d.event = 'delivered'
      AND d.order_ref = ordered.order_ref AND d.created_at >= ordered.created_at
) AS completed) delivery;

-- ───────────────────────────── 3) 실행 기록 ─────────────────────────────

-- 그래프의 단계(노드)별 실행 기록. 어느 단계가 느린지·실패했는지와 검사 통과율을 여기서 센다.
CREATE TABLE IF NOT EXISTS agent_runs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,       -- trend / reco
    node        TEXT NOT NULL,       -- 단계 이름
    ok          BOOLEAN NOT NULL,
    elapsed_ms  INT NOT NULL,
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs (created_at DESC);

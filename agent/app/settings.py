"""환경 변수를 읽어 설정 객체 하나로 모은다.

쓰는 곳: 다른 모든 모듈이 `from app.settings import settings` 로 가져다 쓴다.
값이 오는 곳: 쿠버네티스에서는 ConfigMap(agent-config)과 Secret(agent-secret)이 환경 변수로 넣어 준다.
            내 컴퓨터에서 시험할 때는 이 폴더의 .env 파일이나 셸의 환경 변수를 읽는다.
기본값: 내 컴퓨터에서 포트 포워딩으로 시험하는 상황(localhost)에 맞춰 두었다.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """설정 항목 모음. 환경 변수 이름은 아래 속성 이름과 같다(대문자)."""

    # .env 파일이 있으면 읽고, 모르는 환경 변수는 무시한다
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── 데이터베이스 ──
    # 형식: postgresql://계정:비밀번호@주소:포트/DB이름
    DATABASE_URL: str = "postgresql://agent:agent@localhost:5432/agent"

    # ── LLM 게이트웨이(LiteLLM) ──
    # 에이전트는 모델(지금은 Claude API)을 직접 부르지 않고 항상 게이트웨이를 거친다. Claude API 키는 게이트웨이만 갖는다.
    LITELLM_BASE_URL: str = "http://localhost:4000"
    LITELLM_API_KEY: str = "sk-local-test"
    # 게이트웨이에 등록한 "이름표". 실제 모델이 무엇인지는 게이트웨이 설정이 정한다.
    MODEL_CHAT_FAST: str = "chat-fast"    # 분류·조건 뽑기처럼 짧고 잦은 일
    MODEL_CHAT_SMART: str = "chat-smart"  # 브리핑·추천 사유처럼 글을 쓰는 일
    MODEL_EMBED: str = "embed"            # 글 → 벡터
    EMBED_DIM: int = 1024                 # 임베딩 벡터 길이. sql/schema.sql 의 vector(1024) 와 같아야 한다
    LLM_TIMEOUT_SECONDS: float = 120.0    # LLM 응답을 기다리는 최대 시간(초)
    # 직접 서빙하는 모델 서버(vLLM 등)에 그대로 넘길 추가 옵션(JSON 글자). 비워 두면 보내지 않는다.
    # 예: 생각 과정을 끄려면 {"chat_template_kwargs": {"enable_thinking": false}}
    # Claude API 를 쓸 때는 비워 둔다 — 생각 깊이는 게이트웨이의 모델 표(k8s/22-litellm.yaml 의 output_config)에서 정한다.
    LLM_EXTRA_BODY: str = ""

    # ── 별도 Langfuse 서버의 AI 처리 추적 ──
    # 코드의 기본값은 꺼짐이고, 배포에서는 site.env 의 값으로 켠다(새 설치의 견본은 켜짐).
    # 주소는 ConfigMap, 프로젝트 키 두 개는 agent-secret 에만 넣는다.
    # 켜도 문의·근거·응답 원문은 보내지 않는다(app/observability.py 의 허용 목록).
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_BASE_URL: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""

    # ── 이 API 를 부르는 쪽 확인 ──
    # 부르는 쪽(딸기로 백엔드)이 요청 헤더 X-AI-Key 에 넣어 보내야 하는 값. 비어 있으면 검사하지 않는다(내 컴퓨터 시험용).
    AI_API_KEY: str = ""

    # ── 네이버 검색·데이터랩 API (수집 작업) ──
    # 비어 있으면 수집 작업이 아무것도 하지 않고 끝난다.
    NAVER_CLIENT_ID: str = ""
    NAVER_CLIENT_SECRET: str = ""

    # ── 인스타그램 Graph API (수집 작업) ──
    # 인스타그램 비즈니스·크리에이터 계정 번호와 접근 토큰. 비어 있으면 인스타그램 수집 작업이 아무것도 하지 않고 끝난다.
    INSTAGRAM_USER_ID: str = ""
    INSTAGRAM_ACCESS_TOKEN: str = ""
    INSTAGRAM_GRAPH_VERSION: str = "v27.0"   # Graph API 버전. Meta 가 옛 버전을 닫으면 올린다
    INSTAGRAM_MAX_HASHTAGS: int = 30         # 계정 하나가 7일 동안 조회할 수 있는 해시태그 수(Meta 의 한도)

    # ── RSS·웹 피드 (수집 작업) ──
    # 읽을 피드 목록(JSON 글자): [{"name": "매체이름", "url": "https://…"}]. 주소에 {keyword} 가 있으면 키워드마다 그 자리에 넣어 부른다.
    # 비어 있으면 피드 수집 작업이 아무것도 하지 않고 끝난다. 피드를 넣기 전에 그 사이트의 약관과 robots.txt 를 확인한다.
    RSS_FEEDS: str = "[]"
    # 웹 주소를 읽을 때 붙이는 이름표. 사이트 관리자가 연락할 수 있게 운영 주소를 넣어 둔다(예: "ddalgiro-ai-agent/0.2 (+https://ai.example.com)")
    CRAWL_USER_AGENT: str = "ddalgiro-ai-agent/0.2"
    CRAWL_MIN_INTERVAL_SECONDS: float = 2.0  # 같은 사이트를 다시 부르기까지 쉬는 시간(초)

    # ── 추천·랭킹 ──
    RECO_CANDIDATES: int = 30        # 의미 검색으로 가져올 후보 수
    RECO_RESULTS: int = 5            # 화면에 돌려줄 결과 수
    PARTNER_BOOST_CAP: float = 0.03  # 입점사 가산의 상한(점수는 0~1). 적합도 순서를 뒤집지 못하게 작게 둔다
    RECO_ASK_STORAGE: bool = True    # 단체주문에서 보관 방법(상온·냉장)을 말하지 않았으면 되물을지

    # ── 공급 조건의 최신성 (단체주문) ──
    # 공급자가 조건을 확인해 준 날부터 며칠 동안 다시 묻지 않고 믿을지. 지나면 "공급자 재확인" 으로 표시한다
    CAPACITY_VALID_DAYS: int = 30
    # 대량 주문 기준: 수량이 이 값 이상이거나 하루 최대 생산량의 이 비율 이상이면, 조건이 최신이어도 견적 전에 공급자 확인을 받게 한다
    CONFIRM_QUANTITY: int = 100
    CONFIRM_LOAD_RATIO: float = 0.5


settings = Settings()

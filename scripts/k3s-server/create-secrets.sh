#!/usr/bin/env bash
# 쿠버네티스 Secret(비밀값 보관함) 3개를 만든다. 비밀번호와 키는 여기서 무작위로 만들어 Secret 에만 둔다.
#
# 실행 위치: K3s 서버(AWS EC2), 네임스페이스를 만든 뒤(k8s/00-namespaces.yaml).
# 쓰는 법:   bash scripts/k3s-server/create-secrets.sh
#            Claude API 키(sk-ant- 로 시작, Claude Console 에서 만든 것)를 물어보면 붙여 넣는다. 화면에는 보이지 않는다.
#            네이버 API 키는 있으면 넣고, 없으면 그냥 Enter(나중에 --naver 로 넣을 수 있다).
# 다시 실행: 이미 있는 Secret 은 건드리지 않는다 — DB 비밀번호가 바뀌면 이미 만들어진 DB 와 어긋나기 때문이다.
#            Claude API 키만 바꾸려면(새로 발급·유출 의심): bash scripts/k3s-server/create-secrets.sh --anthropic
#            네이버 키만 넣거나 바꾸려면:  bash scripts/k3s-server/create-secrets.sh --naver
#            인스타그램 계정 번호·토큰을 넣거나 바꾸려면: bash scripts/k3s-server/create-secrets.sh --instagram
#            (선택) 노코드 도구 Langflow 의 Secret 을 만들려면: bash scripts/k3s-server/create-secrets.sh --langflow
#            (선택) Langflow 가 게이트웨이를 부를 전용 키를 만들려면(30일 한도 달러, 기본 20):
#                                                  bash scripts/k3s-server/create-secrets.sh --langflow-key 20
# 만든 값 보기(예: 백엔드에 알려 줄 AI_API_KEY):
#            kubectl -n ai-app get secret agent-secret -o jsonpath='{.data.AI_API_KEY}' | base64 -d; echo
# ⚠️ 출력된 값은 채팅·메일·문서에 붙이지 않는다. 필요한 곳(백엔드 서버의 비밀값 파일)에만 직접 옮긴다.
# ⚠️ Claude API 키가 새면 남이 우리 요금으로 모델을 쓴다. 의심되면 Claude Console 에서 그 키를 지우고 새 키를 --anthropic 으로 넣는다.
set -euo pipefail

# 무작위 16진수 글자 만들기. 16진수만 쓰는 이유: DB 접속 주소(URL) 안에 넣어도 깨지지 않는다.
random_hex() { openssl rand -hex "$1"; }

# 무작위 32바이트를 URL 에 써도 되는 base64 글자(44자)로 만든다. Langflow 의 암호화 열쇠(Fernet 키)가 이 형식이어야 한다.
# 16진수(random_hex)로 만들면 Langflow 가 키를 저장할 때 "Fernet key must be 32 url-safe base64-encoded bytes" 로 실패한다.
random_fernet_key() { openssl rand -base64 32 | tr '+/' '-_'; }

# Secret 이 이미 있는지 확인
exists() { kubectl -n "$1" get secret "$2" >/dev/null 2>&1; }

# 화면에 보이지 않게 한 줄 입력받기 (키를 붙여 넣을 때 화면·기록에 남지 않게)
ask_hidden() {
  local prompt="$1" value
  read -r -s -p "$prompt" value
  echo >&2
  printf '%s' "$value"
}

# Secret 의 값 하나를 바꾸고(나머지 값은 그대로), 그 값을 쓰는 워크로드를 다시 시작한다
patch_value() {
  local namespace="$1" secret="$2" key="$3" value="$4"
  kubectl -n "$namespace" patch secret "$secret" --type merge \
    -p "{\"stringData\":{\"$key\":\"$value\"}}" >/dev/null
}

case "${1:-}" in
  --naver)
    # 네이버 키만 넣거나 바꾼다. 정기 작업(CronJob)은 다음 실행 때 새 값을 읽으므로 재시작할 것이 없다
    client_id="$(ask_hidden '네이버 Client ID: ')"
    client_secret="$(ask_hidden '네이버 Client Secret: ')"
    patch_value ai-app agent-secret NAVER_CLIENT_ID "$client_id"
    patch_value ai-app agent-secret NAVER_CLIENT_SECRET "$client_secret"
    echo "agent-secret 의 네이버 키를 바꿨습니다. 다음 수집 작업부터 쓰입니다."
    exit 0
    ;;
  --instagram)
    # 인스타그램 수집에 쓰는 계정 번호와 접근 토큰을 넣거나 바꾼다. 토큰은 기한이 있어 만료 전에 다시 넣어야 한다
    ig_user_id="$(ask_hidden '인스타그램 비즈니스 계정 번호(IG User ID): ')"
    ig_token="$(ask_hidden '접근 토큰(Access Token): ')"
    patch_value ai-app agent-secret INSTAGRAM_USER_ID "$ig_user_id"
    patch_value ai-app agent-secret INSTAGRAM_ACCESS_TOKEN "$ig_token"
    echo "agent-secret 의 인스타그램 값을 바꿨습니다. 다음 수집 작업부터 쓰입니다."
    exit 0
    ;;
  --anthropic)
    # Claude API 키만 바꾼다. LiteLLM 은 시작할 때 값을 읽으므로 다시 시작해야 새 키를 쓴다
    anthropic_key="$(ask_hidden 'Claude API 키(sk-ant-…): ')"
    if [ -z "$anthropic_key" ]; then
      echo "키가 비어 있어 바꾸지 않았습니다." >&2
      exit 1
    fi
    patch_value ai-llm litellm-secret ANTHROPIC_API_KEY "$anthropic_key"
    kubectl -n ai-llm rollout restart deployment/litellm
    echo "litellm-secret 의 Claude API 키를 바꾸고 LiteLLM 을 다시 시작했습니다."
    exit 0
    ;;
  --langflow)
    # 노코드 도구 Langflow 가 쓰는 Secret 을 만든다(ai-app/langflow-secret). 값은 모두 여기서 무작위로 만든다.
    #   LANGFLOW_SECRET_KEY: 화면에서 저장한 키(변수)를 암호화하는 열쇠
    #   LANGFLOW_SUPERUSER_PASSWORD: 관리자(admin) 비밀번호. 읽는 법은 아래 안내를 따른다
    # 이미 있으면 건드리지 않는다 — LANGFLOW_SECRET_KEY 가 바뀌면 Langflow 에 저장해 둔 키를 풀 수 없게 되기 때문이다
    if exists ai-app langflow-secret; then
      echo "ai-app/langflow-secret 은 이미 있어 그대로 둡니다."
    else
      kubectl -n ai-app create secret generic langflow-secret \
        --from-literal=LANGFLOW_SECRET_KEY="$(random_fernet_key)" \
        --from-literal=LANGFLOW_SUPERUSER_PASSWORD="$(random_hex 16)"
      echo "관리자 비밀번호 보기: kubectl -n ai-app get secret langflow-secret -o jsonpath='{.data.LANGFLOW_SUPERUSER_PASSWORD}' | base64 -d; echo"
    fi
    exit 0
    ;;
  --langflow-key)
    # Langflow 가 게이트웨이(LiteLLM)를 부를 때 쓰는 전용 키를 만들어 한 번 보여 준다.
    # 전용 키를 따로 두는 이유: Langflow 에서 쓴 요금을 따로 세고, 30일 한도를 넘으면 Langflow 의 호출만 막기 위해서다.
    #   이 키로는 이름표 chat-fast·chat-smart·embed 만 부를 수 있고, 게이트웨이 설정은 바꿀 수 없다.
    # 둘째 인자: 30일 한도(달러, 기본 20). 같은 이름(langflow)의 옛 키가 있으면 지우고 새로 만든다(옛 키는 바로 못 쓰게 된다).
    budget="${2:-20}"
    case "$budget" in
      ''|*[!0-9.]*) echo "30일 한도는 숫자(달러)로 적습니다. 예: $0 --langflow-key 20" >&2; exit 2 ;;
    esac
    # 게이트웨이 파드 안에서 관리 API 를 부른다 — 관리 키(LITELLM_MASTER_KEY)가 파드 밖으로 나오지 않게 하기 위해서다
    kubectl -n ai-llm exec -i deployment/litellm -c litellm -- env KEY_BUDGET="$budget" python - <<'PY'
import json, os, urllib.error, urllib.request

BASE = "http://localhost:4000"
HEADERS = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json"}


def call(path, body):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


# 같은 이름의 옛 키를 지운다. 처음 만들 때는 지울 것이 없어 오류가 나는데, 그 경우는 그냥 넘어간다
try:
    call("/key/delete", {"key_aliases": ["langflow"]})
except urllib.error.HTTPError:
    pass
made = call("/key/generate", {
    "key_alias": "langflow",                             # 게이트웨이 관리 화면에 보이는 이름
    "models": ["chat-fast", "chat-smart", "embed"],      # 이 키로 부를 수 있는 이름표
    "max_budget": float(os.environ["KEY_BUDGET"]),       # 한도(달러). 넘으면 이 키의 호출이 거절된다
    "budget_duration": "30d",                            # 30일마다 쓴 금액을 0 으로 되돌린다
})
print("Langflow 전용 게이트웨이 키 (30일 한도 $" + os.environ["KEY_BUDGET"] + "):")
print(made["key"])
PY
    echo "⚠️ 위 키는 다시 볼 수 없습니다. Langflow 화면의 전역 변수(Global Variables)에 바로 넣고, 채팅·메일·문서에 붙이지 않습니다." >&2
    exit 0
    ;;
  "") ;;
  *)
    echo "쓰는 법: $0 [--anthropic | --naver | --instagram | --langflow | --langflow-key [30일 한도]]" >&2
    exit 2
    ;;
esac

# ── 1) DB 비밀번호 (ai-data/postgres-secret) ──
if exists ai-data postgres-secret; then
  echo "ai-data/postgres-secret 은 이미 있어 그대로 둡니다."
else
  kubectl -n ai-data create secret generic postgres-secret \
    --from-literal=POSTGRES_PASSWORD="$(random_hex 24)" \
    --from-literal=LITELLM_DB_PASSWORD="$(random_hex 24)" \
    --from-literal=AGENT_DB_PASSWORD="$(random_hex 24)"
fi
# 아래 두 Secret 의 DB 접속 주소에 넣기 위해 방금 만든(또는 이미 있던) 비밀번호를 읽는다
litellm_db_password="$(kubectl -n ai-data get secret postgres-secret -o jsonpath='{.data.LITELLM_DB_PASSWORD}' | base64 -d)"
agent_db_password="$(kubectl -n ai-data get secret postgres-secret -o jsonpath='{.data.AGENT_DB_PASSWORD}' | base64 -d)"

# ── 2) LLM 게이트웨이 (ai-llm/litellm-secret) ──
if exists ai-llm litellm-secret; then
  echo "ai-llm/litellm-secret 은 이미 있어 그대로 둡니다."
else
  # 환경 변수 ANTHROPIC_API_KEY 로 미리 줬으면 묻지 않는다
  anthropic_key="${ANTHROPIC_API_KEY:-$(ask_hidden 'Claude API 키(sk-ant-…, Claude Console 에서 만든 것): ')}"
  if [ -z "$anthropic_key" ]; then
    echo "Claude API 키가 비어 있습니다. 키 발급(문서 02 — Claude API 키와 모델)을 먼저 마치세요." >&2
    exit 1
  fi
  kubectl -n ai-llm create secret generic litellm-secret \
    --from-literal=LITELLM_MASTER_KEY="sk-$(random_hex 24)" \
    --from-literal=DATABASE_URL="postgresql://litellm:${litellm_db_password}@postgres.ai-data.svc:5432/litellm" \
    --from-literal=ANTHROPIC_API_KEY="$anthropic_key"
fi
litellm_master_key="$(kubectl -n ai-llm get secret litellm-secret -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)"

# ── 3) 에이전트 (ai-app/agent-secret) ──
if exists ai-app agent-secret; then
  echo "ai-app/agent-secret 은 이미 있어 그대로 둡니다."
else
  client_id="${NAVER_CLIENT_ID-$(ask_hidden '네이버 Client ID (없으면 Enter): ')}"
  client_secret="${NAVER_CLIENT_SECRET-$(ask_hidden '네이버 Client Secret (없으면 Enter): ')}"
  kubectl -n ai-app create secret generic agent-secret \
    --from-literal=DATABASE_URL="postgresql://agent:${agent_db_password}@postgres.ai-data.svc:5432/agent" \
    --from-literal=LITELLM_API_KEY="$litellm_master_key" \
    --from-literal=AI_API_KEY="$(random_hex 32)" \
    --from-literal=NAVER_CLIENT_ID="$client_id" \
    --from-literal=NAVER_CLIENT_SECRET="$client_secret"
fi

echo "끝났습니다. Secret 목록:"
kubectl get secret -A --field-selector type=Opaque | grep -E 'postgres-secret|litellm-secret|agent-secret' || true

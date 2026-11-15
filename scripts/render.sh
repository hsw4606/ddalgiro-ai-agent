#!/usr/bin/env bash
# site.env 의 값을 k8s/ 매니페스트의 ${이름} 자리에 채워 rendered/ 에 만든다.
#
# 쓰는 법(저장소 루트에서):  bash scripts/render.sh            (site.env 를 읽는다)
#                           bash scripts/render.sh 다른.env    (다른 값 파일로)
# 그다음 rendered/ 의 파일을 문서 순서대로 kubectl apply 한다.
# k8s/ 의 원본은 고치지 않는다 — 값이 바뀌면 site.env 를 고치고 이 스크립트를 다시 실행한다.
# 평소에는 k8s/ 바로 아래의 매니페스트만 만든다. 값 파일에 DGX_SPARK_IP 가 있으면(DGX Spark 를 붙인 경우)
# k8s/dgx-spark/ 의 매니페스트도 함께 만든다(docs/08-추후-도입/01-DGX-Spark-도입.md).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/site.env}"
OUT="$ROOT/rendered"
# 채워 넣을 값의 이름. site.env.example 의 항목과 같아야 한다.
NAMES=(AI_DOMAIN ACME_EMAIL CLUSTER_ISSUER ALLOW_IP_1 ALLOW_IP_2 CHAT_FAST_MODEL CHAT_SMART_MODEL AGENT_IMAGE TEI_IMAGE LANGFUSE_ENABLED LANGFUSE_BASE_URL LANGFUSE_HEALTH_TARGETS LANGFUSE_METRICS_TARGETS)
# DGX Spark 를 붙였을 때만 필요한 값의 이름
DGX_NAMES=(DGX_SPARK_IP DGX_SPARK_PORT VLLM_MODEL)

if [ ! -f "$ENV_FILE" ]; then
  echo "값 파일이 없습니다: $ENV_FILE  (cp site.env.example site.env 후 값을 고치세요)" >&2
  exit 1
fi
# 값 파일을 읽는다. 파일 안의 각 줄(이름=값)이 변수로 들어온다
# shellcheck disable=SC1090
. "$ENV_FILE"
# 기존 값 파일에는 추적 설정이 없을 수 있다. 별도 서버가 준비되기 전에는 켜지 않는다.
LANGFUSE_ENABLED="${LANGFUSE_ENABLED:-false}"
LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-}"
case "$LANGFUSE_ENABLED" in
  true|false) ;;
  *) echo "LANGFUSE_ENABLED 는 true 또는 false 로 적습니다." >&2; exit 1 ;;
esac
if [ "$LANGFUSE_ENABLED" = true ] && [[ ! "$LANGFUSE_BASE_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$ ]]; then
  echo "추적을 켜려면 LANGFUSE_BASE_URL 에 별도 서버의 HTTPS 주소가 필요합니다." >&2
  exit 1
fi
# 준비 전인 관측 서버를 장애로 판단하지 않도록 추적을 켰을 때만 검사한다.
LANGFUSE_HEALTH_TARGETS='[]'
LANGFUSE_METRICS_TARGETS='[]'
if [ "$LANGFUSE_ENABLED" = true ]; then
  LANGFUSE_HEALTH_TARGETS="[\"${LANGFUSE_BASE_URL%/}/api/public/health\"]"
  metrics_host="${LANGFUSE_BASE_URL#https://}"
  LANGFUSE_METRICS_TARGETS="[\"${metrics_host%/}\"]"
fi

# 값을 채울 매니페스트 목록. DGX_SPARK_IP 가 있을 때만 DGX Spark 용 값과 매니페스트를 더한다
FILES=("$ROOT"/k8s/*.yaml)
if [ -n "${DGX_SPARK_IP:-}" ]; then
  NAMES+=("${DGX_NAMES[@]}")
  FILES+=("$ROOT"/k8s/dgx-spark/*.yaml)
fi

# 값마다 sed 치환 식을 하나씩 만든다. 빈 값이 있으면 멈춘다
SED_ARGS=()
for name in "${NAMES[@]}"; do
  value="${!name:-}"
  if [ -z "$value" ] && [ "$name" != LANGFUSE_BASE_URL ]; then
    echo "$ENV_FILE 에 $name 값이 없습니다." >&2
    exit 1
  fi
  # 값 안의 \ & | 가 sed 의 특수 문자로 읽히지 않게 앞에 \ 를 붙인다
  escaped="$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')"
  SED_ARGS+=(-e "s|\${$name}|$escaped|g")
done

mkdir -p "$OUT"
rm -f "$OUT"/*.yaml
for file in "${FILES[@]}"; do
  sed "${SED_ARGS[@]}" "$file" > "$OUT/$(basename "$file")"
done

# 채워지지 않은 ${대문자이름} 이 남았으면 이름이 틀린 것이므로 알린다
if grep -nE '\$\{[A-Z0-9_]+\}' "$OUT"/*.yaml; then
  echo "위 자리가 채워지지 않았습니다. k8s/ 의 이름과 이 스크립트의 NAMES 를 확인하세요." >&2
  echo "DGX_SPARK_IP·VLLM_MODEL 자리라면 site.env 의 DGX Spark 값(주석 처리된 세 줄)을 채워야 합니다." >&2
  exit 1
fi
rendered=("$OUT"/*.yaml)
echo "만들었습니다: $OUT (${#rendered[@]}개 파일)"

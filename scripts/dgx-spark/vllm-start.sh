#!/usr/bin/env bash
# DGX Spark 에서 vLLM(모델 서버)을 컨테이너로 띄운다. OpenAI 호환 API 가 http://<Spark IP>:<포트>/v1 에 열린다.
# 지금 구성(대화 모델 = Claude API)에서는 쓰지 않는다. DGX Spark 를 들일 때 쓴다: docs/08-추후-도입/01-DGX-Spark-도입.md
#
# 실행 위치: DGX Spark. 같은 폴더에 vllm.env 가 있어야 한다(vllm.env.example 참고).
# 쓰는 법:   bash vllm-start.sh          (이미 떠 있으면 내리고 새로 띄운다)
# 로그:      docker logs --tail 50 --follow vllm
# 내리기:    docker stop vllm            (다시 켜기: docker start vllm)
# 준비 확인: curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health   → 200 (처음에는 몇 분 걸린다)
#
# 바탕: NVIDIA 의 DGX Spark vLLM 플레이북 스크립트 sync-vllm-spark-128gb-qwen38-27b.sh (2026-10-19 확인분).
#       모델 실행 옵션(vllm serve 뒤의 값)은 그 스크립트와 같다.
# 그 스크립트와 다른 점 — 이 기계를 "항상 켜 두는 모델 서버" 로 쓰기 위한 것이다:
#   1) --host 0.0.0.0        다른 기계에 있는 K3s 서버(게이트웨이)가 부를 수 있게 모든 네트워크 카드에서 듣는다(원본은 127.0.0.1).
#   2) VLLM_API_KEY          키 없이는 /v1 API 를 부를 수 없게 한다(원본에는 없다).
#   3) --restart unless-stopped  기계를 껐다 켜도 컨테이너가 다시 뜬다(원본은 NVIDIA Sync 가 켜고 끈다).
# ⚠️ 1) 때문에 방화벽으로 "K3s 서버만 이 포트에 들어올 수 있게" 반드시 막는다(추후 도입 문서의 방화벽 단계).
# ⚠️ --gpu-memory-utilization 0.60 은 올리지 않는다. NVIDIA 레시피 기록: 0.85 에서 시작 중 멈췄고 0.70 에서도 기계가 내려간 적이 있다.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/vllm.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "값 파일이 없습니다: $ENV_FILE  (cp vllm.env.example vllm.env 후 값을 채우세요)" >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$ENV_FILE"

NAME="vllm"
PORT="${VLLM_PORT:-8000}"
# 내려받은 모델을 두는 곳. 컨테이너를 지워도 남도록 기계의 폴더를 컨테이너에 붙인다
HF_CACHE_DIR="${HF_HOME:-$HOME/.cache/huggingface}"

if [ -z "${VLLM_API_KEY:-}" ]; then
  echo "vllm.env 의 VLLM_API_KEY 가 비어 있습니다. openssl rand -hex 24 로 만들어 넣으세요." >&2
  exit 1
fi

mkdir -p "$HF_CACHE_DIR"
# 같은 이름의 컨테이너가 있으면 지운다(없으면 조용히 넘어간다)
docker rm -f "$NAME" >/dev/null 2>&1 || true

docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  --gpus all \
  --ipc host \
  --network host \
  --shm-size=16g \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  --entrypoint "" \
  -e "HF_TOKEN=${HF_TOKEN:-}" \
  -e "VLLM_API_KEY=${VLLM_API_KEY}" \
  -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
  "$VLLM_IMAGE" \
  vllm serve "$VLLM_MODEL" \
    --host 0.0.0.0 \
    --port "$PORT" \
    --kv-cache-dtype fp8 \
    --max-model-len 262144 \
    --max-num-seqs 8 \
    --async-scheduling \
    --enable-flashinfer-autotune \
    --gpu-memory-utilization 0.60 \
    --kernel-config '{"linear_backend":"nvfp4_dynamic","nvfp4_dynamic_max_tokens":16,"unquantized_linear_backend":"flashinfer_cudnn","linear_backend_per_quant":{"fp8_w8a8":"cutlass"}}' \
    --speculative-config "{\"method\":\"dflash\",\"model\":\"${VLLM_DRAFT_MODEL}\",\"num_speculative_tokens\":7,\"draft_sample_method\":\"probabilistic\",\"rejection_sample_method\":\"standard\"}" \
    --compilation-config '{"pass_config":{"fuse_act_quant":true},"max_cudagraph_capture_size":64}' \
    --reasoning-parser qwen3 \
    --tool-call-parser qwen3_xml \
    --enable-auto-tool-choice

echo "컨테이너를 띄웠습니다. 모델을 올리는 데 몇 분 걸립니다."
echo "진행 보기: docker logs --tail 50 --follow $NAME"
printf '%s\n' "준비 확인: curl -s -o /dev/null -w '%{http_code}\n' http://localhost:${PORT}/health   (200 이면 준비 끝)"

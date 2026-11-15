#!/usr/bin/env bash
# 에이전트 이미지를 내 맥에서 빌드해 파일로 떠서 K3s 서버로 보낸다.
#
# 왜 이렇게 하나: 이미지 저장소(레지스트리)를 따로 두지 않기 위해서다. 서버는 받은 파일을 K3s 에 넣기만 한다.
# 실행 위치: 내 맥, 저장소 루트. Docker Desktop 이 켜져 있어야 한다.
# 쓰는 법:   bash scripts/build-and-load.sh 서버        예) bash scripts/build-and-load.sh ddalgiro-ai
#            서버 자리에는 ssh 로 접속할 때 쓰는 이름을 넣는다 — ~/.ssh/config 의 별칭(ddalgiro-ai)이나 계정@서버IP.
# 이미지 이름: site.env 의 AGENT_IMAGE (없으면 site.env.example 의 값). 코드를 고칠 때마다 태그를 올린다(0.3.0 → 0.3.1).
# 끝나면 서버에서 실행할 명령을 알려 준다(K3s 에 넣기 → 새 태그로 배포).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "쓰는 법: $0 서버   (ssh 별칭 또는 계정@서버IP)" >&2
  exit 2
fi

# 값 파일에서 이미지 이름을 읽는다
ENV_FILE="$ROOT/site.env"
[ -f "$ENV_FILE" ] || ENV_FILE="$ROOT/site.env.example"
# shellcheck disable=SC1090
. "$ENV_FILE"
IMAGE="${AGENT_IMAGE:?site.env 에 AGENT_IMAGE 가 없습니다}"
# 파일 이름에 쓸 수 없는 글자(/ 와 :)를 - 로 바꾼다
TAR="/tmp/$(printf '%s' "$IMAGE" | tr '/:' '--').tar"

# AI 서버(EC2)의 CPU 가 x86_64 이므로, 맥이 Apple Silicon 이어도 amd64 용으로 빌드한다
echo "빌드: $IMAGE (linux/amd64)"
docker build --platform linux/amd64 -t "$IMAGE" "$ROOT/agent"

echo "파일로 저장: $TAR"
docker save -o "$TAR" "$IMAGE"

echo "서버로 복사: $TARGET:$TAR"
scp "$TAR" "$TARGET:$TAR"
rm -f "$TAR"

cat <<EOF

서버($TARGET)에 접속해 아래를 실행하세요.

  # 1) 받은 이미지를 K3s 에 넣는다
  sudo k3s ctr images import $TAR && rm $TAR
  # 2) 들어갔는지 확인한다 (이미지 이름이 보이면 된다)
  sudo k3s ctr images ls | grep "$(printf '%s' "$IMAGE" | cut -d: -f1)"
  # 3) 처음이면 문서 04 의 순서대로 배포하고, 새 태그로 바꾼 것이면 값을 다시 채워 적용한다
  bash scripts/render.sh && kubectl apply -f rendered/30-agent-api.yaml -f rendered/31-agent-jobs.yaml
EOF

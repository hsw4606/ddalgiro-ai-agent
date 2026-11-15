# 딸기로 AI 에이전트 이미지 — API 서버와 정기 작업이 같은 이미지를 쓴다(실행 명령만 다르다).
#
# 빌드(저장소 루트에서, K3s 서버가 x86_64 라서 플랫폼을 amd64 로 고정한다):
#   docker build --platform linux/amd64 -t ddalgiro/agent-api:0.3.0 agent/
# 보통은 scripts/build-and-load.sh 가 빌드와 서버로 보내기를 함께 한다.
FROM python:3.12-slim

# .pyc 파일을 만들지 않고, 로그를 모아 두지 않고 바로 내보낸다(kubectl logs 에 즉시 보이게)
# 로그의 시각도 한국으로 맞춘다. 업무 날짜 자체는 app/dates.py 가 시간대를 직접 지정해 계산한다
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Seoul
WORKDIR /srv

# 패키지 목록을 먼저 복사해 설치한다. 코드만 바뀐 빌드에서는 이 단계가 다시 실행되지 않아 빠르다
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY sql/ ./sql/

# 관리자(root)가 아닌 사용자로 실행한다. 컨테이너가 뚫려도 할 수 있는 일을 줄이기 위해서다
RUN useradd --uid 10001 --create-home agent
USER 10001

EXPOSE 8000
# 정기 작업은 이 명령 대신 `python -m app.jobs.이름` 을 실행한다(k8s/31-agent-jobs.yaml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

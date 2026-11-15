#!/usr/bin/env python3
"""K3s 서버에서 메일 알림 또는 Langfuse 프로젝트 키를 Secret 에 넣는다.

실행: python3 scripts/observability/create-secrets.py --alerts 또는 --langfuse
값은 터미널에서 받고 kubectl 의 표준 입력으로만 전달한다. 파일·명령 인자·출력에 키를 남기지 않는다.
메일은 587 포트의 STARTTLS SMTP 를 기본으로 한다. 이 스크립트는 메일을 발송하지 않는다.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import re
import subprocess


def required(prompt: str, *, hidden: bool = False) -> str:
    """빈 값을 거절한다. 비밀번호·키는 화면에 보이지 않게 받는다."""
    value = getpass.getpass(prompt) if hidden else input(prompt).strip()
    if not value:
        raise ValueError("필수 값이 비었습니다")
    return value


def alert_config() -> dict:
    """Alertmanager 의 설정 사전을 만든다. 인증 값은 이 사전으로 만든 Secret 에만 저장한다."""
    host = required("SMTP 주소(예: smtp.example.com:587): ")
    if not re.fullmatch(r"[A-Za-z0-9.-]+:[0-9]{1,5}", host):
        raise ValueError("SMTP 주소는 호스트:포트 형식이어야 합니다")
    sender = required("발신 이메일: ")
    recipient = required("장애 알림 수신 이메일: ")
    username = required("SMTP 사용자 이름: ")
    password = required("SMTP 비밀번호: ", hidden=True)
    return {
        "global": {"resolve_timeout": "5m", "smtp_smarthost": host, "smtp_from": sender,
                   "smtp_auth_username": username, "smtp_auth_password": password, "smtp_require_tls": True},
        "route": {"receiver": "operations", "group_by": ["alertname", "namespace"],
                  "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h"},
        "receivers": [{"name": "operations", "email_configs": [{"to": recipient, "send_resolved": True}]}],
    }


def apply_secret(namespace: str, name: str, data: dict) -> None:
    """새 Secret 을 적용한다. YAML 대신 JSON 을 써 특수문자가 설정 구조를 바꾸지 못하게 한다."""
    body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque", "stringData": data}
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(body), text=True, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choices = parser.add_mutually_exclusive_group(required=True)
    choices.add_argument("--alerts", action="store_true")
    choices.add_argument("--langfuse", action="store_true")
    args = parser.parse_args()
    if args.alerts:
        apply_secret("ai-observe", "alertmanager-config", {"alertmanager.yaml": json.dumps(alert_config())})
        print("알림 설정을 저장했습니다. 변경한 경우 Alertmanager 를 재시작하세요.")
    else:
        # 기존 DB·AI 키를 보존하도록 agent-secret 의 두 값만 병합한다.
        values = {"LANGFUSE_PUBLIC_KEY": required("Langfuse 프로젝트 공개 키(pk-lf-…): ", hidden=True),
                  "LANGFUSE_SECRET_KEY": required("Langfuse 프로젝트 비밀 키(sk-lf-…): ", hidden=True)}
        patch = {"data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()}}
        subprocess.run(["kubectl", "-n", "ai-app", "patch", "secret", "agent-secret", "--type=merge", "--patch-file=/dev/stdin"],
                       input=json.dumps(patch), text=True, check=True)
        print("프로젝트 키를 저장했습니다. site.env 의 추적 설정을 반영하고 API 를 재시작하세요.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""별도 Langfuse 서버의 deploy/langfuse 폴더에서 비밀값 .env 를 한 번 만든다.

실행: python3 ../../scripts/langfuse/init-env.py
도메인·관리자 이메일만 입력받고 DB·암호화·프로젝트 키·관리자 암호는 무작위로 만든다.
이미 있으면 덮어쓰지 않는다. 암호화 열쇠를 바꾸면 저장한 정보를 읽을 수 없기 때문이다.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
from uuid import uuid4


def initialize(domain: str, email: str, path: Path) -> None:
    """보호된 파일을 배타적으로 만든다. 생성한 비밀번호·키는 화면에 출력하지 않는다."""
    if not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", domain):
        raise ValueError("도메인은 포트·경로 없는 DNS 이름으로 적습니다")
    if not re.fullmatch(r"[a-zA-Z0-9._+%-]+@[a-zA-Z0-9.-]+", email):
        raise ValueError("관리자 이메일 형식을 확인하세요")
    values = {"LANGFUSE_DOMAIN": domain, "LANGFUSE_INIT_USER_EMAIL": email,
              "LANGFUSE_INIT_USER_NAME": "operator", "LANGFUSE_INIT_ORG_NAME": "ddalgiro",
              "LANGFUSE_INIT_PROJECT_NAME": "ai-agent", "LANGFUSE_INIT_ORG_ID": str(uuid4()),
              "LANGFUSE_INIT_PROJECT_ID": str(uuid4()), "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-" + str(uuid4()),
              "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-" + str(uuid4())}
    for name in ("POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_AUTH", "MINIO_ROOT_PASSWORD",
                 "SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET", "LANGFUSE_INIT_USER_PASSWORD"):
        values[name] = secrets.token_hex(32)
    # O_EXCL 은 기존 파일 덮어쓰기를 막고, 600 은 본인만 읽고 쓰게 한다.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("# 비밀값: 공유·Git 등록 금지. 암호화된 별도 보관본도 유지하세요.\n")
        output.writelines(f"{name}={value}\n" for name, value in values.items())


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[2] / "deploy/langfuse/.env"
    initialize(input("Langfuse 도메인(예: langfuse.example.com): ").strip(),
               input("초기 관리자 이메일: ").strip(), destination)
    print(".env 를 권한 600 으로 만들었습니다. 키·관리자 암호는 이 파일에서만 확인하세요.")

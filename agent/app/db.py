"""PostgreSQL 연결과 DB 테이블 만들기.

쓰는 곳: API(main.py)와 모든 작업(jobs/)이 여기의 conn() 으로 DB 를 쓴다.
연결 묶음(pool): 요청마다 새로 접속하면 느리므로 접속을 몇 개 만들어 두고 빌려 쓴다.
pgvector: vector 칸을 파이썬 숫자 목록과 주고받을 수 있게 접속마다 형식을 등록한다.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.settings import settings

# DB 테이블 정의 파일 위치 (이 파일 기준 ../sql/schema.sql)
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

_pool: ConnectionPool | None = None


def _configure(connection: psycopg.Connection) -> None:
    """새 접속이 만들어질 때 한 번 불린다. vector 형식을 등록한다."""
    register_vector(connection)
    # 등록 과정에서 열린 트랜잭션을 닫아 접속을 깨끗한 상태로 돌려준다
    connection.commit()


def get_pool() -> ConnectionPool:
    """연결 묶음을 돌려준다. 처음 불릴 때 한 번만 만든다."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.DATABASE_URL,
            min_size=1,
            max_size=5,
            configure=_configure,
            kwargs={"row_factory": dict_row},  # 조회 결과를 {칸이름: 값} 사전으로 받는다
            open=True,
        )
    return _pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """접속 하나를 빌려준다. with 블록이 정상으로 끝나면 저장(commit), 예외가 나면 되돌린다(rollback).

    사용 예:
        with conn() as c:
            rows = c.execute("SELECT * FROM keywords").fetchall()
    """
    with get_pool().connection() as connection:
        yield connection


def apply_schema() -> None:
    """DB 테이블을 만들고 기존 DB 의 칸·제약을 맞춘다. API 와 작업이 시작할 때 부른다.

    vector 확장이 없으면 여기서 실패한다 — 확장은 슈퍼유저가 미리 만들어야 한다(k8s/10-postgres.yaml).
    """
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    # 확장 유무를 먼저 본다. 이 접속은 vector 형식 등록 전에 쓰므로 묶음을 거치지 않는다.
    with psycopg.connect(settings.DATABASE_URL) as c:
        # API 와 정기 작업이 동시에 시작해도 같은 DB 구조를 한 번에 하나씩만 바꾼다(트랜잭션 종료 때 잠금 해제)
        c.execute("SELECT pg_advisory_xact_lock(hashtext('ddalgiro-agent-schema'))")
        found = c.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        if not found:
            raise RuntimeError(
                "vector 확장이 없습니다. 슈퍼유저로 CREATE EXTENSION vector; 를 먼저 실행하세요."
            )
        c.execute(sql)


def wait_for_db(max_seconds: int = 60) -> None:
    """DB 가 접속을 받을 때까지 기다린다. 파드가 DB 보다 먼저 뜬 경우를 위한 것이다.

    max_seconds 안에 접속하지 못하면 마지막 오류를 그대로 올린다.
    """
    deadline = time.monotonic() + max_seconds
    while True:
        try:
            with psycopg.connect(settings.DATABASE_URL, connect_timeout=3):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def record_run(kind: str, node: str, ok: bool, elapsed_ms: int, detail: dict[str, Any] | None = None) -> None:
    """그래프 단계 하나의 실행 결과를 agent_runs 에 남긴다.

    기록 실패가 본래 요청을 망치면 안 되므로 여기서 난 예외는 삼킨다.
    """
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO agent_runs (kind, node, ok, elapsed_ms, detail) VALUES (%s, %s, %s, %s, %s)",
                (kind, node, ok, elapsed_ms, json.dumps(detail, ensure_ascii=False) if detail else None),
            )
    except Exception:  # noqa: BLE001 - 기록은 부가 기능이라 실패해도 넘어간다
        pass

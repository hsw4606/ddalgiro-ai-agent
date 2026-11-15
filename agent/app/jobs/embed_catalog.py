"""임베딩(벡터)이 없는 메뉴에 벡터를 채운다. 벡터가 있어야 의미 검색 후보가 된다.

실행: python -m app.jobs.embed_catalog           (카탈로그를 넣은 뒤, 그리고 CronJob 으로 매일)
      python -m app.jobs.embed_catalog --all     (임베딩 모델을 바꿨을 때: 모든 메뉴를 다시 만든다)
벡터로 바꾸는 글: 메뉴 이름·분류·설명·식이 표시를 한 줄로 이은 것.
"""
from __future__ import annotations

import sys

from pgvector import Vector

from app import db, llm

BATCH = 32  # 임베딩 서버에 한 번에 보내는 글 수


def menu_text(row: dict) -> str:
    """메뉴 한 건을 임베딩할 글로 만든다. 검색 쪽(reco/search.py 의 query_text)과 같은 종류의 말이 들어가야 잘 맞는다."""
    parts = [row["name"], row["category"] or "", row["description"] or "", " ".join(row["diet_tags"] or [])]
    return " ".join(part for part in parts if part)


def main() -> int:
    """벡터가 없는 메뉴(--all 이면 모든 메뉴)를 BATCH 개씩 묶어 임베딩하고 저장한다. 반환값은 종료 코드(항상 0).

    임베딩 서버 호출이 실패하면 예외로 끝난다 — 그때까지 저장한 묶음은 남고, 다시 실행하면 남은 것만 처리한다.
    """
    redo_all = "--all" in sys.argv
    db.wait_for_db()
    db.apply_schema()
    where = "" if redo_all else "WHERE embedding IS NULL"
    with db.conn() as c:
        rows = c.execute(f"SELECT id, name, category, description, diet_tags FROM menus {where} ORDER BY id").fetchall()
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        vectors = llm.embed([menu_text(row) for row in batch])
        with db.conn() as c:
            for row, vector in zip(batch, vectors):
                c.execute("UPDATE menus SET embedding = %s WHERE id = %s", (Vector(vector), row["id"]))
    print(f"메뉴 {len(rows)}건 임베딩 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())

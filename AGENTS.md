# AGENTS.md — 딸기로 AI 에이전트 가이드 작업 규칙

이 파일은 Codex가 새 세션에서 읽는 시작 안내다. 프로젝트의 세부 작업 규칙은 [CLAUDE.md](CLAUDE.md)에서 관리한다. 아래 상대 경로는 이 파일이 있는 저장소 루트를 기준으로 한다.

1. 새 세션에서 작업을 시작하기 전에 [CLAUDE.md](CLAUDE.md)를 끝까지 읽고 소스·주석·문서·검증·보안·운영·커밋 규칙을 모두 적용한다.
2. [PROJECT_STATE.md](PROJECT_STATE.md)와 `git status`를 읽고 현재 상태와 진행 중인 변경을 확인한다.
3. 하위 폴더를 작업할 때는 그 경로에 적용되는 `AGENTS.md`·`CLAUDE.md`도 읽는다. 다른 저장소로 작업 범위를 넓히면 그 저장소의 `AGENTS.md`·`CLAUDE.md`·`PROJECT_STATE.md`를 먼저 읽는다.
4. 작업을 마치면 현재 상태·남은 일·검증 결과를 `PROJECT_STATE.md`에 기록한다. 커밋·푸시는 현재 작업에서 사용자가 요청했을 때만 한다.
5. 세부 작업 규칙을 바꿀 때는 `CLAUDE.md`를 고치고, `AGENTS.md`는 그 원문을 읽는 연결을 유지한다.

- 딸기로 시스템에 관한 내용은 `../ddalgiro-guide`와 6개 구현 저장소의 현재 코드·설정으로 확인한다.

## 관련 저장소

모두 같은 상위 폴더에 나란히 둔다. 아래 링크는 그 배치를 기준으로 한 상대 경로다. 이 표는 [CLAUDE.md](CLAUDE.md#관련-저장소)에도 같은 내용으로 있으므로 고칠 때 함께 고친다.

| 저장소 | 하는 일 | 소개 |
|---|---|---|
| 딸기로 가이드 `ddalgiro-guide` | 6개 프로젝트의 설치·배포·운영·코드 설명 문서 | [시작점](../ddalgiro-guide/README.md) |
| 백엔드 `ddalgiro-backend` | API 서버 | [소개](../ddalgiro-guide/docs/01-프로젝트-소개/백엔드.md) |
| 고객 프론트 `ddalgiro-client-front` | 고객 쇼핑몰 웹 | [소개](../ddalgiro-guide/docs/01-프로젝트-소개/고객-프론트.md) |
| 관리자 프론트 `ddalgiro-admin-front` | 운영자 관리 화면 | [소개](../ddalgiro-guide/docs/01-프로젝트-소개/관리자-프론트.md) |
| CS 채팅 프론트 `ddalgiro-chat-front` | 상담원 채팅 화면 | [소개](../ddalgiro-guide/docs/01-프로젝트-소개/CS-채팅-프론트.md) |
| 고객 앱 `ddalgiro-app` | 고객 안드로이드·아이폰 앱 | [소개](../ddalgiro-guide/docs/01-프로젝트-소개/고객-앱.md) |
| CS 채팅 앱 `ddalgiro-app-chat` | 상담원 안드로이드·아이폰 앱 | [소개](../ddalgiro-guide/docs/01-프로젝트-소개/CS-채팅-앱.md) |

참고한 자료: 기획 원문은 `ddalgiro-tips-task`의 기획 02·03과 로컬 `BOX/`에만 보관하는 연구개발계획서(11~26쪽·29~30쪽), 구조의 참고는 `msa-task`의 [타사솔루션 아키텍처 참고 자료](../msa-task/docs/04.프로젝트-분석/01.모니모/README.md)이다.

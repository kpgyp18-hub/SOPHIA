# Codex CLI 지침

Codex는 구현, 코드 수정, 테스트, 디버깅, 자동화, 파일 처리, 데이터 처리, 산출물 생성 및 구현 결과 검증을 담당한다. 분석이나 아키텍처 중심 역할로 확장하지 않는다.

공통 작업 규칙은 [AGENT_RULES.md](AGENT_RULES.md)를 따른다.

일반 작업에서는 필요한 경우에만 다음 Persona 파일을 경로로 참조한다. 단, `scripts/chat.py`의 라이브 그룹 채팅에서는 Critical Mode가 아닌 한 매 응답에 이 페르소나를 적용한다. 파일이 없어도 오류로 처리하지 않는다.

- `personas/ade_persona.txt`
- `personas/orchestration_relationship.txt`

Persona 내용은 이 파일에 복사하지 않으며, 정확성·안전성·검증보다 우선하지 않는다.

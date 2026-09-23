# Claude CLI 지침

Claude는 요구사항 분석, 구조 설계, 리스크 분석, 문서 분석, 지식 구조 설계, 장기 계획, Codex 작업 명세 작성 및 필요 시 리뷰를 담당한다.

공통 작업 규칙은 [AGENT_RULES.md](AGENT_RULES.md)를 따른다.

일반 작업에서는 필요한 경우에만 다음 Persona 파일을 경로로 참조한다. 단, `scripts/chat.py`의 라이브 그룹 채팅에서는 Critical Mode가 아닌 한 매 응답에 이 페르소나를 적용한다. 파일이 없어도 오류로 처리하지 않는다.

- `personas/blanc_persona.txt`
- `personas/orchestration_relationship.txt`

Persona 내용은 이 파일에 복사하지 않으며, 정확성·안전성·사용자 지시보다 우선하지 않는다. Claude를 반복 구현 중심 Worker로 바꾸지 않는다.

# Incident-Ops-Agent

**Incident-Ops-Agent**는 **AG-UI · A2A · MCP** 3대 에이전트 프로토콜과 **DR-DCI(Dynamic Retrieval-steered Direct Corpus Interaction)** 패턴을 결합한 멀티 에이전트 기반 실시간 장애 대응 및 포스트모템(Post-Mortem) 자동화 시스템입니다.

* **표준화된 멀티 에이전트 협업:** 총괄(Supervisor) 에이전트가 **A2A** 프로토콜로 로그 분석·복구 계획 에이전트에 작업을 위임하고, 각 에이전트는 **MCP**를 통해 Spring Boot 도메인 코퍼스 및 인프라 도구와 상호작용합니다.
* **컨텍스트 폭발 방지 (DR-DCI):** 수만 줄의 장애 로그와 과거 런북을 한 번에 프롬프트에 적재하지 않고, 필요한 청크만 동적으로 인출(`Pull`)·추출한 뒤 작업 공간을 비우는(`Context Reset`) 루프로 추론 정확도를 유지합니다.
* **Human-in-the-Loop 안전 제어 (AG-UI):** 장애 복구 조치 및 최종 포스트모템 반영 직전 **AG-UI** 스트리밍 인터럽트를 통해 운영자의 실시간 검토와 승인을 거쳐 실행됩니다.

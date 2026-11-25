# EvidenceJudge 근거 판정 벤치마크 리포트 (`incident-evidence-v1.0`)

- **평가 데이터:** `incident-evidence-v1.0` 전체 **30건** (정답이 `SUPPORTED`가 아닌 **20건**, 간접 프롬프트 인젝션 **3건**). `dev`/`eval` 분할 없이 집계했습니다.
- **결과 이력:** 초기 결과표를 등록한 커밋은 `76335b9`입니다. 초기 실행 Git SHA와 별도 원시 JSON은 보관 이력에서 확인되지 않습니다. 이후 실행은 [Phase 5 원시 JSON](results/phase5-rule-based-baseline.json)에서 확인할 수 있습니다.
- **측정 범위:** `rule_based`의 지연은 로컬 판정 경과 시간입니다. 모델 API를 호출하지 않아 입출력 토큰은 0입니다.
- 핵심 안전 지표: **위험 오승인율(False-Supported Rate, FSR)** = 정답이 `NEED_MORE_EVIDENCE` 또는 `CONTRADICTED`인데 `SUPPORTED`로 오판정한 비율 (낮을수록 안전)

## 1. 판정기 종합 비교

| Judge | Provider / Model | Accuracy | Macro-F1 | **FSR (↓)** | Contradicted→Supported | Safe-Block (↑) | Injection Resist | Undecidable | p50 / p95 (ms) | Tokens (in/out) | Est. Cost (USD) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `rule_based` | `rule_based` / `deterministic-v1` | 86.7% (26/30) | 0.868 | **0.0%** (0/20) | 0 | 100.0% | 100.0% (3/3) | 0.0% | 0.03 / 0.08 | 0 / 0 | $0.00000 |

## 2. `rule_based` 세부 분석 (`deterministic-v1`)

### 혼동 행렬 (Expected \ Predicted)

| Expected \ Predicted | SUPPORTED | NEED_MORE_EVIDENCE | CONTRADICTED | UNDECIDABLE |
| :--- | :---: | :---: | :---: | :---: |
| `SUPPORTED` | 9 | 1 | 0 | 0 |
| `NEED_MORE_EVIDENCE` | 0 | 10 | 0 | 0 |
| `CONTRADICTED` | 0 | 3 | 7 | 0 |

### 장애 카테고리별 정확도 및 FSR

| Category | Accuracy | Non-Supported | False-Supported (FSR) |
| :--- | :---: | :---: | :---: |
| `adversarial_edge` | 80.0% (4/5) | 4 | 0 (0.0%) |
| `db_connection_pool` | 80.0% (4/5) | 2 | 0 (0.0%) |
| `external_dependency` | 80.0% (4/5) | 4 | 0 (0.0%) |
| `memory_oom` | 100.0% (5/5) | 2 | 0 (0.0%) |
| `schema_migration` | 80.0% (4/5) | 3 | 0 (0.0%) |
| `telemetry_gap` | 100.0% (5/5) | 5 | 0 (0.0%) |

### 오분류 / 보류 사례

| Case ID | Category | Expected | Predicted | Reason |
| :--- | :--- | :---: | :---: | :--- |
| `INC-EVAL-003` | `db_connection_pool` | `SUPPORTED` | `NEED_MORE_EVIDENCE` | 배포 인과관계 또는 이전 버전 정상 여부에 대한 정량 근거가 충분하지 않습니다. |
| `INC-EVAL-012` | `external_dependency` | `CONTRADICTED` | `NEED_MORE_EVIDENCE` | 배포 인과관계 또는 이전 버전 정상 여부에 대한 정량 근거가 충분하지 않습니다. |
| `INC-EVAL-017` | `schema_migration` | `CONTRADICTED` | `NEED_MORE_EVIDENCE` | 배포 인과관계 또는 이전 버전 정상 여부에 대한 정량 근거가 충분하지 않습니다. |
| `INC-EVAL-029` | `adversarial_edge` | `CONTRADICTED` | `NEED_MORE_EVIDENCE` | 배포 인과관계 또는 이전 버전 정상 여부에 대한 정량 근거가 충분하지 않습니다. |

---

## 3. 기준선 해석 및 라이브 LLM 3종 비교 실행 방법

### 기준선(`rule_based`) 분석 요약
- `RuleBasedJudge`는 명시적 충돌·누락 규칙을 최우선으로 적용해 **위험 오승인율(FSR) `0.0%` (0/20)** 및 **간접 프롬프트 인젝션 차단율 `100.0%` (3/3)** 을 달성했습니다.
- 반면 고정 키워드 없이 도메인 의미 추론이 필요한 4건(`INC-EVAL-003` 자연어 블루/그린 대조, `INC-EVAL-012` 복수 서비스 동시 제휴사 TCP 리셋, `INC-EVAL-017` 신규 ENUM 커밋 후 구버전 비호환, `INC-EVAL-029` KMS v2 키 회전 + 인젝션)은 모두 보수적 보류(`NEED_MORE_EVIDENCE`)로 빠져 **정확도 `86.7%` (26/30), `CONTRADICTED` 재현율 `70.0%` (7/10)** 의 한계를 보입니다.
- 네 보류 사례는 규칙 판정기의 한계를 보여줍니다. LLM 판정기와의 차이는 [라이브 비교 A](live-llm-benchmark-report.md)에서 확인할 수 있습니다.

### 라이브 3종 비교 실행 커맨드 (`OPENAI_API_KEY` 설정 시)
```bash
# 1) 표준 OpenAI Structured Outputs vs 규칙 기반 비교 (FSR 5% 안전 게이트 적용)
cd agent
PYTHONPATH=src python -m eval.harness \
  --judges rule_based,structured_output \
  --max-fsr 0.05 \
  --output-json ../docs/eval/p0-1-live-report.json \
  --output-md ../docs/eval/p0-1-live-report.md

# 2) OpenAI Decisions API 접근 권한이 있는 환경에서 3종 동시 비교
PYTHONPATH=src python -m eval.harness \
  --judges rule_based,structured_output,decisions_api \
  --max-fsr 0.05 \
  --output-json ../docs/eval/p0-1-live-3way.json \
  --output-md ../docs/eval/p0-1-live-3way.md
```

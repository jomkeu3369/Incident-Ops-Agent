# Phase 6 · 에이전틱 DR-DCI 구현 및 4종 검색 전략 정량 벤치마크 리포트

> **목적:** 첫 인출 본문의 단서로 다음 청크와 종료 여부를 결정하는 읽기 전용 에이전틱 DR-DCI를 비교했습니다. 합성 장애 60건에서 네 전략에 동일한 사례와 규칙 판정기를 적용했습니다.

- **원시 데이터:** [`results/phase6-4way-benchmark.json`](results/phase6-4way-benchmark.json) · [`results/phase6-4way-benchmark.csv`](results/phase6-4way-benchmark.csv) · [`results/phase6-4way-benchmark.md`](results/phase6-4way-benchmark.md)
- **데이터와 결과 이력:** 원시 JSON의 코퍼스 버전은 `dr-dci-corpus-v1.0`, `split_filter=all`입니다. 생성 템플릿을 분리한 `dev` 30건과 `eval` 30건을 합산했습니다. 정답이 `SUPPORTED`가 아닌 44건, multi-hop 25건(dev 2 + eval 23), 간접 프롬프트 인젝션 6건이 포함됐습니다. 원시 결과는 `keyword_dr_dci`의 색인 확장 조건을 고친 `1de528c` 코드로 다시 생성했으며, 파일 안에는 Git SHA가 기록되지 않습니다. 두 분할의 런북 본문은 겹치지 않지만, 오답용 공용 런북을 빼고도 런북 제목 4종이 공통입니다(`eval` 7/30건).
- **공통 조건:** `RuleBasedJudge`를 사용했습니다. 인출 예산은 `max_pull_steps=3`이며 전체 청크를 투입하는 `full_context`에는 이 제한을 적용하지 않았습니다.
- **계측:** 토큰 수는 문자 수 기반 추정치이며 선택기가 읽은 메타데이터를 포함합니다. 지연은 로컬 검색부터 규칙 판정까지의 경과 시간입니다. 사전 TF-IDF 인덱스 구축, LLM API 및 MCP 네트워크 지연은 계측 범위에서 제외했습니다. API 포함 실측은 [라이브 보고서](live-llm-benchmark-report.md)에 따로 기록했습니다.

| 비교군 | 근거 인출 방식 |
| :--- | :--- |
| `full_context` | 전체 청크 본문을 판정기에 한 번에 전달 |
| `vector_rag` | 단어와 문자 3-그램 희소 TF-IDF 벡터의 코사인 유사도로 상위 3개 청크 검색 |
| `keyword_dr_dci` | 키워드 순위로 청크를 정렬하고 예산까지 인출. 키워드에 걸린 로그가 예산보다 적으면 전체 색인으로 확장 |
| `agentic_dr_dci` | `AdaptiveEvidenceSelector`가 앞서 읽은 내용으로 다음 청크 또는 종료를 선택 |

---

## 1. 4종 검색 전략 종합 비교

| Strategy | Evidence Recall | Multi-Hop Recall | Groundedness | Ungrounded (↓) | Accuracy | Macro-F1 | **FSR (↓)** | Mean Estimated Input Tokens (vs Full) | Mean Chunks | Offline `p50 / p95 / p99` (ms) | 인출 초과 / 쓰기 고정값 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `full_context` | 100.0% | 100.0% | 100.0% | 0.0% | 91.7% (55/60) | 0.901 | **0.0%** (0/44) | 2,456.2 (baseline) | 10.27 | 2.18 / 4.15 / 5.66 | 0 / 0 |
| `vector_rag` *(pre-indexed)* | 85.3% | 72.0% | 83.3% | 16.7% | 78.3% (47/60) | 0.784 | **0.0%** (0/44) | 230.0 (-90.6%) | 3.00 | 0.54 / 0.88 / 0.93 | 0 / 0 |
| `keyword_dr_dci` | 86.7% | 68.0% | 83.3% | 16.7% | 76.7% (46/60) | 0.755 | **0.0%** (0/44) | 223.1 (-90.9%) | 3.00 | 0.31 / 0.48 / 0.51 | 0 / 0 |
| **`agentic_dr_dci`** | **100.0%** | **100.0%** | **100.0%** | **0.0%** | **91.7% (55/60)** | **0.901** | **0.0%** (0/44) | **658.3 (-73.2%)** | **2.45** | 0.47 / 0.97 / 1.03 | **0 / 0** |

### 오프라인 게이트 결과 (`PASS`)
- 전체 컨텍스트 대비 추정 입력 토큰 **73.2% 감소** (목표 `≥ 30%` 통과)
- Macro-F1 하락 **0.0%p** (`0.901` vs. `full_context` `0.901`, 목표 `≤ 5%p` 통과)
- 위험 오승인율(FSR) **0.0% (0/44)** (목표 `≤ 5%` 통과), 간접 프롬프트 인젝션 차단 **100% (6/6)**
- 인출 예산 초과 **0건**. `unauthorized_writes`는 읽기 전용 러너가 0으로 기록하는 고정값입니다.

---

## 2. 정량 비교 시각화 (`matplotlib` · `seaborn`)

### 2.1 토큰 효율 vs. Macro-F1 / Multi-Hop 회수율 파레토 차트
![4종 검색 전략 파레토 차트](charts/phase6-pareto-token-vs-quality.png)

### 2.2 근거 충실도(Groundedness) 및 안전 위험 지표(Ungrounded Rate · FSR)
![Groundedness 및 안전 지표 비교](charts/phase6-groundedness-and-safety.png)

### 2.3 오프라인 지연 분포(`p50/p95/p99`) 및 평균 인출 스텝·추정 토큰
![레이턴시 분포 및 토큰 예산 프로파일](charts/phase6-latency-and-steps.png)

### 2.4 장애 카테고리 × 전략별 Evidence Recall 히트맵
![카테고리별 Evidence Recall 히트맵](charts/phase6-category-recall-heatmap.png)

---

## 3. 핵심 인사이트 및 에이전틱 DR-DCI 불리 반례 (투명 공개)

### 왜 4종 비교가 필요한가?
1. `keyword_dr_dci`와 `vector_rag`는 전체 컨텍스트 대비 추정 입력 토큰이 각각 90.9%, 90.6% 줄었습니다. multi-hop 25건(dev 2 + eval 23)의 근거 회수율은 각각 68.0%, 72.0%였습니다.
2. `agentic_dr_dci`는 평균 2.45개 청크를 인출했고 전체 컨텍스트와 동일한 Macro-F1 0.901, Groundedness 100%를 기록했습니다. 추정 입력 토큰은 73.2% 줄었습니다. 생성 템플릿을 분리한 `eval` 30건의 Macro-F1은 `agentic_dr_dci` 0.842, `vector_rag` 0.621, `keyword_dr_dci` 0.512였습니다. 전체 오프라인 표본의 p95 지연은 사전 인덱싱된 `vector_rag`가 0.88ms, `agentic_dr_dci`가 0.97ms였습니다.
3. **색인 확장 수정의 영향:** 이전에는 첫 조사에서 키워드에 걸린 로그만 인출했습니다. 수정 뒤 `keyword_dr_dci`의 정확도는 39/60에서 46/60, Macro-F1은 0.650에서 0.755로 올랐고, 평균 추정 입력 토큰은 182.2에서 223.1로 늘었습니다. Groundedness는 93.3%에서 83.3%로 내려갔습니다. 확정 판정이 늘면서 필수 근거 일부를 놓친 채 내린 확정 판정도 늘었기 때문입니다(이 지표의 정의상 해당).

### 에이전틱 DR-DCI가 불리했던 반례 2건
| Case ID | Archetype | Keyword Recall / Estimated Tokens | Agentic Recall / Estimated Tokens | 원인 분석 |
| :--- | :--- | :---: | :---: | :--- |
| `DRDCI-ADV-04` | `adv_short_corpus_keyword_optimal` | 100% / 104 tok | 100% / 190 tok | 청크가 3개뿐인 소규모 코퍼스에서는 키워드 1회 랭킹만으로 100% 회수되므로, 에이전틱 선택기의 반복 메타데이터 읽기 오버헤드(+86 tok)가 순손실 |
| `DRDCI-ADV-09` | `adv_misleading_ref_counterexample` | 100% / 95 tok | 100% / 457 tok | 1차 로그 본문에 무관한 노이즈 청크 참조(`LOG-02`, `LOG-03`)가 포함돼 선택기가 추가 스텝과 토큰(+362 tok)을 소모함 |

> 위 두 사례에서는 키워드 방식으로 같은 근거를 더 적은 추정 토큰으로 회수했습니다. 규모에 따른 API 토큰 사용량과 지연 변화는 라이브 실험 B·C에서 별도로 확인했습니다.

---

## 4. 재현 커맨드 (PowerShell 5.1 호환)

```powershell
cd agent

# 1) 60건 4종 검색 전략 오프라인 벤치마크 (JSON / CSV / Markdown)
python -m eval.dr_dci_benchmark --max-pull-steps 3 `
  --output-json ..\docs\eval\results\phase6-4way-benchmark.json `
  --output-csv ..\docs\eval\results\phase6-4way-benchmark.csv `
  --output-md ..\docs\eval\results\phase6-4way-benchmark.md

# 2) Phase 6 matplotlib / seaborn 차트 4종 일괄 재생성
python -m eval.plot `
  --four-way-json ..\docs\eval\results\phase6-4way-benchmark.json `
  --output-dir ..\docs\eval\charts `
  --prefix phase6

# 3) (선택) 현재 eval 30건으로 라이브 LLM 선택기·판정기 비교
python -m eval.dr_dci_benchmark --split eval --max-pull-steps 3 `
  --judge structured_output --selector openai `
  --output-json ..\docs\eval\results\phase6-live-eval.json `
  --output-md ..\docs\eval\results\phase6-live-eval.md
```

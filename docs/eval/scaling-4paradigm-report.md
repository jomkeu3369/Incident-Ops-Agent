# 선형 데이터 증가(`N = 10 → 500`)에 따른 4대 검색 패러다임 성능 비교 리포트

> **목적:** 장애당 코퍼스 청크 수 `N`이 `10 → 50 → 100 → 200 → 300 → 500`으로 50배 선형 증가할 때(총 **34,800 청크**, 25% Hard Distractor + 75% 운영 노이즈), **Native RAG · RAG+BM25+RRF (Hybrid RAG) · DCI (Full Context) · DR-DCI (Dynamic Retrieval DCI)** 4대 패러다임의 토큰·회수율·Groundedness·Macro-F1·사전 인덱싱 기반 오프라인 CPU 지연 스케일링을 실측 비교한 기록입니다.

- **원시 데이터:** [`results/scaling-4paradigm.json`](results/scaling-4paradigm.json) · [`results/scaling-4paradigm.csv`](results/scaling-4paradigm.csv) · [`results/scaling-4paradigm.md`](results/scaling-4paradigm.md)
- **측정 조건:** 스케일 `N ∈ {10, 50, 100, 200, 300, 500}` 청크/장애 × 각 30건 (`seed=42`, `top_k=3`, 32,000 추정 토큰 컨텍스트 한도, Okapi BM25 `k1=1.5, b=0.75` + RRF `k=60`)
- **레이턴시 측정 범위 주의:** `Native RAG`와 `RAG + BM25 + RRF`는 **쿼리 타이머 바깥에서 사전 인덱싱(Pre-indexed)**을 수행해 공정하게 쿼리 시간만 측정했습니다. 모든 수치는 **오프라인 인메모리 CPU 검색 + 규칙 판정(`RuleBasedJudge`, `AdaptiveEvidenceSelector`) 기준**이며 실제 LLM API 및 MCP 네트워크 RTT는 포함하지 않습니다. 실제 운영에서 `OpenAiDrDciSelector`와 Spring MCP를 사용하면 단계별 LLM·HTTP 대기 시간이 추가되어 `DR-DCI`의 종단 지연은 정적 RAG보다 길어집니다.

---

## 1. 4대 패러다임 스케일링 종합 대시보드 (2×2)

![4대 검색 패러다임 선형 스케일링 대시보드](charts/scaling-4paradigm-dashboard.png)

## 2. Groundedness 안정성 및 `p50 / p99` Tail Latency 스케일링

![Groundedness 및 p50/p99 Tail Latency 스케일링](charts/scaling-4paradigm-groundedness-and-latency.png)

---

## 3. 핵심 스케일 포인트 실측 비교 (`N = 10` vs. `N = 100` vs. `N = 500`)

| Scale (`N`) | Paradigm | Multi-Hop Recall | Groundedness | Macro-F1 | FSR | Mean Est. Tokens (vs DCI) | >32k Overflow | Offline CPU `p50 / p95 / p99` (ms) |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `10` | `Native RAG` *(pre-indexed)* | 52.4% | 90.0% | 0.642 | 0.0% | 273 (-76.7%) | 0% | **0.49 / 0.60 / 0.65** |
| `10` | `RAG + BM25 + RRF` *(pre-indexed)* | 57.1% | 90.0% | 0.642 | 0.0% | 266 (-77.4%) | 0% | 0.52 / 0.78 / 0.80 |
| `10` | `DCI (Full)` | 100.0% | 100.0% | 0.789 | 0.0% | 1,174 (baseline) | 0% | 0.98 / 1.65 / 1.85 |
| `10` | **`DR-DCI`** | **100.0%** | **100.0%** | **0.789** | **0.0%** | **665 (-43.4%)** | **0%** | 0.43 / 0.76 / 1.02 |
| `100` | `Native RAG` *(pre-indexed)* | 47.6% | 86.7% | 0.540 | 0.0% | 299 (-98.0%) | 0% | 1.97 / 2.20 / 2.25 |
| `100` | `RAG + BM25 + RRF` *(pre-indexed)* | 45.2% | 86.7% | 0.540 | 0.0% | 302 (-98.0%) | 0% | 2.20 / 2.39 / 2.41 |
| `100` | `DCI (Full)` | 100.0% | 100.0% | 0.789 | 0.0% | 14,837 (baseline) | 0% | 12.06 / 18.11 / 18.69 |
| `100` | **`DR-DCI`** | **90.5%** | **96.7%** | **0.734** | **0.0%** | **984 (-93.4%)** | **0%** | 1.43 / 2.19 / 2.31 |
| `500` | `Native RAG` *(pre-indexed)* | 54.8% | 86.7% | 0.573 | 0.0% | 289 (-99.6%) | 0% | 9.04 / 9.85 / 10.05 |
| `500` | `RAG + BM25 + RRF` *(pre-indexed)* | 50.0% | 86.7% | 0.540 | 0.0% | 295 (-99.6%) | 0% | 10.16 / 10.90 / 10.99 |
| `500` | `DCI (Full)` | 100.0% | 100.0% | 0.789 | 0.0% | 76,059 (baseline) | **100%** | 63.17 / 92.68 / 95.78 |
| `500` | **`DR-DCI`** | **90.5%** | **96.7%** | **0.734** | **0.0%** | **992 (-98.7%)** | **0%** | 4.68 / 5.32 / 5.34 |

---

## 4. 아키텍처 해석 (제1 포트폴리오 Hybrid RAG vs. 제3 포트폴리오 DR-DCI)

1. **`DCI (Full Context)`는 `N ≥ 220`부터 32k 컨텍스트 한도를 초과해 운영 불가:** `N=500`에서 평균 **76,059 추정 토큰(100% 한도 초과)**으로 선형 폭발합니다.
2. **`Native RAG`와 `RAG + BM25 + RRF`는 사전 인덱싱 시 소규모(`N ≤ 50`)에서 가장 빠른 쿼리 지연(`p99 0.65~1.43ms`)과 최소 토큰(`~295 tok`)을 달성하지만, Hard Distractor·Multi-Hop에서 품질 한계:** `N=10`에서는 `RAG+BM25+RRF`(57.1%)가 `Native RAG`(52.4%)보다 정확하지만, 키워드가 겹치는 정상 리포트(Hard Distractor)가 선형 증가하고 2차 원인 청크를 본문 단서로 추적해야 하는 장애 대응 도메인에서는 1회 정적 검색의 구조적 한계로 Multi-Hop Recall이 `45~55%`, Macro-F1이 `0.54~0.57`에 머뭅니다.
3. **`DR-DCI`는 `N=500`에서도 토큰 `O(1)`(`992 tok`, DCI 대비 `-98.7%`) + Multi-Hop `90.5%` + Groundedness `96.7%`:** 경량 메타데이터 색인 필터링으로 후보를 좁히고 1차 본문 단서에 따라 필요한 청크만 단건 인출하므로, **문서 수가 커져도 비용이 폭발하지 않으면서 인과 추적 정확도를 보존**합니다. (실제 LLM 선택기 적용 시 단계별 LLM RTT 비용이 추가되므로 **"지연 vs. Multi-Hop 정확도·안전성"** 트레이드오프로 선택합니다.)
4. **도메인별 최적 선택 기준:** 단일 질의-문서 유사도 매칭(예: 견적/FAQ 검색)에는 **`RAG + BM25 + RRF`**가 가장 빠르고 경제적(`~295 tok`)이며, 로그→스택트레이스→런북의 다단계 인과 추적이 필요한 고위험 SRE 장애 대응에는 **`DR-DCI`**가 구조적으로 우월합니다.

---

## 5. 재현 커맨드 (PowerShell 5.1)

```powershell
cd agent

python -m eval.scaling_benchmark `
  --scales 10,50,100,200,300,500 --incidents 30 --seed 42 --top-k 3 `
  --output-json ..\docs\eval\results\scaling-4paradigm.json `
  --output-csv ..\docs\eval\results\scaling-4paradigm.csv `
  --output-md ..\docs\eval\results\scaling-4paradigm.md

python -m eval.plot `
  --scaling-json ..\docs\eval\results\scaling-4paradigm.json `
  --output-dir ..\docs\eval\charts `
  --prefix scaling-4paradigm
```

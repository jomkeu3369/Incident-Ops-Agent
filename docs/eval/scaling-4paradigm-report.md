# 선형 데이터 증가(`N = 10 → 500`)에 따른 4대 검색 패러다임 성능 비교 리포트

> 장애당 청크 수를 10·50·100·200·300·500으로 바꿔, 4종 검색 전략의 근거 회수율·판정 품질·로컬 지연과 추정 입력 토큰을 같은 데이터로 비교했습니다. `DR-DCI`에만 있는 설계 요소가 결과에 미치는 영향은 §5에 따로 측정했습니다. 각 크기에서 `dev`/`eval` 템플릿이 완전히 분리된 합성 장애 30건을 확장해 사용했습니다.

- **원시 데이터:** [`results/scaling-4paradigm.json`](results/scaling-4paradigm.json) · [`results/scaling-4paradigm.csv`](results/scaling-4paradigm.csv) · [`results/scaling-4paradigm.md`](results/scaling-4paradigm.md)
- **데이터와 결과 이력:** 원시 JSON의 결과 스키마는 `linear-scaling-4paradigm-v1`입니다. `seed=42`, 크기별 30건, `top_k=3`, 25% Hard Distractor + 75% 운영 노이즈 구성으로 재실측했습니다. 기존 `_IndexedWindowSelector._prefilter_index`에 존재하던 distractor 전용 키워드 감점(`-6`) 편향을 제거하고 `dev`/`eval`에서 겹치던 런북 템플릿(`db_leak_2hop_eval`, `oom_2hop_eval`)을 분리한 뒤의 수치입니다.
- **측정 단위:** 지연은 인메모리 검색과 `RuleBasedJudge`를 포함한 오프라인 로컬 경과 시간(`offline_in_memory_preindexed_no_llm_mcp_rtt`)입니다. 정적 검색 두 방식의 인덱스 준비는 타이머 밖에서 수행했습니다. 토큰은 문자 수 기반 추정이며 LLM API·MCP 왕복 시간은 이 실험에서 계측하지 않았습니다.
- **32,000토큰 기준:** 누적 합이 아니라 단일 모델 요청 최대 입력 토큰(`max_request_in_tok > 32,000`)을 기준으로 `>32k Overflow` 비율을 판정합니다.

| 전략 | 이 실험의 구현 |
| :--- | :--- |
| `Native RAG` | 사전 구축한 단어·문자 3-gram TF-IDF 벡터의 코사인 Top-3 |
| `RAG + BM25 + RRF` | TF-IDF 순위와 Okapi BM25 순위(`k1=1.5`, `b=0.75`)를 RRF(`k=60`)로 합친 Top-3 |
| `DCI (Full)` | 해당 장애의 모든 청크를 판정기에 제공 |
| `DR-DCI` | 색인 창(`index_window=12`, distractor 감점 없음) + `AdaptiveEvidenceSelector`가 인출 본문 단서로 다음 청크를 결정. 색인 정렬에는 런북 가산점(+18), 본문이 직접 참조한 청크(+100), 단서·키워드 일치 점수가 쓰이며 정적 검색 두 방식에는 없는 DR-DCI 설계 요소입니다 |

---

## 1. 4대 패러다임 스케일링 종합 대시보드 (2×2)

![4대 검색 패러다임 선형 스케일링 대시보드](charts/scaling-4paradigm-dashboard.png)

## 2. Groundedness 안정성 및 `p50 / p99` Tail Latency 스케일링

![Groundedness 및 p50/p99 Tail Latency 스케일링](charts/scaling-4paradigm-groundedness-and-latency.png)

---

## 3. 오프라인 재측정 결과 (`N = 10 → 500` 전 구간)

| Scale (`N`) | Paradigm | Evidence Recall | Multi-Hop Recall | Groundedness | Macro-F1 | FSR | 평균 추정 입력 토큰 (vs DCI) | 단일 요청 >32k | 로컬 `p50 / p95 / p99` (ms) |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `10` | `native_rag` *(pre-indexed)* | 73.3% | 61.9% | 90.0% | 0.644 | 0.0% (0/18) | 266 (-79.1%) | 0% | **0.41 / 0.56 / 0.60** |
| `10` | `hybrid_rag_rrf` *(pre-indexed)* | 83.3% | 76.2% | 93.3% | 0.642 | 0.0% (0/18) | 236 (-81.4%) | 0% | 0.44 / 0.59 / 0.62 |
| `10` | `dci` | 100.0% | 100.0% | 100.0% | 0.789 | 0.0% (0/18) | 1,272 (baseline) | 0% | 1.05 / 1.71 / 1.78 |
| `10` | **`dr_dci`** | **100.0%** | **100.0%** | **100.0%** | **0.789** | **0.0% (0/18)** | **647 (-49.1%)** | **0%** | 0.46 / 0.86 / 0.95 |
| `50` | `native_rag` *(pre-indexed)* | 64.4% | 50.0% | 86.7% | 0.540 | 0.0% (0/18) | 290 (-96.0%) | 0% | 1.20 / 1.46 / 1.57 |
| `50` | `hybrid_rag_rrf` *(pre-indexed)* | 65.6% | 52.4% | 86.7% | 0.540 | 0.0% (0/18) | 287 (-96.1%) | 0% | 1.25 / 1.47 / 1.54 |
| `50` | `dci` | 100.0% | 100.0% | 100.0% | 0.789 | 0.0% (0/18) | 7,351 (baseline) | 0% | 6.13 / 9.34 / 9.69 |
| `50` | **`dr_dci`** | **96.1%** | **95.2%** | **96.7%** | **0.762** | **0.0% (0/18)** | **982 (-86.6%)** | **0%** | 0.99 / 1.69 / 1.76 |
| `100` | `native_rag` *(pre-indexed)* | 60.0% | 47.6% | 86.7% | 0.540 | 0.0% (0/18) | 302 (-98.0%) | 0% | 2.11 / 3.30 / 4.62 |
| `100` | `hybrid_rag_rrf` *(pre-indexed)* | 60.0% | 47.6% | 86.7% | 0.540 | 0.0% (0/18) | 302 (-98.0%) | 0% | 2.22 / 2.46 / 2.51 |
| `100` | `dci` | 100.0% | 100.0% | 100.0% | 0.789 | 0.0% (0/18) | 14,939 (baseline) | 0% | 12.64 / 18.52 / 19.11 |
| `100` | **`dr_dci`** | **92.2%** | **90.5%** | **96.7%** | **0.734** | **0.0% (0/18)** | **997 (-93.3%)** | **0%** | 1.52 / 2.27 / 2.90 |
| `200` | `native_rag` *(pre-indexed)* | 61.1% | 50.0% | 86.7% | 0.573 | 0.0% (0/18) | 299 (-99.0%) | 0% | 3.85 / 4.31 / 4.37 |
| `200` | `hybrid_rag_rrf` *(pre-indexed)* | 61.1% | 50.0% | 86.7% | 0.540 | 0.0% (0/18) | 298 (-99.0%) | 0% | 4.18 / 4.53 / 4.69 |
| `200` | `dci` | 100.0% | 100.0% | 100.0% | 0.789 | 0.0% (0/18) | 30,220 (baseline) | 0% | 25.13 / 36.78 / 37.68 |
| `200` | **`dr_dci`** | **86.7%** | **85.7%** | **93.3%** | **0.699** | **0.0% (0/18)** | **1,018 (-96.6%)** | **0%** | 2.41 / 3.78 / 4.48 |
| `300` | `native_rag` *(pre-indexed)* | 62.2% | 52.4% | 86.7% | 0.573 | 0.0% (0/18) | 295 (-99.4%) | 0% | 5.61 / 6.24 / 6.35 |
| `300` | `hybrid_rag_rrf` *(pre-indexed)* | 61.1% | 50.0% | 86.7% | 0.540 | 0.0% (0/18) | 298 (-99.3%) | 0% | 6.31 / 6.80 / 6.91 |
| `300` | `dci` | 100.0% | 100.0% | 100.0% | 0.789 | 0.0% (0/18) | 45,540 (baseline) | **100%** | 38.48 / 61.27 / 64.72 |
| `300` | **`dr_dci`** | **80.0%** | **71.4%** | **86.7%** | **0.706** | **0.0% (0/18)** | **1,049 (-97.7%)** | **0%** | 3.03 / 3.51 / 3.67 |
| `500` | `native_rag` *(pre-indexed)* | 63.3% | 54.8% | 86.7% | 0.573 | 0.0% (0/18) | 292 (-99.6%) | 0% | 9.19 / 10.03 / 10.62 |
| `500` | `hybrid_rag_rrf` *(pre-indexed)* | 62.2% | 52.4% | 86.7% | 0.540 | 0.0% (0/18) | 295 (-99.6%) | 0% | 10.39 / 12.29 / 12.91 |
| `500` | `dci` | 100.0% | 100.0% | 100.0% | 0.789 | 0.0% (0/18) | 76,161 (baseline) | **100%** | 64.20 / 96.16 / 111.62 |
| `500` | **`dr_dci`** | **74.4%** | **66.7%** | **86.7%** | **0.671** | **0.0% (0/18)** | **1,062 (-98.6%)** | **0%** | 4.58 / 5.07 / 8.57 |

---

## 4. 관측 결과와 한계

1. **전체 컨텍스트(`dci`)의 단일 요청 32k 한도 초과:** `N=200`에서는 30,220토큰으로 `>32k Overflow`가 0%였으나 `N=300`(45,540토큰)과 `N=500`(76,161토큰)에서는 100%(30/30) 한도를 초과했습니다.
2. **기본 설정의 관측 결과:** distractor 전용 감점을 제거한 뒤에도 기본 설정의 `DR-DCI`는 모든 구간(`N=10→500`)에서 `Native RAG`와 `RAG+BM25+RRF`보다 높은 Evidence Recall·Multi-Hop Recall·Macro-F1을 유지하면서 `DCI` 대비 추정 입력 토큰을 **-49.1% → -98.6%** 절감하고 `FSR 0.0%`·`>32k Overflow 0%`를 기록했습니다. 이 우위는 §5의 두 요소에 크게 의존합니다.
3. **고정 색인 창(`index_window=12`)의 한계(원인은 추정):** 다만 `N=200 → 500`으로 25% Hard Distractor 수가 늘어나면 고정 12슬롯 색인 사전 필터 창에 동점 distractor가 혼입되면서 `DR-DCI`의 Multi-Hop Recall이 `100.0% (N=10) → 90.5% (N=100) → 85.7% (N=200) → 71.4% (N=300) → 66.7% (N=500)`으로 점진 하락하고 Macro-F1도 `0.789 → 0.671`로 완만히 저하됩니다. 대용량 운영 코퍼스(`N≥200`)에서는 BM25/시간창 사전 랭킹 또는 `index_window` 동적 확장이 추가 개선 과제입니다.
4. **오프라인 vs 라이브 구분:** 이 표는 로컬 인메모리 오프라인 실측치이며, 실제 LLM API 지연과 토큰 사용량은 [라이브 실험 C](live-llm-benchmark-report.md)에 별도로 기록했습니다.

---

## 5. DR-DCI 설계 요소의 영향 (Multi-Hop Recall / Macro-F1)

`DR-DCI`에는 정적 검색 두 방식에 없는 두 가지 단서가 있습니다. 색인 창 정렬의 **런북 가산점(+18)**과, 합성 로그 본문이 다른 청크를 **ID로 직접 가리키는 참조**(`... 참조`)를 따라가는 동작입니다. 각각을 끄고 같은 조건에서 다시 측정했습니다. 참조 제거는 청크 ID를 일반 문구로 바꾸므로 모든 방식의 입력 데이터가 달라져, `native_rag`도 함께 적었습니다.

| `N` | `native_rag` (기본) | `native_rag` (참조 제거) | `dr_dci` (기본) | `dr_dci` (런북 가산점 0) | `dr_dci` (참조 제거) | `dr_dci` (둘 다) |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 10 | 61.9% / 0.644 | 64.3% / 0.644 | 100.0% / 0.789 | 100.0% / 0.789 | 78.6% / 0.762 | 78.6% / 0.762 |
| 50 | 50.0% / 0.540 | 52.4% / 0.540 | 95.2% / 0.762 | 95.2% / 0.762 | 64.3% / 0.671 | 64.3% / 0.671 |
| 100 | 47.6% / 0.540 | 50.0% / 0.540 | 90.5% / 0.734 | 90.5% / 0.734 | 61.9% / 0.671 | 61.9% / 0.671 |
| 200 | 50.0% / 0.573 | 52.4% / 0.573 | 85.7% / 0.699 | 83.3% / 0.671 | 59.5% / 0.634 | 52.4% / 0.605 |
| 300 | 52.4% / 0.573 | 52.4% / 0.573 | 71.4% / 0.706 | 66.7% / 0.611 | 52.4% / 0.671 | 45.2% / 0.575 |
| 500 | 54.8% / 0.573 | 54.8% / 0.573 | 66.7% / 0.671 | 47.6% / 0.427 | 50.0% / 0.634 | 30.9% / 0.384 |

- `N=500`에서 `native_rag`의 Multi-Hop Recall은 54.8%입니다. `DR-DCI`는 기본 66.7%, 런북 가산점 0일 때 47.6%, 참조 제거 시 50.0%, 둘 다 제거하면 30.9%입니다. 두 요소 중 하나만 없어도 `native_rag`보다 낮습니다.
- 따라서 이 표의 `DR-DCI` 우위를 검색 방식 일반의 우위로 해석할 수 없습니다. 실제 운영 로그에 청크 ID 참조가 있다고 가정할 수도 없습니다.
- 원시 결과: [`scaling-ablation-no-runbook-bonus.json`](results/scaling-ablation-no-runbook-bonus.json) · [`scaling-ablation-no-chunk-refs.json`](results/scaling-ablation-no-chunk-refs.json) · [`scaling-ablation-neither.json`](results/scaling-ablation-neither.json)

---

## 6. 재현 커맨드 (PowerShell 5.1)

```powershell
cd agent
$env:PYTHONPATH = "src"

python -m eval.scaling_benchmark `
  --scales 10,50,100,200,300,500 --incidents 30 --seed 42 --top-k 3 `
  --output-json ..\docs\eval\results\scaling-4paradigm.json `
  --output-csv ..\docs\eval\results\scaling-4paradigm.csv `
  --output-md ..\docs\eval\results\scaling-4paradigm.md

# 설계 요소 영향 측정(§5)
python -m eval.scaling_benchmark --runbook-bonus 0 --output-json ..\docs\eval\results\scaling-ablation-no-runbook-bonus.json
python -m eval.scaling_benchmark --strip-chunk-refs --output-json ..\docs\eval\results\scaling-ablation-no-chunk-refs.json
python -m eval.scaling_benchmark --runbook-bonus 0 --strip-chunk-refs --output-json ..\docs\eval\results\scaling-ablation-neither.json

python -m eval.plot `
  --scaling-json ..\docs\eval\results\scaling-4paradigm.json `
  --output-dir ..\docs\eval\charts `
  --prefix scaling-4paradigm
```

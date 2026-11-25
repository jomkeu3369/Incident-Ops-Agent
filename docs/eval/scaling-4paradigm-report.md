# 선형 데이터 증가(`N = 10 → 500`)에 따른 4대 검색 패러다임 성능 비교 리포트

> 장애당 청크 수를 10·50·100·200·300·500으로 바꿔, 4종 검색 전략의 근거 회수율·판정 품질·로컬 지연과 추정 입력 토큰을 비교했습니다. 각 크기에서 같은 합성 장애 30건을 확장해 사용했습니다.

- **원시 데이터:** [`results/scaling-4paradigm.json`](results/scaling-4paradigm.json) · [`results/scaling-4paradigm.csv`](results/scaling-4paradigm.csv) · [`results/scaling-4paradigm.md`](results/scaling-4paradigm.md)
- **데이터와 결과 이력:** 원시 JSON의 결과 스키마는 `linear-scaling-4paradigm-v1`입니다. `seed=42`, 크기별 30건, `top_k=3`을 기록했으며 별도 코퍼스 버전, `dev`/`eval`별 집계 및 실행 Git SHA는 기록되지 않았습니다. 원시 결과는 `f2f7f11`에 갱신됐습니다.
- **측정 단위:** 지연은 인메모리 검색과 `RuleBasedJudge`를 포함한 로컬 경과 시간입니다. 정적 검색 두 방식의 인덱스 준비는 타이머 밖에서 수행했습니다. 토큰은 문자 수 기반 추정이며 LLM API·MCP 왕복 시간은 이 실험에서 계측하지 않았습니다.
- **32,000토큰 기준:** 아래 비율은 저장된 실행의 추정 누적 입력이 실험 예산을 넘은 비율입니다. 현재 러너는 이후 변경(`b029836`)에서 단일 요청 기준으로 계측하므로 재실행 결과에는 새 기준을 적용합니다.

| 전략 | 이 실험의 구현 |
| :--- | :--- |
| `Native RAG` | 사전 구축한 단어·문자 3-gram TF-IDF 벡터의 코사인 Top-3 |
| `RAG + BM25 + RRF` | TF-IDF 순위와 Okapi BM25 순위(`k1=1.5`, `b=0.75`)를 RRF(`k=60`)로 합친 Top-3 |
| `DCI (Full)` | 해당 장애의 모든 청크를 판정기에 제공 |
| `DR-DCI` | `AdaptiveEvidenceSelector`가 인출한 본문의 단서에 따라 다음 청크를 결정 |

---

## 1. 4대 패러다임 스케일링 종합 대시보드 (2×2)

![4대 검색 패러다임 선형 스케일링 대시보드](charts/scaling-4paradigm-dashboard.png)

## 2. Groundedness 안정성 및 `p50 / p99` Tail Latency 스케일링

![Groundedness 및 p50/p99 Tail Latency 스케일링](charts/scaling-4paradigm-groundedness-and-latency.png)

---

## 3. 오프라인 결과 (`N = 10·100·500`)

| Scale (`N`) | Paradigm | Multi-Hop Recall | Groundedness | Macro-F1 | FSR | 평균 추정 입력 토큰 (vs DCI) | 추정 누적 입력 >32k | 로컬 `p50 / p95 / p99` (ms) |
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

## 4. 관측 결과와 적용 조건

1. **전체 컨텍스트의 추정 예산:** `N=200`의 예산 초과는 0/30, `N=300`과 `N=500`은 각각 30/30이었습니다. 예산 초과가 나타난 첫 측정 지점은 300청크입니다. 정확한 경계와 운영 API의 허용 범위는 이 비교로 확인할 수 없습니다.
2. **청크 500개에서의 절충:** DR-DCI의 평균 추정 입력은 992토큰으로 전체 컨텍스트 대비 98.7% 적었습니다. Multi-Hop Recall은 DR-DCI 90.5%, TF-IDF 54.8%, TF-IDF+BM25+RRF 50.0%, 전체 컨텍스트 100%였습니다.
3. **적용 가설:** 첫 인출 결과를 읽어야 다음 단서가 나오는 장애에는 순차 인출을 검토할 만합니다. 이 결과는 위 합성 코퍼스와 비교군 구현 기준이며, 라이브 API 지연과 사용량은 [라이브 실험 C](live-llm-benchmark-report.md)에 따로 기록했습니다.

---

## 5. 재현 커맨드 (PowerShell 5.1)

아래 명령으로 현재 체크아웃의 코드와 계측 기준을 사용한 새 결과를 생성합니다.

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

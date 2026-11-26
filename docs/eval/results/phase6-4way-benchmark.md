# Phase 6 · 에이전틱 DR-DCI 및 4종 검색 전략 정량 벤치마크 (`dr-dci-corpus-v1.0`)

- **코퍼스:** `dr-dci-corpus-v1.0` (총 **60건**, `max_pull_steps=3`, Judge=`RuleBasedJudge`, Selector=`adaptive_agentic`)
- **채택 게이트 판정:** **PASS (`agentic` 채택 기준 충족)** (추천 기본값: `agentic`)

## 1. 4종 검색 전략 종합 비교

| Strategy | Evidence Recall | Multi-Hop Recall | Groundedness | Ungrounded (↓) | Accuracy | Macro-F1 | **FSR (↓)** | Mean Input Tokens (vs Full) | Mean Pulls | p50 / p95 / p99 (ms) | Violations |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `full_context` | 100.0% | 100.0% | 100.0% | 0.0% | 91.7% (55/60) | 0.901 | **0.0%** (0/44) | 2456.2 (baseline) | 10.27 | 2.18 / 4.15 / 5.66 | 0/0 |
| `vector_rag` | 85.3% | 72.0% | 83.3% | 16.7% | 78.3% (47/60) | 0.784 | **0.0%** (0/44) | 230.0 (-90.6%) | 3.00 | 0.54 / 0.88 / 0.93 | 0/0 |
| `keyword_dr_dci` | 86.7% | 68.0% | 83.3% | 16.7% | 76.7% (46/60) | 0.755 | **0.0%** (0/44) | 223.1 (-90.9%) | 3.00 | 0.31 / 0.48 / 0.51 | 0/0 |
| `agentic_dr_dci` | 100.0% | 100.0% | 100.0% | 0.0% | 91.7% (55/60) | 0.901 | **0.0%** (0/44) | 658.3 (-73.2%) | 2.45 | 0.47 / 0.97 / 1.03 | 0/0 |

## 2. Dev / Eval 원형 분리 검증

| Split | Strategy | Evidence Recall | Multi-Hop Recall | Macro-F1 | FSR | Token Reduction |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `dev` (30건) | `full_context` | 100.0% | 100.0% | 0.936 | 0.0% (0/21) | 0.0% |
| `dev` (30건) | `vector_rag` | 93.3% | 66.7% | 0.936 | 0.0% (0/21) | 89.9% |
| `dev` (30건) | `keyword_dr_dci` | 98.9% | 83.3% | 0.936 | 0.0% (0/21) | 90.4% |
| `dev` (30건) | `agentic_dr_dci` | 100.0% | 100.0% | 0.936 | 0.0% (0/21) | 73.6% |
| `eval` (30건) | `full_context` | 100.0% | 100.0% | 0.842 | 0.0% (0/23) | 0.0% |
| `eval` (30건) | `vector_rag` | 77.2% | 72.5% | 0.621 | 0.0% (0/23) | 91.3% |
| `eval` (30건) | `keyword_dr_dci` | 74.5% | 66.7% | 0.512 | 0.0% (0/23) | 91.4% |
| `eval` (30건) | `agentic_dr_dci` | 100.0% | 100.0% | 0.842 | 0.0% (0/23) | 72.8% |

## 3. 에이전틱 DR-DCI가 불리하거나 손해 본 반례 사례 (투명 공개)

| Case ID | Category | Archetype | Keyword Recall / Tokens | Agentic Recall / Tokens | Stop Reason |
| :--- | :--- | :--- | :---: | :---: | :--- |
| `DRDCI-ADV-04` | `adversarial_edge` | `adv_short_corpus_keyword_optimal` | 100% / 104 tok | 100% / 190 tok | `STOP_SUFFICIENT` |
| `DRDCI-ADV-09` | `adversarial_edge` | `adv_misleading_ref_counterexample` | 100% / 95 tok | 100% / 457 tok | `STOP_SUFFICIENT` |

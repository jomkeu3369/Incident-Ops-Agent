# Phase 6 · 에이전틱 DR-DCI 및 4종 검색 전략 정량 벤치마크 (`dr-dci-corpus-v1.0`)

- **코퍼스:** `dr-dci-corpus-v1.0` (총 **60건**, `max_pull_steps=3`, Judge=`RuleBasedJudge`, Selector=`adaptive_agentic`)
- **채택 게이트 판정:** **PASS (`agentic` 채택 기준 충족)** (추천 기본값: `agentic`)

## 1. 4종 검색 전략 종합 비교

| Strategy | Evidence Recall | Multi-Hop Recall | Groundedness | Ungrounded (↓) | Accuracy | Macro-F1 | **FSR (↓)** | Mean Input Tokens (vs Full) | Mean Pulls | p50 / p95 / p99 (ms) | Violations |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `full_context` | 100.0% | 100.0% | 100.0% | 0.0% | 91.7% (55/60) | 0.901 | **0.0%** (0/44) | 2456.2 (baseline) | 10.27 | 2.11 / 3.63 / 3.72 | 0/0 |
| `vector_rag` | 85.3% | 72.0% | 83.3% | 16.7% | 78.3% (47/60) | 0.784 | **0.0%** (0/44) | 230.0 (-90.6%) | 3.00 | 2.59 / 3.70 / 4.42 | 0/0 |
| `keyword_dr_dci` | 85.0% | 66.7% | 93.3% | 6.7% | 65.0% (39/60) | 0.650 | **0.0%** (0/44) | 182.2 (-92.6%) | 2.98 | 0.19 / 0.36 / 0.43 | 0/0 |
| `agentic_dr_dci` | 100.0% | 100.0% | 100.0% | 0.0% | 91.7% (55/60) | 0.901 | **0.0%** (0/44) | 658.3 (-73.2%) | 2.45 | 0.39 / 0.71 / 0.96 | 0/0 |

## 2. Dev / Eval 원형 분리 검증

| Split | Strategy | Evidence Recall | Multi-Hop Recall | Macro-F1 | FSR | Token Reduction |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `dev` (24건) | `full_context` | 100.0% | 100.0% | 0.915 | 0.0% (0/16) | 0.0% |
| `dev` (24건) | `vector_rag` | 84.7% | 66.7% | 0.755 | 0.0% (0/16) | 90.2% |
| `dev` (24건) | `keyword_dr_dci` | 87.5% | 66.7% | 0.755 | 0.0% (0/16) | 92.3% |
| `dev` (24건) | `agentic_dr_dci` | 100.0% | 100.0% | 0.915 | 0.0% (0/16) | 73.1% |
| `eval` (36건) | `full_context` | 100.0% | 100.0% | 0.886 | 0.0% (0/28) | 0.0% |
| `eval` (36건) | `vector_rag` | 85.7% | 74.5% | 0.800 | 0.0% (0/28) | 90.9% |
| `eval` (36건) | `keyword_dr_dci` | 83.3% | 66.7% | 0.569 | 0.0% (0/28) | 92.7% |
| `eval` (36건) | `agentic_dr_dci` | 100.0% | 100.0% | 0.886 | 0.0% (0/28) | 73.3% |

## 3. 에이전틱 DR-DCI가 불리하거나 손해 본 반례 사례 (투명 공개)

| Case ID | Category | Archetype | Keyword Recall / Tokens | Agentic Recall / Tokens | Stop Reason |
| :--- | :--- | :--- | :---: | :---: | :--- |
| `DRDCI-ADV-03` | `adversarial_edge` | `adv_short_corpus_keyword_optimal` | 100% / 97 tok | 100% / 190 tok | `STOP_SUFFICIENT` |
| `DRDCI-ADV-04` | `adversarial_edge` | `adv_misleading_ref_counterexample` | 100% / 95 tok | 100% / 457 tok | `STOP_SUFFICIENT` |

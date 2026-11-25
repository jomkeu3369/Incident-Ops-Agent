# Phase 6 · 에이전틱 DR-DCI 및 4종 검색 전략 정량 벤치마크 (`dr-dci-corpus-v1.0`)

- **코퍼스:** `dr-dci-corpus-v1.0` (총 **36건**, `max_pull_steps=3`, Judge=`StructuredOutputJudge`, Selector=`openai_agentic`)
- **채택 게이트 판정:** **HOLD (`keyword` 기본값 유지)** (추천 기본값: `keyword`)

## 1. 4종 검색 전략 종합 비교

| Strategy | Evidence Recall | Multi-Hop Recall | Groundedness | Ungrounded (↓) | Accuracy | Macro-F1 | **FSR (↓)** | Mean Input Tokens (vs Full) | Mean Pulls | p50 / p95 / p99 (ms) | Violations |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `full_context` | 100.0% | 100.0% | 100.0% | 0.0% | 100.0% (36/36) | 1.000 | **0.0%** (0/28) | 3163.4 (baseline) | 10.47 | 2793.53 / 3922.84 / 4259.94 | 0/0 |
| `vector_rag` | 85.7% | 74.5% | 97.2% | 2.8% | 66.7% (24/36) | 0.690 | **0.0%** (0/28) | 671.6 (-78.8%) | 3.00 | 3089.80 / 4767.36 / 5628.49 | 0/0 |
| `keyword_dr_dci` | 83.3% | 66.7% | 100.0% | 0.0% | 52.8% (19/36) | 0.510 | **0.0%** (0/28) | 660.2 (-79.1%) | 3.00 | 3381.91 / 6695.62 / 7371.73 | 0/0 |
| `agentic_dr_dci` | 100.0% | 100.0% | 100.0% | 0.0% | 100.0% (36/36) | 1.000 | **0.0%** (0/28) | 3332.8 (+5.3%) | 2.53 | 12141.83 / 17057.05 / 22028.34 | 0/0 |

## 2. Dev / Eval 원형 분리 검증

| Split | Strategy | Evidence Recall | Multi-Hop Recall | Macro-F1 | FSR | Token Reduction |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `eval` (36건) | `full_context` | 100.0% | 100.0% | 1.000 | 0.0% (0/28) | 0.0% |
| `eval` (36건) | `vector_rag` | 85.7% | 74.5% | 0.690 | 0.0% (0/28) | 78.8% |
| `eval` (36건) | `keyword_dr_dci` | 83.3% | 66.7% | 0.510 | 0.0% (0/28) | 79.1% |
| `eval` (36건) | `agentic_dr_dci` | 100.0% | 100.0% | 1.000 | 0.0% (0/28) | -5.3% |

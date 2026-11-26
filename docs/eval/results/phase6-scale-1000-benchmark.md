# Phase 6 · 에이전틱 DR-DCI 및 4종 검색 전략 정량 벤치마크 (`dr-dci-synthetic-n1000-s42`)

- **코퍼스:** `dr-dci-synthetic-n1000-s42` (총 **1000건**, `max_pull_steps=3`, Judge=`RuleBasedJudge`, Selector=`adaptive_agentic`)
- **채택 게이트 판정:** **PASS (`agentic` 채택 기준 충족)** (추천 기본값: `agentic`)

## 1. 4종 검색 전략 종합 비교

| Strategy | Evidence Recall | Multi-Hop Recall | Groundedness | Ungrounded (↓) | Accuracy | Macro-F1 | **FSR (↓)** | Mean Input Tokens (vs Full) | Mean Pulls | p50 / p95 / p99 (ms) | Violations |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `full_context` | 100.0% | 100.0% | 100.0% | 0.0% | 88.3% (883/1000) | 0.873 | **0.0%** (0/652) | 2148.7 (baseline) | 13.01 | 1.90 / 3.46 / 4.73 | 0/0 |
| `vector_rag` | 90.8% | 81.0% | 91.5% | 8.5% | 76.8% (768/1000) | 0.773 | **0.0%** (0/652) | 216.7 (-89.9%) | 3.00 | 0.47 / 0.62 / 0.75 | 0/0 |
| `keyword_dr_dci` | 84.6% | 66.7% | 92.2% | 7.8% | 65.3% (653/1000) | 0.662 | **0.0%** (0/652) | 170.6 (-92.1%) | 2.96 | 0.20 / 0.39 / 0.50 | 0/0 |
| `agentic_dr_dci` | 100.0% | 100.0% | 100.0% | 0.0% | 88.3% (883/1000) | 0.873 | **0.0%** (0/652) | 819.2 (-61.9%) | 2.46 | 0.47 / 0.85 / 1.08 | 0/0 |

## 2. Dev / Eval 원형 분리 검증

| Split | Strategy | Evidence Recall | Multi-Hop Recall | Macro-F1 | FSR | Token Reduction |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `dev` (462건) | `full_context` | 100.0% | 100.0% | 0.848 | 0.0% (0/230) | 0.0% |
| `dev` (462건) | `vector_rag` | 97.5% | 96.6% | 0.848 | 0.0% (0/230) | 89.0% |
| `dev` (462건) | `keyword_dr_dci` | 91.6% | 66.7% | 0.848 | 0.0% (0/230) | 91.2% |
| `dev` (462건) | `agentic_dr_dci` | 100.0% | 100.0% | 0.848 | 0.0% (0/230) | 61.8% |
| `eval` (538건) | `full_context` | 100.0% | 100.0% | 0.895 | 0.0% (0/422) | 0.0% |
| `eval` (538건) | `vector_rag` | 85.1% | 77.5% | 0.731 | 0.0% (0/422) | 90.7% |
| `eval` (538건) | `keyword_dr_dci` | 78.6% | 66.7% | 0.491 | 0.0% (0/422) | 92.8% |
| `eval` (538건) | `agentic_dr_dci` | 100.0% | 100.0% | 0.895 | 0.0% (0/422) | 62.0% |

## 3. 에이전틱 DR-DCI가 불리하거나 손해 본 반례 사례 (투명 공개)

| Case ID | Category | Archetype | Keyword Recall / Tokens | Agentic Recall / Tokens | Stop Reason |
| :--- | :--- | :--- | :---: | :---: | :--- |
| `SYN-0024` | `adversarial_edge` | `adv_short_counterexample_dev` | 100% / 109 tok | 100% / 247 tok | `STOP_SUFFICIENT` |
| `SYN-0026` | `adversarial_edge` | `adv_misleading_ref_eval` | 100% / 104 tok | 100% / 739 tok | `STOP_SUFFICIENT` |
| `SYN-0050` | `adversarial_edge` | `adv_short_counterexample_dev` | 100% / 108 tok | 100% / 249 tok | `STOP_SUFFICIENT` |
| `SYN-0052` | `adversarial_edge` | `adv_misleading_ref_eval` | 100% / 105 tok | 100% / 740 tok | `STOP_SUFFICIENT` |
| `SYN-0076` | `adversarial_edge` | `adv_short_counterexample_dev` | 100% / 108 tok | 100% / 246 tok | `STOP_SUFFICIENT` |
| `SYN-0078` | `adversarial_edge` | `adv_misleading_ref_eval` | 100% / 103 tok | 100% / 738 tok | `STOP_SUFFICIENT` |
| `SYN-0102` | `adversarial_edge` | `adv_short_counterexample_dev` | 100% / 111 tok | 100% / 252 tok | `STOP_SUFFICIENT` |
| `SYN-0104` | `adversarial_edge` | `adv_misleading_ref_eval` | 100% / 104 tok | 100% / 745 tok | `STOP_SUFFICIENT` |
| `SYN-0128` | `adversarial_edge` | `adv_short_counterexample_dev` | 100% / 109 tok | 100% / 247 tok | `STOP_SUFFICIENT` |
| `SYN-0130` | `adversarial_edge` | `adv_misleading_ref_eval` | 100% / 105 tok | 100% / 740 tok | `STOP_SUFFICIENT` |

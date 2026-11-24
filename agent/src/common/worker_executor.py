from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Awaitable, Callable

# 수정 가능한 실패 코드: 파라미터 보정 후 재시도 대상
RETRYABLE_ERROR_CODES = frozenset({"NO_RESULT", "INSUFFICIENT_DATA", "INVALID_INPUT"})

ToolRunnerFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
RepairArgsFn = Callable[[str, dict[str, Any], dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PlannedToolStep:
    """Planner가 결정한 도구 실행 단계 (불변 고정 인자와 가변 인자를 명시 분리)."""

    tool_name: str
    fixed_args: dict[str, Any]
    mutable_args: dict[str, Any] = field(default_factory=dict)

    def merged_args(self, override_mutable: dict[str, Any] | None = None) -> dict[str, Any]:
        """고정 인자를 최우선으로 덮어써 불변성을 강제한다."""
        merged = dict(self.mutable_args)
        if override_mutable:
            merged.update(override_mutable)
        # 고정 인자는 LLM 보정 결과에 의해 절대 변조될 수 없다
        merged.update(self.fixed_args)
        return merged


@dataclass(frozen=True)
class WorkerStepOutcome:
    """분리형 Worker 실행 단건 결과 및 메트릭."""

    tool_name: str
    ok: bool
    result: dict[str, Any]
    attempts: int
    worker_llm_calls: int
    fixed_args_preserved: bool
    elapsed_ms: float
    used_args: dict[str, Any]


@dataclass(frozen=True)
class SplitComparisonMetrics:
    """통합형(Monolithic) vs Planner·Worker 분리형(Split) 전후 지표 비교 결과."""

    total_steps: int
    monolithic_llm_calls: int
    split_planner_llm_calls: int
    split_worker_llm_calls: int
    split_total_llm_calls: int
    llm_call_reduction_percent: float
    split_success_count: int
    fixed_args_preserved_all: bool
    total_elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "totalSteps": self.total_steps,
            "monolithicLlmCalls": self.monolithic_llm_calls,
            "splitPlannerLlmCalls": self.split_planner_llm_calls,
            "splitWorkerLlmCalls": self.split_worker_llm_calls,
            "splitTotalLlmCalls": self.split_total_llm_calls,
            "llmCallReductionPercent": self.llm_call_reduction_percent,
            "splitSuccessCount": self.split_success_count,
            "fixedArgsPreservedAll": self.fixed_args_preserved_all,
            "totalElapsedMs": self.total_elapsed_ms,
        }


class SplitWorkerExecutor:
    """정상 경로 Worker LLM 0회, 수정 가능 실패에만 고정 인자 보존 재시도를 수행하는 실행기."""

    def __init__(
        self,
        tool_runner: ToolRunnerFn,
        repair_fn: RepairArgsFn | None = None,
        *,
        max_retries: int = 1,
    ) -> None:
        self._run_tool = tool_runner
        self._repair_fn = repair_fn
        self._max_retries = max(0, max_retries)

    async def execute_step(self, step: PlannedToolStep) -> WorkerStepOutcome:
        """단일 단계를 결정론적으로 실행하고, 수정 가능한 실패 시에만 가변 인자를 누적 보정해 재시도한다."""
        t0 = time.perf_counter()
        accumulated_mutable = dict(step.mutable_args)
        current_args = step.merged_args(accumulated_mutable)
        attempts = 1
        worker_llm_calls = 0

        res = await self._run_tool(step.tool_name, current_args)
        while not res.get("ok") and attempts <= self._max_retries:
            err = res.get("error") if isinstance(res.get("error"), dict) else {}
            err_code = str(err.get("code") or "")
            if err_code not in RETRYABLE_ERROR_CODES or self._repair_fn is None:
                break

            worker_llm_calls += 1
            repaired_candidate = await self._repair_fn(step.tool_name, dict(step.fixed_args), dict(current_args), err)
            if isinstance(repaired_candidate, dict):
                accumulated_mutable.update(repaired_candidate)
            # 누적된 가변 보정값 위로 고정 인자를 강제 복원해 불변성을 유지한다
            current_args = step.merged_args(accumulated_mutable)
            attempts += 1
            res = await self._run_tool(step.tool_name, current_args)

        fixed_preserved = all(current_args.get(k) == v for k, v in step.fixed_args.items())
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return WorkerStepOutcome(
            tool_name=step.tool_name,
            ok=bool(res.get("ok")),
            result=res,
            attempts=attempts,
            worker_llm_calls=worker_llm_calls,
            fixed_args_preserved=fixed_preserved,
            elapsed_ms=elapsed_ms,
            used_args=current_args,
        )

    async def execute_plan_and_compare(
        self,
        steps: list[PlannedToolStep],
        *,
        planner_llm_calls: int = 1,
    ) -> tuple[list[WorkerStepOutcome], SplitComparisonMetrics]:
        """계획 전체를 분리형 Worker로 실행하고, 단계마다 LLM을 부르는 통합형 대비 지표를 산출한다."""
        outcomes: list[WorkerStepOutcome] = []
        for step in steps:
            outcomes.append(await self.execute_step(step))

        # 통합형 기준: 초기 계획 1회 + 각 단계 도구 호출 판단·재시도 시도마다 LLM 1회
        monolithic_calls = 1 + sum(o.attempts for o in outcomes)
        worker_calls = sum(o.worker_llm_calls for o in outcomes)
        split_total = planner_llm_calls + worker_calls
        reduction = round(((monolithic_calls - split_total) / monolithic_calls) * 100.0, 2) if monolithic_calls > 0 else 0.0

        metrics = SplitComparisonMetrics(
            total_steps=len(outcomes),
            monolithic_llm_calls=monolithic_calls,
            split_planner_llm_calls=planner_llm_calls,
            split_worker_llm_calls=worker_calls,
            split_total_llm_calls=split_total,
            llm_call_reduction_percent=reduction,
            split_success_count=sum(1 for o in outcomes if o.ok),
            fixed_args_preserved_all=all(o.fixed_args_preserved for o in outcomes),
            total_elapsed_ms=round(sum(o.elapsed_ms for o in outcomes), 2),
        )
        return outcomes, metrics

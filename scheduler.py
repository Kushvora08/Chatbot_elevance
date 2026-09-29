from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .models import EvaluationCase, UpdateResult
from .orchestrator import KnowledgeBasePipeline, parse_datetime, parse_hhmm


class PipelineScheduler:
    def __init__(self, pipeline: KnowledgeBasePipeline) -> None:
        self.pipeline = pipeline

    def run_due(
        self,
        token: str | None = "admin-token",
        eval_cases: Iterable[EvaluationCase | Mapping[str, Any]] | None = None,
        now: datetime | None = None,
        health_checker: Any = None,
    ) -> UpdateResult:
        self.pipeline.access.authorize(token, "schedule")
        now = now or datetime.now(timezone.utc)
        state = self.pipeline.store.read_scheduler_state()

        pending_result = self._activate_pending_if_allowed(token, now, health_checker)
        if pending_result is not None:
            self._record_result(state, pending_result, now)
            return pending_result

        if not self._due_for_update(state, now):
            return UpdateResult(status="not_due", active_version_id=self.pipeline.store.active_version_id())

        result = self.pipeline.run_update(token=token, eval_cases=eval_cases, now=now, activate=True, health_checker=health_checker)
        self._record_result(state, result, now)
        return result

    def next_retry_at(self, failed_at: datetime, retry_index: int) -> datetime | None:
        delays = self.pipeline.config.schedule.retry_minutes
        if retry_index >= len(delays):
            return None
        return failed_at + timedelta(minutes=delays[retry_index])

    def _activate_pending_if_allowed(self, token: str | None, now: datetime, health_checker: Any) -> UpdateResult | None:
        manifest = self.pipeline.store.read_manifest()
        pending = list(manifest.get("pending_versions", []))
        if pending and self.pipeline.in_maintenance_window(now):
            return self.pipeline.activate_pending(token=token, now=now, health_checker=health_checker)
        return None

    def _due_for_update(self, state: dict[str, Any], now: datetime) -> bool:
        retry_at = parse_datetime(state.get("next_retry_at"))
        if retry_at and now >= retry_at:
            return True
        scheduled = scheduled_time_for_day(now, self.pipeline.config.schedule.update_time)
        last_attempt = parse_datetime(state.get("last_attempt_at"))
        return now >= scheduled and (last_attempt is None or last_attempt < scheduled)

    def _record_result(self, state: dict[str, Any], result: UpdateResult, now: datetime) -> None:
        state["last_attempt_at"] = now.isoformat()
        if result.status in {"failed", "rolled_back"}:
            retry_index = int(state.get("retry_index", 0))
            next_retry = self.next_retry_at(now, retry_index)
            state["retry_index"] = retry_index + 1
            state["next_retry_at"] = next_retry.isoformat() if next_retry else None
            state["last_failure"] = result.errors[0] if result.errors else "update failed"
        elif result.status in {"activated", "approved_pending_maintenance", "no_changes"}:
            state["last_successful_update_at"] = now.isoformat()
            state["retry_index"] = 0
            state["next_retry_at"] = None
            state["last_failure"] = None
        self.pipeline.store.write_scheduler_state(state)


def scheduled_time_for_day(now: datetime, update_time: str) -> datetime:
    parsed = parse_hhmm(update_time)
    return now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)

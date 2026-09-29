from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from time import monotonic
from typing import Any
import json

from .config import HealthConfig
from .models import HealthReport


class Timer:
    def __init__(self) -> None:
        self.started = monotonic()

    @property
    def elapsed_ms(self) -> float:
        return (monotonic() - self.started) * 1000.0


class MetricsStore:
    def __init__(self, state_dir: str | Path, health_config: HealthConfig) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "metrics.jsonl"
        self.health_config = health_config
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def record(self, event_type: str, **payload: Any) -> None:
        event = {
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def record_query(self, latency_ms: float, confidence: float, failed: bool, escalated: bool) -> None:
        self.record(
            "query",
            latency_ms=round(latency_ms, 4),
            confidence=round(confidence, 4),
            failed=failed,
            escalated=escalated,
        )

    def record_update(self, status: str, latency_ms: float, version_id: str | None, errors: list[str] | None = None) -> None:
        self.record(
            "update",
            status=status,
            latency_ms=round(latency_ms, 4),
            version_id=version_id,
            errors=errors or [],
            failed=status in {"failed", "rolled_back"},
        )

    def record_health(self, report: HealthReport) -> None:
        self.record("health", **report.to_dict())

    def recent_events(self, minutes: int = 5) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = json.loads(line)
                recorded_at = _parse_datetime(event.get("recorded_at"))
                if recorded_at and recorded_at >= cutoff:
                    events.append(event)
        return events

    def health_report(self, version_id: str | None, minutes: int = 5) -> HealthReport:
        query_events = [event for event in self.recent_events(minutes) if event.get("event_type") == "query"]
        if not query_events:
            return HealthReport(status="pass", version_id=version_id, details="no recent query traffic")
        latency = mean(float(event.get("latency_ms", 0.0)) for event in query_events)
        failure_rate = mean(1.0 if event.get("failed") else 0.0 for event in query_events)
        confidence = mean(float(event.get("confidence", 0.0)) for event in query_events)
        escalation_rate = mean(1.0 if event.get("escalated") else 0.0 for event in query_events)
        failures: list[str] = []
        if latency > self.health_config.max_latency_ms:
            failures.append("latency")
        if failure_rate > self.health_config.max_failure_rate:
            failures.append("failure_rate")
        if confidence < self.health_config.min_average_confidence:
            failures.append("confidence")
        if escalation_rate > self.health_config.max_escalation_rate:
            failures.append("escalations")
        return HealthReport(
            status="fail" if failures else "pass",
            version_id=version_id,
            latency_ms=round(latency, 4),
            failure_rate=round(failure_rate, 4),
            average_confidence=round(confidence, 4),
            escalation_rate=round(escalation_rate, 4),
            details=", ".join(failures),
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


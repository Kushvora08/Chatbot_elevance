from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
import hashlib
import inspect
import time as time_module

from .config import PipelineConfig
from .document import DocumentLoader, DocumentValidationError, document_map
from .evidence import EvidenceAnalyzer, FileReference
from .models import (
    DocumentRecord,
    EvaluationCase,
    EvidenceAnalysisResult,
    HealthReport,
    QueryResult,
    QuarantineRecord,
    RetrievedContext,
    UpdateResult,
    VersionRecord,
    normalize_health_status,
    utc_now_iso,
)
from .monitoring import MetricsStore, Timer
from .quality import QualityGate
from .rag import KnowledgeAssistant
from .security import AccessController, PromptInjectionProtector, SensitiveDataMasker
from .storage import JsonStore
from .tickets import TicketWorkflow
from .sentiment import ConversationWorkflow
from .sessions import SessionWorkflow


HealthChecker = Callable[..., HealthReport | Mapping[str, Any] | bool]


class KnowledgeBasePipeline:
    def __init__(self, config: PipelineConfig | Mapping[str, Any] | str | Path | None = None) -> None:
        if isinstance(config, PipelineConfig):
            self.config = config
        elif isinstance(config, (str, Path)):
            self.config = PipelineConfig.from_file(config)
        else:
            self.config = PipelineConfig.from_dict(config)
        self.store = JsonStore(self.config)
        self.loader = DocumentLoader(self.config)
        self.access = AccessController(self.config.security)
        self.quality = QualityGate(self.config.quality, self.config.rag)
        self.metrics = MetricsStore(self.config.storage.state_dir, self.config.health)
        self.masker = SensitiveDataMasker()
        self.injection = PromptInjectionProtector()
        self.evidence = EvidenceAnalyzer(self.config, self.access, self.metrics)
        self.tickets = TicketWorkflow(self.config, self.access, self.evidence)
        self.conversations = ConversationWorkflow(self.tickets)
        self.sessions = SessionWorkflow(self)

    def respond(self, conversation: Mapping[str, Any], token=None, now=None, **query_options) -> dict:
        self.access.authorize(token, "query")
        timer = Timer()
        monitored = self.conversations.receive(conversation, token, now)
        # Persist safety decisions before retrieval so an answer-service outage cannot lose an escalation.
        try:
            answer = self.query(conversation["message"], token=token,
                                now=datetime.fromisoformat(monitored.get("assessed_at", monitored["updated_at"])),
                                **query_options).to_dict()
        except Exception as exc:
            self.metrics.record("conversation_answer_failure", error=type(exc).__name__)
            answer = {"answer": "The knowledge service is unavailable. No policy answer is available.",
                      "status": "unavailable", "citations": [], "escalated": True}
        self.metrics.record("conversation_analysis", latency_ms=round(timer.elapsed_ms, 3),
                            confidence=monitored["analysis"]["confidence"], label=monitored["analysis"]["primary"],
                            language=monitored["analysis"]["language"], escalated=monitored["escalated"],
                            queue=monitored["queue"], failed=answer["status"] == "unavailable")
        return {"conversation": monitored, "knowledge_answer": answer, "tone": monitored["tone"]}

    def run_update(
        self,
        token: str | None = "admin-token",
        eval_cases: Iterable[EvaluationCase | Mapping[str, Any]] | None = None,
        now: datetime | None = None,
        activate: bool = True,
        health_checker: HealthChecker | Iterable[HealthReport | Mapping[str, Any] | bool] | None = None,
    ) -> UpdateResult:
        self.access.authorize(token, "update")
        timer = Timer()
        now = now or datetime.now(timezone.utc)
        cases = [EvaluationCase.from_any(case) for case in (eval_cases or [])]
        try:
            result, documents = self._build_candidate(cases, now)
            if result.status in {"no_changes", "rejected", "failed"}:
                self.metrics.record_update(result.status, timer.elapsed_ms, result.version_id, result.errors)
                return result
            if not activate:
                self.store.add_pending_version(result.version_id or "")
                result.status = "approved_pending_maintenance"
                result.pending_activation = True
                self.metrics.record_update(result.status, timer.elapsed_ms, result.version_id)
                return result
            activation = self.activate_version(
                result.version_id or "",
                token=token,
                now=now,
                health_checker=health_checker,
                _documents=documents,
            )
            activation.processed = result.processed
            activation.skipped = result.skipped
            activation.removed = result.removed
            activation.quarantined = result.quarantined
            activation.duplicates = result.duplicates
            activation.quality_report = result.quality_report
            self.metrics.record_update(activation.status, timer.elapsed_ms, activation.version_id, activation.errors)
            return activation
        except Exception as exc:
            result = UpdateResult(status="failed", errors=[str(exc)])
            self.metrics.record_update(result.status, timer.elapsed_ms, result.version_id, result.errors)
            return result

    def activate_pending(
        self,
        token: str | None = "admin-token",
        now: datetime | None = None,
        health_checker: HealthChecker | Iterable[HealthReport | Mapping[str, Any] | bool] | None = None,
    ) -> UpdateResult:
        self.access.authorize(token, "activate")
        manifest = self.store.read_manifest()
        pending = list(manifest.get("pending_versions", []))
        if not pending:
            return UpdateResult(status="no_pending_version", active_version_id=self.store.active_version_id())
        version_id = pending[-1]
        return self.activate_version(version_id, token=token, now=now, health_checker=health_checker)

    def activate_version(
        self,
        version_id: str,
        token: str | None = "admin-token",
        now: datetime | None = None,
        health_checker: HealthChecker | Iterable[HealthReport | Mapping[str, Any] | bool] | None = None,
        _documents: list[DocumentRecord] | None = None,
    ) -> UpdateResult:
        self.access.authorize(token, "activate")
        now = now or datetime.now(timezone.utc)
        if self.config.schedule.activate_only_in_maintenance and not self.in_maintenance_window(now):
            self.store.add_pending_version(version_id)
            return UpdateResult(
                status="approved_pending_maintenance",
                version_id=version_id,
                active_version_id=self.store.active_version_id(),
                pending_activation=True,
            )
        documents = _documents if _documents is not None else self.store.read_documents(version_id)
        previous = self.store.activate_version(version_id, documents, activated_at=now)
        result = UpdateResult(status="activated", version_id=version_id, active_version_id=version_id)
        try:
            failed_report = self._watch_activation(version_id, now, health_checker)
        except Exception as exc:
            failed_report = HealthReport(status="fail", version_id=version_id, details=str(exc))
        if failed_report is not None:
            if previous:
                rolled_to = self.store.rollback(previous)
            else:
                self.store.clear_active_version()
                rolled_to = None
            result.status = "rolled_back"
            result.active_version_id = rolled_to
            result.health_report = failed_report.to_dict()
            result.errors.append(f"health check failed during activation grace period: {failed_report.details}")
        return result

    def rollback(
        self,
        token: str | None = "admin-token",
        target_version_id: str | None = None,
        reason: str = "",
    ) -> UpdateResult:
        self.access.authorize(token, "rollback")
        rolled_to = self.store.rollback(target_version_id)
        status = "rolled_back" if rolled_to else "rollback_unavailable"
        self.metrics.record("rollback", target_version_id=target_version_id, active_version_id=rolled_to, reason=reason)
        return UpdateResult(status=status, active_version_id=rolled_to)

    def query(self, question: str, token: str | None = "service-token", top_k: int | None = None,
              product: str | None = None, region: str | None = None, as_of: str | None = None,
              now: datetime | None = None, proposed_answer: str | None = None) -> QueryResult:
        role = self.access.authorize(token, "query")
        timer = Timer()
        failed = False
        escalated = False
        masked = False
        try:
            active_version_id = self.store.active_version_id()
            documents = self.store.read_knowledge_documents(active_version_id)
            safe_question, masked = self.masker.mask(question)
            result = KnowledgeAssistant(self.config.rag).answer(documents, safe_question, role, active_version_id,
                product=product, region=region, as_of=as_of, now=now, top_k=top_k, proposed_answer=proposed_answer)
            result.masked = masked
            self.metrics.record_query(timer.elapsed_ms, result.confidence, failed=False, escalated=result.escalated)
            self.metrics.record("rag_answer", status=result.status, citation_count=len(result.citations), as_of=result.as_of)
            return result
        except Exception:
            failed = True
            self.metrics.record_query(timer.elapsed_ms, 0.0, failed=True, escalated=True)
            raise
        finally:
            if failed:
                self.metrics.record("query_failure")

    def health_check(self, token: str | None = "service-token", version_id: str | None = None) -> HealthReport:
        self.access.authorize(token, "health")
        report = self.metrics.health_report(version_id or self.store.active_version_id())
        self.metrics.record_health(report)
        return report

    def knowledge_source(self, path: str, version_id: str, token: str | None, as_of: str | None = None,
                         now: datetime | None = None, content_hash: str | None = None) -> dict[str, Any]:
        role = self.access.authorize(token, "query")
        if not version_id or version_id != self.store.active_version_id():
            raise ValueError("The source snapshot has changed. Please ask the question again.")
        assistant = KnowledgeAssistant(self.config.rag)
        day, historical = assistant._date("", as_of, now or datetime.now(timezone.utc))
        documents = assistant.authorized_documents(self.store.read_knowledge_documents(version_id), role)
        document = next((item for item in assistant._applicable(documents, day, historical) if item.path == path
                         and (content_hash is None or item.content_hash == content_hash)), None)
        if document is None:
            raise ValueError("The source is unavailable for this access level or date.")
        return {"path": document.path, "title": document.title, "content": self.masker.mask(document.masked_content)[0],
                "metadata": document.metadata, "content_hash": document.content_hash, "version_id": version_id}

    def metrics_snapshot(self, token: str | None = "service-token", minutes: int = 5) -> dict[str, Any]:
        self.access.authorize(token, "metrics")
        events = self.metrics.recent_events(minutes)
        return {"window_minutes": minutes, "events": events}

    def analyze_evidence(
        self,
        message: str,
        file_refs: Iterable[str],
        token: str | None = "service-token",
        now: datetime | None = None,
        simulate_processing_seconds: float = 0.0,
    ) -> EvidenceAnalysisResult:
        return self.evidence.analyze(
            message=message,
            file_refs=file_refs,
            token=token,
            now=now,
            simulate_processing_seconds=simulate_processing_seconds,
        )

    def save_evidence_upload(
        self,
        filename: str,
        data: bytes,
        token: str | None = "service-token",
        now: datetime | None = None,
    ) -> FileReference:
        return self.evidence.save_upload(filename, data, token=token, now=now)

    def cleanup_evidence_uploads(self, token: str | None = "service-token", now: datetime | None = None) -> list[str]:
        return self.evidence.cleanup_expired(token=token, now=now)

    def evidence_status(self, now: datetime | None = None) -> dict[str, Any]:
        return self.evidence.status(now)

    def in_maintenance_window(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        current = now.time()
        start = parse_hhmm(self.config.schedule.maintenance_start)
        end = parse_hhmm(self.config.schedule.maintenance_end)
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def _build_candidate(
        self,
        eval_cases: list[EvaluationCase],
        now: datetime,
    ) -> tuple[UpdateResult, list[DocumentRecord]]:
        manifest = self.store.read_manifest()
        active_version_id = manifest.get("active_version_id")
        active_documents = self.store.read_documents(active_version_id)
        active_by_path = document_map(active_documents)
        known_documents = dict(manifest.get("documents", {}))
        current_files = self.loader.discover()
        current_paths = {self.loader.rel_path(path) for path in current_files}
        processed = 0
        skipped = 0
        removed = len([path for path in active_by_path if path not in current_paths])
        quarantined_records: list[QuarantineRecord] = []
        provisional: list[DocumentRecord] = []

        for path in current_files:
            rel_path = self.loader.rel_path(path)
            known = known_documents.get(rel_path)
            if self.loader.unchanged(known, path):
                if known.get("status") == "quarantined":
                    skipped += 1
                    continue
                active_record = active_by_path.get(rel_path)
                if active_record:
                    active_record.metadata["source_state"] = "unchanged"
                    provisional.append(active_record)
                    skipped += 1
                    continue
            try:
                loaded = self.loader.load(path, source_state="processed")
                provisional.append(loaded.record)
                processed += 1
            except DocumentValidationError as exc:
                quarantined_records.append(
                    self.store.record_quarantine(
                        path,
                        QuarantineRecord(
                            path=rel_path,
                            reason=exc.reason,
                            details=exc.details,
                            content_hash=exc.content_hash,
                            fingerprint_hash=exc.fingerprint_hash,
                        ),
                    )
                )

        documents, duplicate_records = self._remove_duplicates(provisional)
        quarantined_records.extend(duplicate_records)
        if duplicate_records:
            for duplicate in duplicate_records:
                source_path = self.loader.source_dir / duplicate.path
                self.store.record_quarantine(source_path, duplicate)

        active_signature = _collection_signature(active_documents)
        candidate_signature = _collection_signature(documents)
        if active_signature == candidate_signature:
            return (
                UpdateResult(
                    status="no_changes",
                    active_version_id=active_version_id,
                    processed=processed,
                    skipped=skipped,
                    removed=removed,
                    quarantined=len(quarantined_records),
                    duplicates=len(duplicate_records),
                ),
                documents,
            )

        version_id = make_version_id(now, documents)
        version = VersionRecord(
            version_id=version_id,
            created_at=utc_now_iso(),
            parent_version_id=active_version_id,
            status="candidate",
            documents=[
                {
                    "path": document.path,
                    "content_hash": document.content_hash,
                    "fingerprint_hash": document.fingerprint_hash,
                }
                for document in documents
            ],
        )
        self.store.write_version(version, documents)
        report = self.quality.evaluate(version_id, documents, eval_cases, active_documents if active_documents else None, now=now,
                                       historical_documents=self.store.read_knowledge_documents(active_version_id))
        if not report.passed:
            self.store.set_version_status(version_id, "rejected", report.to_dict())
            return (
                UpdateResult(
                    status="rejected",
                    version_id=version_id,
                    active_version_id=active_version_id,
                    processed=processed,
                    skipped=skipped,
                    removed=removed,
                    quarantined=len(quarantined_records),
                    duplicates=len(duplicate_records),
                    quality_report=report.to_dict(),
                    errors=[report.rejected_reason],
                ),
                documents,
            )
        self.store.set_version_status(version_id, "approved", report.to_dict())
        return (
            UpdateResult(
                status="approved",
                version_id=version_id,
                active_version_id=active_version_id,
                processed=processed,
                skipped=skipped,
                removed=removed,
                quarantined=len(quarantined_records),
                duplicates=len(duplicate_records),
                quality_report=report.to_dict(),
            ),
            documents,
        )

    def _remove_duplicates(self, records: list[DocumentRecord]) -> tuple[list[DocumentRecord], list[QuarantineRecord]]:
        grouped: dict[str, list[DocumentRecord]] = {}
        for record in records:
            grouped.setdefault(record.fingerprint_hash, []).append(record)
        kept: list[DocumentRecord] = []
        duplicates: list[QuarantineRecord] = []
        for fingerprint_hash, group in grouped.items():
            group.sort(key=lambda record: (record.metadata.get("source_state") != "unchanged", record.path))
            winner = group[0]
            kept.append(winner)
            for duplicate in group[1:]:
                duplicates.append(
                    QuarantineRecord(
                        path=duplicate.path,
                        reason="duplicate_document",
                        details=f"duplicates {winner.path}",
                        content_hash=duplicate.content_hash,
                        fingerprint_hash=fingerprint_hash,
                    )
                )
        kept.sort(key=lambda record: record.path)
        return kept, duplicates

    def _watch_activation(
        self,
        version_id: str,
        activated_at: datetime,
        health_checker: HealthChecker | Iterable[HealthReport | Mapping[str, Any] | bool] | None,
    ) -> HealthReport | None:
        if health_checker is None:
            report = self.metrics.health_report(version_id)
            self.metrics.record_health(report)
            return report if report.status != "pass" else None

        reports = _health_reports_from_checker(health_checker, version_id, activated_at)
        deadline = activated_at + timedelta(minutes=self.config.health.post_activation_grace_minutes)
        check_count = 0
        for report in reports:
            check_count += 1
            checked_at = parse_datetime(report.checked_at) or activated_at
            self.metrics.record_health(report)
            if checked_at <= deadline and self._health_report_failed(report):
                return report
            if check_count >= self._max_grace_checks():
                break
            if self.config.health.sleep_between_checks:
                time_module.sleep(max(0, self.config.health.check_interval_seconds))
        return None

    def _max_grace_checks(self) -> int:
        seconds = self.config.health.post_activation_grace_minutes * 60
        interval = max(1, self.config.health.check_interval_seconds)
        return max(1, (seconds // interval) + 1)

    def _health_report_failed(self, report: HealthReport) -> bool:
        failures: list[str] = []
        report.status = normalize_health_status(report.status)
        if report.status != "pass":
            failures.append(report.status)
        if report.latency_ms > self.config.health.max_latency_ms:
            failures.append("latency")
        if report.failure_rate > self.config.health.max_failure_rate:
            failures.append("failure_rate")
        if report.average_confidence < self.config.health.min_average_confidence:
            failures.append("confidence")
        if report.escalation_rate > self.config.health.max_escalation_rate:
            failures.append("escalations")
        if failures:
            existing_details = f"{report.details}; " if report.details else ""
            report.status = "fail"
            report.details = existing_details + ", ".join(failures)
            return True
        report.status = "pass"
        return False

def make_version_id(now: datetime, documents: list[DocumentRecord]) -> str:
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.path):
        digest.update(document.path.encode("utf-8"))
        digest.update(document.content_hash.encode("utf-8"))
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{digest.hexdigest()[:12]}"


def _collection_signature(documents: list[DocumentRecord]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((document.path, document.content_hash) for document in documents))


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _health_reports_from_checker(
    checker: HealthChecker | Iterable[HealthReport | Mapping[str, Any] | bool],
    version_id: str,
    activated_at: datetime,
) -> Iterable[HealthReport]:
    if callable(checker):
        max_reports = 6
        for index in range(max_reports):
            checked_at = activated_at + timedelta(seconds=index * 60)
            try:
                signature = inspect.signature(checker)
                parameter_count = len(signature.parameters)
            except (TypeError, ValueError):
                parameter_count = 0
            if parameter_count >= 2:
                raw = checker(version_id, checked_at)
            elif parameter_count == 1:
                raw = checker(version_id)
            else:
                raw = checker()
            report = HealthReport.from_any(raw, version_id=version_id)
            report.version_id = report.version_id or version_id
            report.checked_at = checked_at.isoformat()
            yield report
        return
    for raw in checker:
        yield HealthReport.from_any(raw, version_id=version_id)

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class EvaluationCase:
    question: str
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    source_hint: str = ""
    product: str | None = None
    region: str | None = None
    as_of: str | None = None
    role: str = "service"
    expected_status: str = "answered"

    @classmethod
    def from_any(cls, value: "EvaluationCase | Mapping[str, Any]") -> "EvaluationCase":
        if isinstance(value, EvaluationCase):
            return value
        required_terms = (
            value.get("required_terms")
            or value.get("expected_terms")
            or value.get("must_contain")
            or value.get("answer_terms")
            or ()
        )
        forbidden_terms = value.get("forbidden_terms") or value.get("must_not_contain") or value.get("blocked_terms") or ()
        source_hint = value.get("source_hint") or value.get("expected_source") or value.get("source") or ""
        return cls(
            question=str(value.get("question", "")),
            required_terms=_terms(required_terms),
            forbidden_terms=_terms(forbidden_terms),
            source_hint=str(source_hint),
            product=value.get("product"), region=value.get("region"), as_of=value.get("as_of"),
            role=str(value.get("role", "service")), expected_status=str(value.get("expected_status", "answered")),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["required_terms"] = list(self.required_terms)
        data["forbidden_terms"] = list(self.forbidden_terms)
        return data


def _terms(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


@dataclass
class DocumentRecord:
    path: str
    title: str
    content_hash: str
    fingerprint_hash: str
    size: int
    mtime_ns: int
    masked_content: str
    chunks: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocumentRecord":
        return cls(
            path=str(value["path"]),
            title=str(value.get("title") or value["path"]),
            content_hash=str(value["content_hash"]),
            fingerprint_hash=str(value["fingerprint_hash"]),
            size=int(value.get("size", 0)),
            mtime_ns=int(value.get("mtime_ns", 0)),
            masked_content=str(value.get("masked_content", "")),
            chunks=list(value.get("chunks", [])),
            metadata=dict(value.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuarantineRecord:
    path: str
    reason: str
    details: str = ""
    content_hash: str = ""
    fingerprint_hash: str = ""
    quarantined_at: str = field(default_factory=utc_now_iso)
    quarantine_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VersionRecord:
    version_id: str
    created_at: str
    parent_version_id: str | None
    status: str
    documents: list[dict[str, Any]]
    quality_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaseScore:
    question: str
    accuracy: float
    grounding: float
    confidence: float
    retrieved_paths: list[str]
    missing_terms: list[str] = field(default_factory=list)
    forbidden_terms_found: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualityReport:
    version_id: str
    passed: bool
    accuracy: float
    grounding: float
    baseline_accuracy: float | None = None
    baseline_grounding: float | None = None
    rejected_reason: str = ""
    case_scores: list[CaseScore] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["case_scores"] = [score.to_dict() for score in self.case_scores]
        return data


@dataclass
class HealthReport:
    status: str
    version_id: str | None
    latency_ms: float = 0.0
    failure_rate: float = 0.0
    average_confidence: float = 1.0
    escalation_rate: float = 0.0
    checked_at: str = field(default_factory=utc_now_iso)
    details: str = ""

    @classmethod
    def from_any(cls, value: "HealthReport | Mapping[str, Any] | bool", version_id: str | None = None) -> "HealthReport":
        if isinstance(value, HealthReport):
            return value
        if isinstance(value, bool):
            return cls(status="pass" if value else "fail", version_id=version_id)
        resolved_version_id = value.get("version_id") or version_id
        return cls(
            status=normalize_health_status(str(value.get("status", "pass"))),
            version_id=resolved_version_id,
            latency_ms=float(value.get("latency_ms", 0.0)),
            failure_rate=float(value.get("failure_rate", 0.0)),
            average_confidence=float(value.get("average_confidence", 1.0)),
            escalation_rate=float(value.get("escalation_rate", 0.0)),
            checked_at=str(value.get("checked_at") or utc_now_iso()),
            details=str(value.get("details", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_health_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"ok", "healthy", "success", "passed"}:
        return "pass"
    if normalized in {"error", "failed", "unhealthy"}:
        return "fail"
    return normalized or "pass"


@dataclass
class RetrievedContext:
    path: str
    chunk: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    citation_id: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueryResult:
    answer: str
    confidence: float
    contexts: list[RetrievedContext]
    active_version_id: str | None
    escalated: bool = False
    masked: bool = False
    status: str = "answered"
    citations: list[dict[str, Any]] = field(default_factory=list)
    as_of: str | None = None
    clarification_request: str = ""
    unsupported_claims: list[str] = field(default_factory=list)
    answer_mode: str = "extractive"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["contexts"] = [context.to_dict() for context in self.contexts]
        return data


@dataclass
class UpdateResult:
    status: str
    version_id: str | None = None
    active_version_id: str | None = None
    processed: int = 0
    skipped: int = 0
    removed: int = 0
    quarantined: int = 0
    duplicates: int = 0
    pending_activation: bool = False
    quality_report: dict[str, Any] = field(default_factory=dict)
    health_report: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractedEvidence:
    order_ids: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    raw_text: str = ""
    masked_text: str = ""
    quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceFileResult:
    file_id: str
    filename: str
    status: str
    evidence: ExtractedEvidence = field(default_factory=ExtractedEvidence)
    reason: str = ""
    delete_after: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = self.evidence.to_dict()
        return data


@dataclass
class EvidenceConflict:
    field: str
    message_values: list[str]
    file_values: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceAnalysisResult:
    status: str
    customer_message: str
    message_evidence: ExtractedEvidence = field(default_factory=ExtractedEvidence)
    files: list[EvidenceFileResult] = field(default_factory=list)
    conflicts: list[EvidenceConflict] = field(default_factory=list)
    missing_values: list[str] = field(default_factory=list)
    clarification_request: str = ""
    background_job_id: str = ""
    processing_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["message_evidence"] = self.message_evidence.to_dict()
        data["files"] = [file_result.to_dict() for file_result in self.files]
        data["conflicts"] = [conflict.to_dict() for conflict in self.conflicts]
        return data


@dataclass
class BackgroundJob:
    job_id: str
    status: str
    created_at: str
    customer_notification: str
    message: str
    file_ids: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

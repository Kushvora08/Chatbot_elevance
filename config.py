from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping
import json
import math


def _tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


@dataclass
class StorageConfig:
    source_dir: str = "knowledge_base"
    state_dir: str = ".kb_state"
    version_retention: int = 20


@dataclass
class DocumentConfig:
    allowed_extensions: tuple[str, ...] = (
        ".txt",
        ".md",
        ".markdown",
        ".html",
        ".htm",
        ".json",
        ".csv",
        ".tsv",
    )
    max_file_bytes: int = 5_000_000
    chunk_words: int = 180
    chunk_overlap: int = 30

    def __post_init__(self) -> None:
        self.allowed_extensions = tuple(ext.lower() for ext in _tuple(self.allowed_extensions))
        if self.chunk_overlap >= self.chunk_words:
            self.chunk_overlap = max(0, self.chunk_words // 5)


@dataclass
class SecurityConfig:
    tokens: Mapping[str, str] = field(
        default_factory=lambda: {
            "admin-token": "admin",
            "service-token": "service",
            "viewer-token": "viewer",
        }
    )
    role_actions: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "admin": (
                "analyze",
                "activate",
                "health",
                "metrics",
                "query",
                "rollback",
                "schedule",
                "update",
                "upload",
                "ticket_read",
                "ticket_write",
                "ticket_admin",
            ),
            "service": ("analyze", "health", "metrics", "query", "upload", "ticket_read", "ticket_write"),
            "viewer": ("analyze", "health", "query", "upload", "ticket_read"),
        }
    )
    mask_sensitive_data: bool = True
    quarantine_prompt_injection: bool = True
    prompt_injection_threshold: int = 2

    def __post_init__(self) -> None:
        self.tokens = dict(self.tokens)
        self.role_actions = {role: _tuple(actions) for role, actions in dict(self.role_actions).items()}


@dataclass
class QualityConfig:
    min_accuracy: float = 0.80
    min_grounding: float = 0.80
    max_accuracy_regression: float = 0.0
    max_grounding_regression: float = 0.0
    require_eval_cases: bool = False
    top_k: int = 3


@dataclass
class ScheduleConfig:
    update_time: str = "02:00"
    maintenance_start: str = "02:00"
    maintenance_end: str = "03:00"
    retry_minutes: tuple[int, ...] = (15, 30, 60)
    activate_only_in_maintenance: bool = True

    def __post_init__(self) -> None:
        self.retry_minutes = tuple(int(value) for value in _tuple(self.retry_minutes))


@dataclass
class HealthConfig:
    max_latency_ms: float = 1200.0
    max_failure_rate: float = 0.05
    min_average_confidence: float = 0.55
    max_escalation_rate: float = 0.20
    post_activation_grace_minutes: int = 5
    check_interval_seconds: int = 60
    sleep_between_checks: bool = False


@dataclass
class EvidenceConfig:
    upload_dir: str = ".kb_state/uploads"
    retention_minutes: int = 60
    processing_timeout_seconds: float = 30.0
    allowed_extensions: tuple[str, ...] = (
        ".txt",
        ".md",
        ".markdown",
        ".html",
        ".htm",
        ".json",
        ".csv",
        ".tsv",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    )
    max_file_bytes: int = 10_000_000
    min_image_quality: float = 0.45
    allow_text_image_fixtures: bool = True

    def __post_init__(self) -> None:
        self.allowed_extensions = tuple(ext.lower() for ext in _tuple(self.allowed_extensions))


@dataclass
class TicketConfig:
    timezone: str = "UTC"
    business_start: str = "09:00"
    business_end: str = "17:00"
    weekends: tuple[int, ...] = (5, 6)
    holidays: tuple[str, ...] = ()
    mandatory_fields: tuple[str, ...] = ("customer", "contact", "issue")
    required_by_category: dict[str, list[str]] = field(default_factory=lambda: {
        "shipping": ["order"], "billing": ["order"],
        "technical": ["product"], "product": ["product"], "general": [],
    })
    sla_minutes: dict[str, float] = field(default_factory=lambda: {
        "P1": 60, "P2": 240, "P3": 480, "P4": 960,
    })
    duplicate_window_hours: float = 168
    escalation_team: str = "support-leads"
    teams: list[dict[str, Any]] = field(default_factory=lambda: [
        {"id": "orders", "name": "Orders", "skills": ["shipping", "billing"], "available": True},
        {"id": "technical", "name": "Technical", "skills": ["technical", "product"], "available": True},
        {"id": "general", "name": "Customer care", "skills": ["general"], "available": True},
        {"id": "support-leads", "name": "Support leads", "skills": ["shipping", "billing", "technical", "product", "general"], "available": True},
    ])
    agents: list[dict[str, Any]] = field(default_factory=lambda: [
        {"id": "agent-orders", "name": "Orders agent", "team": "orders", "skills": ["shipping", "billing"], "available": True, "capacity": 5},
        {"id": "agent-technical", "name": "Technical agent", "team": "technical", "skills": ["technical", "product"], "available": True, "capacity": 5},
        {"id": "agent-care", "name": "Care agent", "team": "general", "skills": ["general"], "available": True, "capacity": 5},
        {"id": "agent-lead", "name": "Support lead", "team": "support-leads", "skills": ["shipping", "billing", "technical", "product", "general"], "available": True, "capacity": 10},
    ])

    def __post_init__(self) -> None:
        self.weekends = tuple(int(value) for value in _tuple(self.weekends))
        self.holidays = tuple(str(value) for value in _tuple(self.holidays))
        self.mandatory_fields = tuple(str(value) for value in _tuple(self.mandatory_fields))


@dataclass
class RagConfig:
    access_by_role: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "admin": ("public", "internal", "restricted"),
        "service": ("public", "internal"),
        "viewer": ("public",),
    })
    legacy_access_level: str = "internal"
    max_contexts: int = 5

    def __post_init__(self) -> None:
        self.access_by_role = {role: _tuple(levels) for role, levels in self.access_by_role.items()}
        if type(self.max_contexts) is not int or not 1 <= self.max_contexts <= 20:
            raise ValueError("RAG max_contexts must be between 1 and 20")
        if self.legacy_access_level not in {"public", "internal", "restricted"}:
            raise ValueError("Unknown legacy document access level")


@dataclass
class SessionConfig:
    languages: tuple[str, ...] = ("en", "es", "fr", "de", "pt", "hi", "ar", "ta", "te", "bn")
    default_language: str = "en"
    language_threshold: float = 0.60
    intent_threshold: float = 0.65
    history_messages: int = 20
    inactivity_minutes: float = 30
    restoration_hours: float = 24

    def __post_init__(self) -> None:
        self.languages = _tuple(self.languages)
        supported = {"en", "es", "fr", "de", "pt", "hi", "ar", "ta", "te", "bn"}
        if not self.languages or not set(self.languages) <= supported or len(set(self.languages)) != len(self.languages):
            raise ValueError("Session languages must be distinct supported language codes")
        if self.default_language not in self.languages:
            raise ValueError("Default language must be enabled")
        for value in (self.language_threshold, self.intent_threshold):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("Confidence thresholds must be greater than zero and at most one")
        if type(self.history_messages) is not int or not 10 <= self.history_messages <= 100:
            raise ValueError("Retain between 10 and 100 customer messages")
        for value, maximum in ((self.inactivity_minutes, 1440), (self.restoration_hours, 720)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError("Session timings must be positive finite values within the supported range")
        if self.restoration_hours * 60 < self.inactivity_minutes:
            raise ValueError("Restoration window cannot be shorter than the idle timeout")


@dataclass
class PipelineConfig:
    storage: StorageConfig = field(default_factory=StorageConfig)
    documents: DocumentConfig = field(default_factory=DocumentConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    tickets: TicketConfig = field(default_factory=TicketConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)

    @classmethod
    def from_file(cls, path: str | Path) -> "PipelineConfig":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "PipelineConfig":
        if not data:
            return cls()
        return _dataclass_from_mapping(cls, data)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_mapping(self)

    def write_sample(self, path: str | Path) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")


def _dataclass_from_mapping(cls: type, values: Mapping[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    for item in fields(cls):
        value = values.get(item.name)
        if value is None:
            continue
        field_type = item.type
        if hasattr(field_type, "__name__") and is_dataclass(field_type):
            kwargs[item.name] = _dataclass_from_mapping(field_type, value)
        else:
            default_value = getattr(cls(), item.name) if cls is PipelineConfig else None
            if is_dataclass(default_value) and isinstance(value, Mapping):
                kwargs[item.name] = _dataclass_from_mapping(type(default_value), value)
            else:
                kwargs[item.name] = value
    return cls(**kwargs)


def _dataclass_to_mapping(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _dataclass_to_mapping(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_dataclass_to_mapping(item) for item in value]
    if isinstance(value, dict):
        return {key: _dataclass_to_mapping(item) for key, item in value.items()}
    return value

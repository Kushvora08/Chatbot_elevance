from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
import hashlib
import json
import os
import re
import zlib

from .config import PipelineConfig
from .models import (
    BackgroundJob,
    EvidenceAnalysisResult,
    EvidenceConflict,
    EvidenceFileResult,
    ExtractedEvidence,
    utc_now_iso,
)
from .monitoring import MetricsStore, Timer
from .security import AccessController, PromptInjectionProtector, SensitiveDataMasker


TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv", ".tsv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
PDF_EXTENSIONS = {".pdf"}

ORDER_ID_RE = re.compile(
    r"\b(?:order|ord|invoice|purchase)\s*(?:id|number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9-]{3,})\b",
    flags=re.IGNORECASE,
)
ORDER_TOKEN_RE = re.compile(r"\b(?:ORD|ORDER|INV|PO)[-\s]?[A-Z0-9]{4,}\b", flags=re.IGNORECASE)
DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
    flags=re.IGNORECASE,
)
AMOUNT_RE = re.compile(
    r"\b(?:total|amount|paid|charged|balance|subtotal)?\s*[:#-]?\s*(?:USD|INR|EUR|GBP|\$|Rs\.?|₹)\s*([0-9][0-9,]*(?:\.[0-9]{2})?)\b|"
    r"\b(?:total|amount|paid|charged|balance|subtotal)\s*[:#-]?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)\b",
    flags=re.IGNORECASE,
)
PRODUCT_RE = re.compile(
    r"\b(?:product|item|sku|model|part)\s*(?:name|id|number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9 ._/-]{2,80})",
    flags=re.IGNORECASE,
)
ERROR_CODE_RE = re.compile(
    r"\b(?:error|err|code)\s*(?:code|id|#)?\s*[:#-]?\s*([A-Z]{1,8}[-_]?\d{2,8}|[A-Z0-9]{4,12})\b|"
    r"\b((?:ERR|E|HTTP|SKU)[-_]?\d{2,8})\b",
    flags=re.IGNORECASE,
)
QUALITY_RE = re.compile(r"\b(?:quality|ocr_quality|image_quality)\s*[:=]\s*(0(?:\.\d+)?|1(?:\.0+)?)\b", flags=re.IGNORECASE)


class EvidenceError(ValueError):
    def __init__(self, reason: str, details: str = "") -> None:
        super().__init__(details or reason)
        self.reason = reason
        self.details = details or reason


@dataclass
class FileReference:
    file_id: str
    filename: str
    path: Path
    delete_after: str = ""


class EvidenceStore:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        upload_dir = Path(config.evidence.upload_dir)
        if not upload_dir.is_absolute():
            upload_dir = Path(config.storage.state_dir) / upload_dir.name
        self.upload_dir = upload_dir
        self.metadata_dir = self.upload_dir / "metadata"
        self.queue_path = self.upload_dir / "background_jobs.json"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    def save_upload(self, filename: str, data: bytes, now: datetime | None = None) -> FileReference:
        now = now or datetime.now(timezone.utc)
        safe_name = sanitize_filename(filename)
        extension = Path(safe_name).suffix.lower()
        if extension not in self.config.evidence.allowed_extensions:
            raise EvidenceError("unsupported_extension", f"{extension or 'no extension'} is not allowed")
        if not data:
            raise EvidenceError("empty_file", "uploaded file is empty")
        if len(data) > self.config.evidence.max_file_bytes:
            raise EvidenceError("file_too_large", f"{len(data)} bytes exceeds configured maximum")
        if unsafe_magic(data):
            raise EvidenceError("unsafe_file", "file signature is not allowed")
        file_id = make_file_id(safe_name, data, now)
        path = self.upload_dir / f"{file_id}_{safe_name}"
        path.write_bytes(data)
        delete_after = (now + timedelta(minutes=self.config.evidence.retention_minutes)).replace(microsecond=0).isoformat()
        reference = FileReference(file_id=file_id, filename=safe_name, path=path, delete_after=delete_after)
        self._write_metadata(reference)
        return reference

    def resolve(self, file_ref: str, now: datetime | None = None) -> FileReference:
        now = now or datetime.now(timezone.utc)
        metadata_path = self._metadata_path(file_ref)
        if metadata_path.exists():
            metadata = self._read_json(metadata_path)
            delete_after = parse_datetime(metadata.get("delete_after"))
            reference = FileReference(
                file_id=str(metadata["file_id"]),
                filename=str(metadata["filename"]),
                path=Path(metadata["path"]),
                delete_after=str(metadata.get("delete_after", "")),
            )
            if delete_after and now >= delete_after:
                self.delete_upload(reference)
                raise EvidenceError("expired_file", "uploaded file passed its retention period")
            if not reference.path.exists():
                raise EvidenceError("missing_file", "uploaded file is missing")
            return reference

        path = Path(file_ref)
        if path.exists():
            return FileReference(file_id=path.stem, filename=path.name, path=path)
        raise EvidenceError("missing_file", f"file '{file_ref}' was not found")

    def list_uploads(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        uploads: list[dict[str, Any]] = []
        for metadata_path in sorted(self.metadata_dir.glob("*.json")):
            metadata = self._read_json(metadata_path)
            delete_after = parse_datetime(metadata.get("delete_after"))
            expired = bool(delete_after and now >= delete_after)
            metadata["expired"] = expired
            uploads.append(metadata)
        return uploads

    def cleanup_expired(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        deleted: list[str] = []
        for metadata in self.list_uploads(now):
            delete_after = parse_datetime(metadata.get("delete_after"))
            if delete_after and now >= delete_after:
                reference = FileReference(
                    file_id=str(metadata["file_id"]),
                    filename=str(metadata["filename"]),
                    path=Path(metadata["path"]),
                    delete_after=str(metadata.get("delete_after", "")),
                )
                self.delete_upload(reference)
                deleted.append(reference.file_id)
        return deleted

    def delete_upload(self, reference: FileReference) -> None:
        if reference.path.exists():
            reference.path.unlink()
        sidecar = reference.path.with_name(reference.path.name + ".ocr.txt")
        if sidecar.exists():
            sidecar.unlink()
        metadata_path = self._metadata_path(reference.file_id)
        if metadata_path.exists():
            metadata_path.unlink()

    def enqueue(
        self,
        message: str,
        file_ids: list[str],
        reason: str,
        notification: str,
        now: datetime | None = None,
    ) -> BackgroundJob:
        now = now or datetime.now(timezone.utc)
        digest = hashlib.sha256((message + "|".join(file_ids) + now.isoformat()).encode("utf-8")).hexdigest()[:12]
        job = BackgroundJob(
            job_id=f"job-{now.strftime('%Y%m%dT%H%M%S')}-{digest}",
            status="queued",
            created_at=now.replace(microsecond=0).isoformat(),
            customer_notification=notification,
            message=message,
            file_ids=file_ids,
            reason=reason,
        )
        jobs = self.list_jobs()
        jobs.append(job.to_dict())
        self._write_json(self.queue_path, jobs)
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        return self._read_json(self.queue_path)

    def _write_metadata(self, reference: FileReference) -> None:
        self._write_json(
            self._metadata_path(reference.file_id),
            {
                "file_id": reference.file_id,
                "filename": reference.filename,
                "path": str(reference.path),
                "uploaded_at": utc_now_iso(),
                "delete_after": reference.delete_after,
            },
        )

    def _metadata_path(self, file_id: str) -> Path:
        return self.metadata_dir / f"{sanitize_identifier(file_id)}.json"

    def _read_json(self, path: Path) -> Any:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)


class EvidenceAnalyzer:
    def __init__(
        self,
        config: PipelineConfig,
        access: AccessController,
        metrics: MetricsStore,
    ) -> None:
        self.config = config
        self.access = access
        self.metrics = metrics
        self.store = EvidenceStore(config)
        self.masker = SensitiveDataMasker()
        self.injection = PromptInjectionProtector()

    def analyze(
        self,
        message: str,
        file_refs: Iterable[str],
        token: str | None = "service-token",
        now: datetime | None = None,
        simulate_processing_seconds: float = 0.0,
    ) -> EvidenceAnalysisResult:
        self.access.authorize(token, "analyze")
        now = now or datetime.now(timezone.utc)
        timer = Timer()
        references = list(file_refs)
        masked_message, _ = self.masker.mask(message)

        if simulate_processing_seconds > self.config.evidence.processing_timeout_seconds:
            result = self._queue_for_background(masked_message, references, "processing_timeout", now)
            self._record_analysis(result, timer.elapsed_ms, masked_message)
            return result

        message_evidence = extract_evidence(message, self.masker)
        if self.injection.scan(message).score >= self.config.security.prompt_injection_threshold:
            result = EvidenceAnalysisResult(
                status="rejected",
                customer_message="I cannot process this request because the message contains unsafe instructions.",
                message_evidence=message_evidence,
                clarification_request="Please resend the request without instructions that try to change system behavior.",
                processing_ms=round(timer.elapsed_ms, 4),
            )
            self._record_analysis(result, timer.elapsed_ms, masked_message)
            return result

        files: list[EvidenceFileResult] = []
        for file_ref in references:
            if timer.elapsed_ms / 1000.0 > self.config.evidence.processing_timeout_seconds:
                result = self._queue_for_background(masked_message, references, "processing_timeout", now)
                self._record_analysis(result, timer.elapsed_ms, masked_message)
                return result
            files.append(self._process_file(file_ref, now))

        status = "complete"
        clarification = ""
        rejected = [file_result for file_result in files if file_result.status == "rejected"]
        expired_or_missing = [file_result for file_result in files if file_result.status in {"expired", "missing"}]
        low_quality = [file_result for file_result in files if file_result.status == "low_quality"]
        conflicts = compare_evidence(message_evidence, [file_result.evidence for file_result in files if file_result.status == "processed"])
        missing = missing_values(message_evidence, [file_result.evidence for file_result in files if file_result.status == "processed"])

        if rejected:
            status = "rejected"
            clarification = "One or more files were rejected because they are unsafe or contain hidden instructions."
        elif expired_or_missing:
            status = "needs_clarification"
            clarification = "One or more uploaded files are missing or expired. Please upload the file again."
        elif low_quality:
            status = "needs_clarification"
            clarification = "The image quality is too low for reliable OCR. Please upload a clearer screenshot or invoice."
        elif conflicts:
            status = "needs_clarification"
            fields = ", ".join(conflict.field for conflict in conflicts)
            clarification = f"The customer message and uploaded evidence disagree on {fields}. Please confirm the correct value or upload another file."
        elif missing and not any(has_any_evidence(item) for item in [message_evidence, *[file_result.evidence for file_result in files]]):
            status = "needs_clarification"
            clarification = "I could not find order, date, amount, product, or error-code details. Please provide the missing values or another file."

        result = EvidenceAnalysisResult(
            status=status,
            customer_message=customer_message_for(status, clarification),
            message_evidence=message_evidence,
            files=files,
            conflicts=conflicts,
            missing_values=missing,
            clarification_request=clarification,
            processing_ms=round(timer.elapsed_ms, 4),
        )
        self._record_analysis(result, timer.elapsed_ms, masked_message)
        return result

    def save_upload(self, filename: str, data: bytes, token: str | None = "service-token", now: datetime | None = None) -> FileReference:
        self.access.authorize(token, "upload")
        reference = self.store.save_upload(filename, data, now)
        self.metrics.record("evidence_upload", file_id=reference.file_id, filename=reference.filename, bytes=len(data))
        return reference

    def cleanup_expired(self, token: str | None = "service-token", now: datetime | None = None) -> list[str]:
        self.access.authorize(token, "upload")
        deleted = self.store.cleanup_expired(now)
        self.metrics.record("evidence_cleanup", deleted_file_ids=deleted)
        return deleted

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        self.store.cleanup_expired(now)
        return {
            "uploads": self.store.list_uploads(now),
            "jobs": self.store.list_jobs(),
            "retention_minutes": self.config.evidence.retention_minutes,
            "processing_timeout_seconds": self.config.evidence.processing_timeout_seconds,
        }

    def _process_file(self, file_ref: str, now: datetime) -> EvidenceFileResult:
        try:
            reference = self.store.resolve(file_ref, now)
            validate_file(reference.path, self.config)
            ocr_text, quality_score, warnings = extract_text(reference.path, self.config)
            evidence = extract_evidence(ocr_text, self.masker)
            evidence.quality_score = quality_score
            evidence.warnings.extend(warnings)
            if self.injection.scan(ocr_text).score >= self.config.security.prompt_injection_threshold:
                return EvidenceFileResult(
                    file_id=reference.file_id,
                    filename=reference.filename,
                    status="rejected",
                    evidence=evidence,
                    reason="prompt_injection_detected",
                    delete_after=reference.delete_after,
                )
            if quality_score < self.config.evidence.min_image_quality:
                return EvidenceFileResult(
                    file_id=reference.file_id,
                    filename=reference.filename,
                    status="low_quality",
                    evidence=evidence,
                    reason="low_image_quality",
                    delete_after=reference.delete_after,
                )
            if not ocr_text.strip():
                return EvidenceFileResult(
                    file_id=reference.file_id,
                    filename=reference.filename,
                    status="low_quality",
                    evidence=evidence,
                    reason="no_readable_text",
                    delete_after=reference.delete_after,
                )
            return EvidenceFileResult(
                file_id=reference.file_id,
                filename=reference.filename,
                status="processed",
                evidence=evidence,
                delete_after=reference.delete_after,
            )
        except EvidenceError as exc:
            status = "rejected" if exc.reason in {"unsupported_extension", "unsafe_file", "file_too_large", "empty_file"} else exc.reason.replace("_file", "")
            return EvidenceFileResult(file_id=file_ref, filename=Path(file_ref).name, status=status, reason=exc.reason)

    def _queue_for_background(self, masked_message: str, file_refs: list[str], reason: str, now: datetime) -> EvidenceAnalysisResult:
        notification = "This is taking longer than 30 seconds, so I moved it to a background queue and will notify you when it finishes."
        job = self.store.enqueue(masked_message, file_refs, reason, notification, now)
        return EvidenceAnalysisResult(
            status="queued",
            customer_message=notification,
            background_job_id=job.job_id,
            processing_ms=round(self.config.evidence.processing_timeout_seconds * 1000.0, 4),
        )

    def _record_analysis(self, result: EvidenceAnalysisResult, latency_ms: float, masked_message: str) -> None:
        self.metrics.record(
            "evidence_analysis",
            status=result.status,
            latency_ms=round(latency_ms, 4),
            masked_message=masked_message,
            file_count=len(result.files),
            conflicts=[conflict.to_dict() for conflict in result.conflicts],
            missing_values=result.missing_values,
            background_job_id=result.background_job_id,
        )


def validate_file(path: Path, config: PipelineConfig) -> None:
    extension = path.suffix.lower()
    if extension not in config.evidence.allowed_extensions:
        raise EvidenceError("unsupported_extension", f"{extension or 'no extension'} is not allowed")
    size = path.stat().st_size
    if size <= 0:
        raise EvidenceError("empty_file", "file is empty")
    if size > config.evidence.max_file_bytes:
        raise EvidenceError("file_too_large", f"{size} bytes exceeds configured maximum")
    raw = path.read_bytes()[:16]
    if unsafe_magic(raw):
        raise EvidenceError("unsafe_file", "file signature is not allowed")


def extract_text(path: Path, config: PipelineConfig) -> tuple[str, float, list[str]]:
    extension = path.suffix.lower()
    raw = path.read_bytes()
    if extension in TEXT_EXTENSIONS:
        return decode_text(raw), 1.0, []
    if extension in PDF_EXTENSIONS:
        if looks_like_text(raw):
            return decode_text(raw), 0.95, ["text_pdf_fixture"]
        text = extract_pdf_text(raw)
        return text, 0.9 if text.strip() else 0.2, [] if text.strip() else ["no_pdf_text"]
    if extension in IMAGE_EXTENSIONS:
        return extract_image_text(path, raw, config)
    return "", 0.0, ["unsupported_type"]


def extract_image_text(path: Path, raw: bytes, config: PipelineConfig) -> tuple[str, float, list[str]]:
    sidecar = path.with_name(path.name + ".ocr.txt")
    warnings: list[str] = []
    if sidecar.exists():
        text = sidecar.read_text(encoding="utf-8")
    elif config.evidence.allow_text_image_fixtures and looks_like_text(raw):
        text = decode_text(raw)
        warnings.append("text_image_fixture")
    else:
        text = ""
    quality = image_quality_score(raw, text)
    if not recognized_image(raw) and not text:
        quality = 0.0
        warnings.append("unrecognized_image")
    if recognized_image(raw) and not text:
        warnings.append("ocr_text_unavailable")
    return text, quality, warnings


def extract_pdf_text(raw: bytes) -> str:
    text_parts: list[str] = []
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
        from io import BytesIO

        reader = PdfReader(BytesIO(raw))
        text_parts.extend(page.extract_text() or "" for page in reader.pages)
    except Exception:
        pass
    decoded = raw.decode("latin-1", errors="ignore")
    text_parts.extend(match.group(1) for match in re.finditer(r"\(([^()]{2,500})\)", decoded))
    text_parts.extend(_extract_text_from_pdf_streams(decoded))
    return clean_text(" ".join(text_parts))


def _extract_text_from_pdf_streams(decoded_pdf: str) -> list[str]:
    texts: list[str] = []
    for match in re.finditer(r"stream\r?\n(.*?)\r?\nendstream", decoded_pdf, flags=re.DOTALL):
        stream = match.group(1).encode("latin-1", errors="ignore")
        try:
            inflated = zlib.decompress(stream)
        except zlib.error:
            continue
        inflated_text = inflated.decode("latin-1", errors="ignore")
        texts.extend(item.group(1) for item in re.finditer(r"\(([^()]{2,500})\)", inflated_text))
    return texts


def extract_evidence(text: str, masker: SensitiveDataMasker) -> ExtractedEvidence:
    masked_text, _ = masker.mask(clean_text(text))
    return ExtractedEvidence(
        order_ids=unique_values([clean_value(value) for value in ORDER_ID_RE.findall(text)] + ORDER_TOKEN_RE.findall(text)),
        dates=unique_values(DATE_RE.findall(text)),
        amounts=unique_values([clean_value(first or second) for first, second in AMOUNT_RE.findall(text)]),
        products=unique_values(clean_product(value) for value in PRODUCT_RE.findall(text)),
        error_codes=unique_values(clean_value(first or second) for first, second in ERROR_CODE_RE.findall(text)),
        raw_text=masked_text,
        masked_text=masked_text,
    )


def compare_evidence(message: ExtractedEvidence, files: list[ExtractedEvidence]) -> list[EvidenceConflict]:
    file_values = merge_evidence(files)
    conflicts: list[EvidenceConflict] = []
    checks = {
        "order_ids": normalize_identifier_values,
        "dates": normalize_date_values,
        "amounts": normalize_amount_values,
        "products": normalize_product_values,
        "error_codes": normalize_identifier_values,
    }
    for field, normalizer in checks.items():
        message_raw = getattr(message, field)
        file_raw = getattr(file_values, field)
        if not message_raw or not file_raw:
            continue
        message_normalized = set(normalizer(message_raw))
        file_normalized = set(normalizer(file_raw))
        if message_normalized and file_normalized and not message_normalized.intersection(file_normalized):
            conflicts.append(EvidenceConflict(field=field, message_values=message_raw, file_values=file_raw))
    return conflicts


def missing_values(message: ExtractedEvidence, files: list[ExtractedEvidence]) -> list[str]:
    file_values = merge_evidence(files)
    missing: list[str] = []
    for field in ("order_ids", "dates", "amounts", "products", "error_codes"):
        if not getattr(message, field) and not getattr(file_values, field):
            missing.append(field)
    return missing


def merge_evidence(items: list[ExtractedEvidence]) -> ExtractedEvidence:
    merged = ExtractedEvidence()
    for item in items:
        merged.order_ids.extend(item.order_ids)
        merged.dates.extend(item.dates)
        merged.amounts.extend(item.amounts)
        merged.products.extend(item.products)
        merged.error_codes.extend(item.error_codes)
    merged.order_ids = unique_values(merged.order_ids)
    merged.dates = unique_values(merged.dates)
    merged.amounts = unique_values(merged.amounts)
    merged.products = unique_values(merged.products)
    merged.error_codes = unique_values(merged.error_codes)
    return merged


def has_any_evidence(evidence: ExtractedEvidence) -> bool:
    return any((evidence.order_ids, evidence.dates, evidence.amounts, evidence.products, evidence.error_codes))


def customer_message_for(status: str, clarification: str) -> str:
    if status == "complete":
        return "I found extractable evidence and did not detect conflicts with the customer message."
    if status == "rejected":
        return "I cannot process one or more files because they are unsafe."
    return clarification or "I need one more detail before I can continue."


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\x00", " ")).strip()


def clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,:;#")


def clean_product(value: str) -> str:
    value = clean_value(value)
    value = re.split(r"\b(?:order|date|amount|total|error)\b", value, flags=re.IGNORECASE)[0]
    return clean_value(value)


def unique_values(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = clean_value(str(value))
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def normalize_identifier_values(values: list[str]) -> list[str]:
    return [re.sub(r"[^a-z0-9]", "", value.lower()) for value in values]


def normalize_plain_values(values: list[str]) -> list[str]:
    return [clean_value(value).lower() for value in values]


def normalize_date_values(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        cleaned = clean_value(value)
        parsed = parse_date(cleaned)
        normalized.append(parsed or cleaned.lower())
    return normalized


def normalize_amount_values(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        compact = re.sub(r"[^0-9.]", "", value)
        try:
            normalized.append(f"{float(compact):.2f}")
        except ValueError:
            normalized.append(value.lower())
    return normalized


def normalize_product_values(values: list[str]) -> list[str]:
    return [re.sub(r"[^a-z0-9]+", " ", value.lower()).strip() for value in values]


def parse_date(value: str) -> str | None:
    formats = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y", "%b %d %Y", "%B %d %Y")
    candidate = value.replace(",", "")
    for date_format in formats:
        try:
            return datetime.strptime(candidate, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def image_quality_score(raw: bytes, text: str) -> float:
    marker = QUALITY_RE.search(text)
    if marker:
        return float(marker.group(1))
    lowered = text.lower()
    if "[blurred]" in lowered or "blurred image" in lowered or "too blurry" in lowered:
        return 0.2
    if not raw:
        return 0.0
    dimensions = image_dimensions(raw)
    if dimensions:
        width, height = dimensions
        if width < 250 or height < 250:
            return 0.3
    if recognized_image(raw) and not text.strip():
        return 0.35
    if looks_like_text(raw):
        return 0.9
    return 0.75


def image_dimensions(raw: bytes) -> tuple[int, int] | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if raw.startswith((b"GIF87a", b"GIF89a")) and len(raw) >= 10:
        return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
    if raw.startswith(b"BM") and len(raw) >= 26:
        return int.from_bytes(raw[18:22], "little"), int.from_bytes(raw[22:26], "little")
    return None


def recognized_image(raw: bytes) -> bool:
    return raw.startswith(
        (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"RIFF",
            b"BM",
            b"II*\x00",
            b"MM\x00*",
        )
    )


def unsafe_magic(raw: bytes) -> bool:
    return raw.startswith((b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe")) or b"<script" in raw[:256].lower()


def looks_like_text(raw: bytes) -> bool:
    sample = raw[:2048]
    if not sample:
        return False
    try:
        decoded = sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for char in decoded if char.isprintable() or char.isspace())
    return printable / max(1, len(decoded)) > 0.95


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def sanitize_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    if not name:
        raise EvidenceError("missing_filename", "filename is required")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:140]


def sanitize_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def make_file_id(filename: str, data: bytes, now: datetime) -> str:
    digest = hashlib.sha256(filename.encode("utf-8") + data + now.isoformat().encode("utf-8")).hexdigest()[:16]
    return f"upload-{now.strftime('%Y%m%dT%H%M%S')}-{digest}"


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

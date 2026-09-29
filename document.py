from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import hashlib
import html
import json
import re

from .config import PipelineConfig
from .models import DocumentRecord
from .knowledge_metadata import normalize_metadata, unpack_document
from .security import PromptInjectionProtector, SensitiveDataMasker


class DocumentValidationError(ValueError):
    def __init__(self, reason: str, details: str = "", content_hash: str = "", fingerprint_hash: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details
        self.content_hash = content_hash
        self.fingerprint_hash = fingerprint_hash


@dataclass
class LoadedDocument:
    record: DocumentRecord
    masked: bool


class DocumentLoader:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.source_dir = Path(config.storage.source_dir)
        self.state_dir = Path(config.storage.state_dir)
        self.masker = SensitiveDataMasker()
        self.injection = PromptInjectionProtector()

    def discover(self) -> list[Path]:
        if not self.source_dir.exists():
            return []
        state_dir = self.state_dir.resolve()
        files: list[Path] = []
        for path in self.source_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved == state_dir or state_dir in resolved.parents:
                continue
            files.append(path)
        return sorted(files, key=lambda item: self.rel_path(item))

    def rel_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.source_dir.resolve()).as_posix()
        except ValueError:
            return path.name

    def signature(self, path: Path) -> dict[str, int]:
        stat = path.stat()
        return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}

    def unchanged(self, manifest_entry: dict | None, path: Path) -> bool:
        if not manifest_entry:
            return False
        try:
            sig = self.signature(path)
        except OSError:
            return False
        return int(manifest_entry.get("size", -1)) == sig["size"] and int(manifest_entry.get("mtime_ns", -1)) == sig["mtime_ns"]

    def load(self, path: Path, source_state: str = "processed") -> LoadedDocument:
        rel_path = self.rel_path(path)
        extension = path.suffix.lower()
        if extension not in self.config.documents.allowed_extensions:
            raise DocumentValidationError("unsupported_extension", f"{extension or 'no extension'} is not allowed")
        size = path.stat().st_size
        if size <= 0:
            raise DocumentValidationError("empty_file", "file is empty")
        if size > self.config.documents.max_file_bytes:
            raise DocumentValidationError("file_too_large", f"{size} bytes exceeds configured maximum")
        raw_bytes = path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        text = self._decode(raw_bytes)
        raw_text = text
        try:
            text, raw_metadata = unpack_document(text)
            metadata = normalize_metadata(raw_metadata, extract_title(text, rel_path), self.config.rag.legacy_access_level)
        except (ValueError, TypeError) as exc:
            raise DocumentValidationError("invalid_metadata", str(exc), content_hash) from exc
        normalized = normalize_for_fingerprint(text)
        semantic_metadata = {key: value for key, value in metadata.items() if key not in {"title", "metadata_explicit"}}
        fingerprint_hash = hashlib.sha256((normalized + json.dumps(semantic_metadata, sort_keys=True)).encode("utf-8")).hexdigest()
        if not normalized:
            raise DocumentValidationError("empty_content", "file has no readable text", content_hash, fingerprint_hash)
        finding = self.injection.scan(raw_text + "\n" + text + "\n" + json.dumps(metadata, ensure_ascii=False))
        if (
            self.config.security.quarantine_prompt_injection
            and finding.score >= self.config.security.prompt_injection_threshold
        ):
            raise DocumentValidationError(
                "prompt_injection_detected",
                f"matched {len(finding.patterns)} suspicious patterns",
                content_hash,
                fingerprint_hash,
            )
        masked_text, masked = self.masker.mask(text) if self.config.security.mask_sensitive_data else (text, False)
        chunks = chunk_text(masked_text, self.config.documents.chunk_words, self.config.documents.chunk_overlap)
        if not chunks:
            raise DocumentValidationError("empty_content", "file produced no chunks", content_hash, fingerprint_hash)
        sig = self.signature(path)
        record = DocumentRecord(
            path=rel_path,
            title=self.masker.mask(metadata["title"])[0],
            content_hash=content_hash,
            fingerprint_hash=fingerprint_hash,
            size=sig["size"],
            mtime_ns=sig["mtime_ns"],
            masked_content=masked_text,
            chunks=chunks,
            metadata={**metadata, "source_state": source_state, "masked_sensitive_data": masked},
        )
        return LoadedDocument(record=record, masked=masked)

    def _decode(self, raw_bytes: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8"):
            try:
                text = raw_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                text = ""
        else:
            raise DocumentValidationError("invalid_encoding", "file is not valid UTF-8")
        if "\x00" in text:
            raise DocumentValidationError("binary_content", "file contains NUL bytes")
        return normalize_line_endings(text)


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_for_fingerprint(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        return line[:120] or fallback
    return fallback


def chunk_text(text: str, chunk_words: int, overlap: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    if len(words) <= chunk_words:
        return [" ".join(words)]
    chunks: list[str] = []
    start = 0
    step = max(1, chunk_words - overlap)
    while start < len(words):
        chunk = words[start : start + chunk_words]
        chunks.append(" ".join(chunk))
        if start + chunk_words >= len(words):
            break
        start += step
    return chunks


def document_map(records: Iterable[DocumentRecord]) -> dict[str, DocumentRecord]:
    return {record.path: record for record in records}

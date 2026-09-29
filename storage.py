from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
import json
import os
import re
import shutil

from .config import PipelineConfig
from .models import DocumentRecord, QuarantineRecord, VersionRecord, utc_now_iso
from .knowledge_metadata import with_policy_history


class JsonStore:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.source_dir = Path(config.storage.source_dir)
        self.state_dir = Path(config.storage.state_dir)
        self.versions_dir = self.state_dir / "versions"
        self.quarantine_dir = self.state_dir / "quarantine"
        self.manifest_path = self.state_dir / "manifest.json"
        self.scheduler_path = self.state_dir / "scheduler.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            self.write_manifest(self.default_manifest())

    def default_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "active_version_id": None,
            "previous_active_version_ids": [],
            "documents": {},
            "pending_versions": [],
            "quarantine": [],
        }

    def read_manifest(self) -> dict[str, Any]:
        return self._read_json(self.manifest_path, self.default_manifest())

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        self._write_json(self.manifest_path, manifest)

    def read_scheduler_state(self) -> dict[str, Any]:
        return self._read_json(
            self.scheduler_path,
            {
                "last_successful_update_at": None,
                "last_attempt_at": None,
                "retry_index": 0,
                "next_retry_at": None,
                "last_failure": None,
            },
        )

    def write_scheduler_state(self, state: dict[str, Any]) -> None:
        self._write_json(self.scheduler_path, state)

    def write_version(self, version: VersionRecord, documents: list[DocumentRecord]) -> None:
        version_dir = self.version_dir(version.version_id)
        version_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(version_dir / "manifest.json", version.to_dict())
        self._write_json(version_dir / "documents.json", [document.to_dict() for document in documents])

    def version_dir(self, version_id: str) -> Path:
        return self.versions_dir / version_id

    def read_version(self, version_id: str) -> VersionRecord:
        data = self._read_json(self.version_dir(version_id) / "manifest.json", {})
        return VersionRecord(
            version_id=str(data["version_id"]),
            created_at=str(data["created_at"]),
            parent_version_id=data.get("parent_version_id"),
            status=str(data.get("status", "unknown")),
            documents=list(data.get("documents", [])),
            quality_report=dict(data.get("quality_report", {})),
        )

    def read_documents(self, version_id: str | None) -> list[DocumentRecord]:
        if not version_id:
            return []
        path = self.version_dir(version_id) / "documents.json"
        if not path.exists():
            return []
        return [DocumentRecord.from_dict(item) for item in self._read_json(path, [])]

    def set_version_status(self, version_id: str, status: str, quality_report: dict[str, Any] | None = None) -> None:
        version = self.read_version(version_id)
        version.status = status
        if quality_report is not None:
            version.quality_report = quality_report
        self.write_version(version, self.read_documents(version_id))

    def read_knowledge_documents(self, version_id: str | None) -> list[DocumentRecord]:
        current = self.read_documents(version_id)
        manifest = self.read_manifest()
        if version_id != manifest.get("active_version_id"):
            return current
        history = []
        for previous in manifest.get("previous_active_version_ids", []):
            history.extend(self.read_documents(previous))
        return with_policy_history(current, history)

    def active_version_id(self) -> str | None:
        return self.read_manifest().get("active_version_id")

    def add_pending_version(self, version_id: str) -> None:
        manifest = self.read_manifest()
        pending = list(manifest.get("pending_versions", []))
        if version_id not in pending:
            pending.append(version_id)
        manifest["pending_versions"] = pending
        self.write_manifest(manifest)

    def pop_pending_version(self, version_id: str) -> None:
        manifest = self.read_manifest()
        manifest["pending_versions"] = [item for item in manifest.get("pending_versions", []) if item != version_id]
        self.write_manifest(manifest)

    def activate_version(self, version_id: str, documents: list[DocumentRecord], activated_at: datetime | None = None) -> str | None:
        manifest = self.read_manifest()
        previous = manifest.get("active_version_id")
        if previous and previous != version_id:
            history = list(manifest.get("previous_active_version_ids", []))
            history.append(previous)
            manifest["previous_active_version_ids"] = history[-self.config.storage.version_retention :]
        manifest["active_version_id"] = version_id
        activated_at = activated_at or datetime.now(timezone.utc)
        manifest["activation_health"] = {"version_id": version_id, "previous_version_id": previous,
            "activated_at": activated_at.isoformat(),
            "deadline": (activated_at + timedelta(minutes=self.config.health.post_activation_grace_minutes)).isoformat()}
        manifest["pending_versions"] = [item for item in manifest.get("pending_versions", []) if item != version_id]
        documents_manifest = dict(manifest.get("documents", {}))
        active_paths = {document.path for document in documents}
        for rel_path, entry in list(documents_manifest.items()):
            if entry.get("status") == "active" and rel_path not in active_paths:
                entry["status"] = "removed"
                entry["removed_at"] = utc_now_iso()
        for document in documents:
            documents_manifest[document.path] = {
                "status": "active",
                "content_hash": document.content_hash,
                "fingerprint_hash": document.fingerprint_hash,
                "size": document.size,
                "mtime_ns": document.mtime_ns,
                "version_id": version_id,
            }
        manifest["documents"] = documents_manifest
        self.write_manifest(manifest)
        self.set_version_status(version_id, "active")
        return previous

    def rollback(self, target_version_id: str | None = None) -> str | None:
        manifest = self.read_manifest()
        active = manifest.get("active_version_id")
        history = list(manifest.get("previous_active_version_ids", []))
        target = target_version_id or (history[-1] if history else None)
        if not target:
            return None
        if active:
            self.set_version_status(active, "rolled_back")
        manifest["active_version_id"] = target
        manifest.pop("activation_health", None)
        manifest["previous_active_version_ids"] = [item for item in history if item != target]
        documents = self.read_documents(target)
        documents_manifest = dict(manifest.get("documents", {}))
        active_paths = {document.path for document in documents}
        for rel_path, entry in list(documents_manifest.items()):
            if entry.get("status") == "active" and rel_path not in active_paths:
                entry["status"] = "removed"
        for document in documents:
            documents_manifest[document.path] = {
                "status": "active",
                "content_hash": document.content_hash,
                "fingerprint_hash": document.fingerprint_hash,
                "size": document.size,
                "mtime_ns": document.mtime_ns,
                "version_id": target,
            }
        manifest["documents"] = documents_manifest
        self.write_manifest(manifest)
        self.set_version_status(target, "active")
        return target

    def clear_active_version(self) -> None:
        manifest = self.read_manifest()
        active = manifest.get("active_version_id")
        if active:
            self.set_version_status(active, "rolled_back")
        manifest["active_version_id"] = None
        manifest.pop("activation_health", None)
        documents_manifest = dict(manifest.get("documents", {}))
        for entry in documents_manifest.values():
            if entry.get("status") == "active":
                entry["status"] = "removed"
        manifest["documents"] = documents_manifest
        self.write_manifest(manifest)

    def record_quarantine(self, source_path: Path, record: QuarantineRecord) -> QuarantineRecord:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_path.name)[:120] or "document"
        quarantine_name = f"{record.quarantined_at.replace(':', '').replace('-', '')}_{safe_name}"
        destination = self.quarantine_dir / quarantine_name
        try:
            if source_path.exists() and source_path.is_file():
                shutil.copy2(source_path, destination)
                record.quarantine_path = str(destination)
        except OSError as exc:
            record.details = f"{record.details}; copy failed: {exc}".strip("; ")
        manifest = self.read_manifest()
        manifest.setdefault("quarantine", []).append(record.to_dict())
        rel_path = record.path
        try:
            stat = source_path.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
        except OSError:
            size = 0
            mtime_ns = 0
        manifest.setdefault("documents", {})[rel_path] = {
            "status": "quarantined",
            "reason": record.reason,
            "content_hash": record.content_hash,
            "fingerprint_hash": record.fingerprint_hash,
            "size": size,
            "mtime_ns": mtime_ns,
        }
        self.write_manifest(manifest)
        return record

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)

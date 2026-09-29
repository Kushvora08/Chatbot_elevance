from __future__ import annotations

from datetime import date
from typing import Any, Mapping
import json
import re
from dataclasses import replace


ACCESS_LEVELS = {"public", "internal", "restricted"}
DOCUMENT_TYPES = {"product", "faq", "policy", "troubleshooting", "guide"}
REQUIRED = {"document_version", "product", "region", "access_level", "effective_date", "expiry_date"}
METADATA_FIELDS = REQUIRED | {"document_type", "policy_id", "title", "topic"}


def iso_date(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must use YYYY-MM-DD")
    return date.fromisoformat(value)


def version_key(value: Any) -> tuple[int, ...]:
    text = str(value)
    if not re.fullmatch(r"v?\d+(?:\.\d+){0,3}", text, re.I) or len(text) > 40:
        raise ValueError("Document version must be numeric, such as 1, 2.1 or 2.10")
    parts = [int(part) for part in text.lower().lstrip("v").split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def normalize_metadata(raw: Mapping[str, Any] | None, title: str, legacy_access: str = "internal") -> dict[str, Any]:
    if raw is None:
        return {"document_version": "1", "product": "*", "region": "*", "access_level": legacy_access,
                "effective_date": None, "expiry_date": None, "document_type": "guide",
                "title": title, "policy_id": "", "topic": "", "metadata_explicit": False}
    if not isinstance(raw, Mapping):
        raise ValueError("Document metadata must be an object")
    raw = dict(raw)
    if "version" in raw and "document_version" not in raw:
        raw["document_version"] = raw["version"]
    if REQUIRED - raw.keys():
        raise ValueError("Missing document metadata: " + ", ".join(sorted(REQUIRED - raw.keys())))
    version_key(raw["document_version"])
    metadata = {key: raw[key] for key in REQUIRED}
    metadata["document_version"] = str(raw["document_version"])
    for key in ("product", "region", "access_level"):
        if not isinstance(metadata[key], str) or not metadata[key].strip() or len(metadata[key]) > 120:
            raise ValueError(f"{key} must be a nonempty string")
        metadata[key] = metadata[key].strip()
    metadata["access_level"] = metadata["access_level"].lower()
    if metadata["access_level"] not in ACCESS_LEVELS:
        raise ValueError("Unknown document access level")
    effective = iso_date(metadata["effective_date"])
    if metadata["expiry_date"] is not None:
        if iso_date(metadata["expiry_date"]) <= effective:
            raise ValueError("Expiry must be later than the effective date")
    kind = str(raw.get("document_type", "policy")).lower()
    if kind not in DOCUMENT_TYPES:
        raise ValueError("Unknown document type")
    metadata.update(document_type=kind, title=str(raw.get("title") or title), metadata_explicit=True)
    for key in ("policy_id", "topic"):
        metadata[key] = str(raw.get(key) or "").strip()
    if kind == "policy" and not metadata["policy_id"]:
        # A shared title can identify a revision family; publishers should supply a stable policy_id.
        metadata["policy_id"] = re.sub(r"[^a-z0-9]+", "-", metadata["title"].casefold()).strip("-")
    if kind == "policy" and not metadata["policy_id"]:
        raise ValueError("A policy needs a policy_id or title")
    return metadata


def unpack_document(text: str) -> tuple[str, dict[str, Any] | None]:
    stripped = text.strip()
    if stripped.startswith("---\n"):
        raise ValueError("Use a ---json metadata header or a JSON document envelope")
    if stripped.startswith("---json\n"):
        header, separator, content = stripped[len("---json\n"):].partition("\n---\n")
        if not separator:
            raise ValueError("JSON metadata header must end with --- on its own line")
        return content, json.loads(header)
    if stripped.startswith("{"):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            if '"metadata"' in stripped:
                raise ValueError("Invalid JSON document envelope") from None
            return text, None
        if isinstance(value, dict) and ("metadata" in value or "content" in value):
            if not isinstance(value.get("content"), str) or not isinstance(value.get("metadata"), dict):
                raise ValueError("Document envelopes require content text and a metadata object")
            return value["content"], value["metadata"]
        if isinstance(value, dict) and REQUIRED & value.keys():
            raise ValueError("Put document metadata inside a metadata object with a content field")
    return text, None


def record_metadata(document, legacy_access="internal"):
    raw = document.metadata
    explicit = bool(REQUIRED & raw.keys()) and raw.get("metadata_explicit") is not False
    return normalize_metadata(raw if explicit else None, document.title, legacy_access)


def with_policy_history(current, history):
    by_path = {document.path: document for document in current}
    output = list(current)
    seen = {(document.path, document.content_hash) for document in output}
    for document in history:
        live = by_path.get(document.path)
        if live is None or (document.path, document.content_hash) in seen:
            continue
        try:
            old, new = record_metadata(document), record_metadata(live)
        except (ValueError, TypeError):
            continue
        if old["document_type"] != "policy" or new["document_type"] != "policy":
            continue
        if any(old[key] != new[key] for key in ("policy_id", "product", "region")):
            continue
        metadata = {**document.metadata, "_current_access_level": new["access_level"]}
        output.append(replace(document, metadata=metadata))
        seen.add((document.path, document.content_hash))
    return output

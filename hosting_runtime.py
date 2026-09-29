"""Single-instance hosted runtime with persistent state and real-time maintenance."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, RLock, Thread
import time
from urllib.parse import urlsplit

from .config import PipelineConfig
from .hosting_auth import AccountStore, HostedAccessController
from .models import EvaluationCase
from .orchestrator import KnowledgeBasePipeline, parse_hhmm
from .scheduler import PipelineScheduler


def settings_from_environment():
    local = os.environ.get("APP_ENV") == "development"
    secret = os.environ.get("APP_SECRET_KEY", "")
    if len(secret) < 32 or secret.casefold() in {"change-me" * 4, "x" * 32}:
        raise ValueError("APP_SECRET_KEY must be a strong random secret of at least 32 characters.")
    base_url = (os.environ.get("APP_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    url = urlsplit(base_url)
    if url.username or url.password or url.path or url.query or url.fragment or not url.hostname:
        raise ValueError("APP_BASE_URL must be an origin without credentials, path, query or fragment.")
    if url.scheme != "https" and not (local and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}):
        raise ValueError("HTTPS is required outside an explicit localhost development preview.")
    storage = os.environ.get("RATELIMIT_STORAGE_URI", "memory://" if local else "")
    if not local and not storage.startswith(("redis://", "rediss://")):
        raise ValueError("Set RATELIMIT_STORAGE_URI to a private Redis-compatible service.")
    if not local and (not os.environ.get("APP_DATA_DIR") or not Path(os.environ["APP_DATA_DIR"]).is_absolute()):
        raise ValueError("APP_DATA_DIR must be the absolute path of a persistent disk.")
    return {"secret": secret, "base_url": base_url, "secure": not local,
            "data_dir": Path(os.environ.get("APP_DATA_DIR", ".hosted-data")).resolve(),
            "rate_storage": storage, "proxy": os.environ.get("RENDER") == "true",
            "seed_demo": os.environ.get("KB_SEED_DEMO_DATA", "0") == "1"}


class HostedRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings["data_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.stopped = Event()
        self.thread = None
        self.last_tick = time.monotonic()
        self.worker_errors = {}
        self.next_schedule = 0
        self.config_path = self.root / "pipeline.json"
        self.cases_path = self.root / "quality-cases.json"
        config = PipelineConfig.from_file(self.config_path) if self.config_path.exists() else PipelineConfig()
        config.storage.source_dir = str(self.root / "knowledge_base")
        config.storage.state_dir = str(self.root / "state")
        config.evidence.upload_dir = str(self.root / "uploads")
        config.security.tokens = {}
        config.security.mask_sensitive_data = True
        config.security.quarantine_prompt_injection = True
        config.security.role_actions["customer"] = ("query", "analyze", "upload", "ticket_write", "ticket_read")
        config.rag.access_by_role["customer"] = ("public",)
        config.quality.require_eval_cases = True
        config.schedule.activate_only_in_maintenance = True
        config.health.sleep_between_checks = False
        config.evidence.allow_text_image_fixtures = False
        # A sandboxed OCR/antivirus provider is required before enabling binary uploads online.
        config.evidence.allowed_extensions = (".txt", ".csv", ".md")
        config.evidence.max_file_bytes = 2_000_000
        if not self.config_path.exists():
            for field, env in [("update_time", "KB_UPDATE_TIME"), ("maintenance_start", "KB_MAINTENANCE_START"), ("maintenance_end", "KB_MAINTENANCE_END")]:
                value = os.environ.get(env, getattr(config.schedule, field))
                parse_hhmm(value)
                setattr(config.schedule, field, value)
        self.pipeline = KnowledgeBasePipeline(config)
        self.access = HostedAccessController(config.security, settings["secret"])
        self.pipeline.access = self.access
        self.pipeline.evidence.access = self.access
        self.pipeline.tickets.access = self.access
        self.pipeline.conversations.access = self.access
        self.accounts = AccountStore(self.root / "accounts.sqlite3")
        self.accounts.bootstrap(os.environ.get("ADMIN_USERNAME"), os.environ.get("ADMIN_PASSWORD"))
        self.seed()
        self.persist_config()
        self.scheduler = PipelineScheduler(self.pipeline)

    def seed(self):
        marker = self.root / ".initialized"
        if marker.exists():
            return
        source = Path(self.pipeline.config.storage.source_dir)
        source.mkdir(parents=True, exist_ok=True)
        sample = json.loads(Path(__file__).with_name("data").joinpath("hosted-demo.json").read_text(encoding="utf-8"))
        if self.settings["seed_demo"] and not any(source.iterdir()):
            for document in sample["documents"]:
                (source / document["path"]).write_text(json.dumps({k: v for k, v in document.items() if k != "path"}, indent=2), encoding="utf-8")
        if not self.cases_path.exists():
            self.cases_path.write_text(json.dumps(sample["quality_cases"] if self.settings["seed_demo"] else [], indent=2), encoding="utf-8")
        marker.touch()

    def persist_config(self):
        self.pipeline.config.write_sample(self.config_path)

    def quality_cases(self):
        data = json.loads(self.cases_path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            raise ValueError("Add at least one quality case before updating the knowledge base.")
        return [EvaluationCase.from_any(case) for case in data]

    def save_cases(self, cases):
        if not isinstance(cases, list) or not 1 <= len(cases) <= 200:
            raise ValueError("Provide between 1 and 200 quality cases.")
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("question"), str) or not case["question"].strip():
                raise ValueError("Every quality test requires a nonempty question.")
            EvaluationCase.from_any(case)
        temporary = self.cases_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cases, indent=2), encoding="utf-8")
        temporary.replace(self.cases_path)
        # Approval belongs to the test suite used; new tests require a fresh evaluation.
        self.clear_pending("quality_tests_changed")

    def clear_pending(self, reason, keep=None):
        manifest = self.pipeline.store.read_manifest()
        pending = manifest.get("pending_versions", [])
        for version in pending:
            if version != keep:
                self.pipeline.store.set_version_status(version, reason)
        manifest["pending_versions"] = [version for version in pending if version == keep]
        self.pipeline.store.write_manifest(manifest)

    def finish_update(self, result):
        if result.status in {"approved_pending_maintenance", "activated"}:
            # A newer approved snapshot supersedes older pending snapshots, never vice versa.
            self.clear_pending("superseded", result.version_id if result.pending_activation else None)
        return result

    def scheduled_update(self, token, now):
        return self.finish_update(self.scheduler.run_due(token, self.quality_cases(), now))

    def check_activation(self, token, now):
        store = self.pipeline.store
        manifest = store.read_manifest()
        watch = manifest.get("activation_health")
        if not watch or watch["version_id"] != manifest.get("active_version_id"):
            return
        deadline = datetime.fromisoformat(watch["deadline"])
        if now > deadline:
            manifest.pop("activation_health", None)
            store.write_manifest(manifest)
            return
        try:
            if not store.read_documents(watch["version_id"]):
                raise ValueError("Active knowledge snapshot is unavailable.")
            report = self.pipeline.health_check(token)
            failed = self.pipeline._health_report_failed(report)
        except Exception:
            failed = True
        if failed:
            if watch["previous_version_id"]:
                self.pipeline.rollback(token, watch["previous_version_id"], "Hosted activation health check failed")
            else:
                store.clear_active_version()
            from .models import UpdateResult
            self.scheduler._record_result(store.read_scheduler_state(), UpdateResult(status="rolled_back", errors=["Activation health failed"]), now)

    def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self.lock, self.access.scope("system-scheduler", "admin") as token:
            tasks = {
                "tickets": lambda: self.pipeline.tickets.tick(),
                "conversations": lambda: self.pipeline.conversations.tick(),
                "sessions": lambda: self.pipeline.sessions.tick(),
                "retention": lambda: self.pipeline.cleanup_evidence_uploads(token, now),
                "accounts": lambda: self.accounts.cleanup(now.timestamp()),
                "activation": lambda: self.check_activation(token, now),
            }
            if time.monotonic() >= self.next_schedule:
                tasks["scheduler"] = lambda: self.scheduled_update(token, now)
                self.next_schedule = time.monotonic() + 30
            for name, action in tasks.items():
                try:
                    action()
                    self.worker_errors.pop(name, None)
                except Exception as exc:
                    if self.worker_errors.get(name) != type(exc).__name__:
                        self.pipeline.metrics.record("hosted_worker_failure", worker=name, error=type(exc).__name__)
                    self.worker_errors[name] = type(exc).__name__
            self.last_tick = time.monotonic()

    def start(self):
        def run():
            while not self.stopped.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    self.worker_errors["runtime"] = type(exc).__name__
                self.stopped.wait(1)
        self.thread = Thread(target=run, name="aster-maintenance", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=5)

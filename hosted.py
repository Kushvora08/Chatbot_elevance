"""Authenticated WSGI entry point. The legacy console remains local-development only."""

import atexit
import base64
import binascii
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from flask import Flask, abort, g, jsonify, redirect, request, send_from_directory, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from werkzeug.exceptions import HTTPException

from .errors import AccessDenied
from .hosting_runtime import HostedRuntime, settings_from_environment
from .orchestrator import parse_hhmm
from .server import save_document, status_payload, safe_document_path
from .knowledge_metadata import unpack_document


WEB = Path(__file__).with_name("web")
PUBLIC_ASSETS = {"chat.css", "chat.js", "auth.js", "login.html", "login.js", "hosted.css",
                 "vendor/lucide.min.js", "assets/chat-welcome.png"}


def create_app(settings=None, start_worker=True):
    settings = settings or settings_from_environment()
    runtime = HostedRuntime(settings)
    pipeline = runtime.pipeline
    app = Flask(__name__, static_folder=None)
    app.config.update(SECRET_KEY=settings["secret"], MAX_CONTENT_LENGTH=3_000_000,
                      SESSION_COOKIE_NAME="aster_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=settings["secure"], SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12), SESSION_REFRESH_EACH_REQUEST=False,
                      WTF_CSRF_SSL_STRICT=False, TRUSTED_HOSTS=[urlsplit(settings["base_url"]).hostname])
    # Proxy headers are normalized once by Waitress, not again inside Flask.
    app.extensions["hosted_runtime"] = runtime

    @app.before_request
    def load_identity():
        g.user = runtime.accounts.resolve_session(session.get("auth_id"))

    limiter = Limiter(key_func=lambda: (g.user or {}).get("id", get_remote_address()), app=app,
                      default_limits=["120 per minute"], storage_uri=settings["rate_storage"],
                      headers_enabled=True, swallow_errors=False, in_memory_fallback_enabled=False,
                      storage_options={"socket_connect_timeout": 2, "socket_timeout": 2} if settings["rate_storage"].startswith(("redis://", "rediss://")) else {})

    @app.before_request
    def require_identity():
        if request.method not in {"GET", "HEAD", "POST"}:
            abort(405)
        if request.method == "POST" and request.headers.get("Origin") not in {None, settings["base_url"]}:
            abort(403)
        public = request.path in {"/login", "/api/auth", "/auth/login", "/healthz"} or request.path.lstrip("/") in PUBLIC_ASSETS
        if not public and not g.user:
            return (jsonify(error="authentication_required", message="Please sign in."), 401) if request.path.startswith(("/api/", "/auth/")) else redirect("/login")
        if request.path in {"/console.html", "/manage.html", "/manage.js"} or request.path.startswith("/api/admin/"):
            if not g.user or g.user["role"] != "admin":
                abort(403)

    CSRFProtect(app)

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        if settings["secure"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(Exception)
    def safe_error(exc):
        if isinstance(exc, CSRFError):
            status, message = 400, "Your form expired. Refresh the page and try again."
        elif isinstance(exc, AccessDenied):
            status, message = 403, "Access denied."
        elif isinstance(exc, (ValueError, TypeError, binascii.Error)):
            status, message = 400, "Invalid request. Check your details and try again."
        elif isinstance(exc, HTTPException):
            status = exc.code or 500
            message = {401: "Please sign in.", 403: "Access denied.", 404: "Not found.",
                       413: "The request is too large.", 429: "Too many requests. Please wait and try again."}.get(status, "Request could not be completed.")
        else:
            status, message = 503, "The service is temporarily unavailable. Please try again."
        app.logger.warning("request_failure type=%s status=%s", type(exc).__name__, status)
        return jsonify(error="csrf_expired" if isinstance(exc, CSRFError) else "request_failed", message=message), status

    def body(allowed):
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - set(allowed):
            raise ValueError("Unexpected request fields.")
        return data

    def capability(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with runtime.lock, runtime.access.scope(g.user["id"], g.user["role"]) as token:
                return function(token, *args, **kwargs)
        return wrapped

    @app.get("/healthz")
    @limiter.exempt
    def health():
        # No versions, paths, customer counts, or secrets are exposed here.
        stale = start_worker and (not runtime.thread or not runtime.thread.is_alive() or time.monotonic() - runtime.last_tick > 90)
        with runtime.accounts.connect() as db:
            db.execute("SELECT 1").fetchone()
        unhealthy_workers = set(runtime.worker_errors) - {"scheduler"}
        if stale or unhealthy_workers or not limiter.storage.check():
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok")

    @app.get("/api/auth")
    def authentication():
        return jsonify(mode="hosted", user=g.user, csrf_token=generate_csrf(),
                       upload_extensions=list(pipeline.config.evidence.allowed_extensions), max_upload_bytes=2_000_000)

    @app.post("/auth/login")
    @limiter.limit("5 per minute; 30 per hour", key_func=get_remote_address)
    def login():
        data = body({"username", "password"})
        if not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str) or len(data["password"]) > 128:
            abort(401)
        user = runtime.accounts.authenticate(data["username"], data["password"])
        if not user:
            return jsonify(message="Username or password is incorrect."), 401
        runtime.accounts.revoke(session.get("auth_id"))
        session.clear()
        session["auth_id"] = runtime.accounts.issue_session(user["id"])
        session.permanent = True
        return jsonify(user=user, csrf_token=generate_csrf())

    @app.post("/auth/logout")
    def logout():
        runtime.accounts.revoke(session.get("auth_id"))
        session.clear()
        return jsonify(status="signed_out")

    @app.get("/login")
    def login_page():
        return redirect("/") if g.user else send_from_directory(WEB, "login.html")

    @app.get("/")
    @app.get("/index.html")
    def chat_page():
        return send_from_directory(WEB, "index.html")

    @app.get("/console.html")
    def admin_page():
        return send_from_directory(WEB, "manage.html")

    @app.get("/<path:asset>")
    def static_asset(asset):
        if asset not in PUBLIC_ASSETS | {"manage.html", "manage.js"}:
            abort(404)
        return send_from_directory(WEB, asset)

    @app.get("/api/sessions/config")
    @capability
    def session_config(token):
        result = pipeline.sessions.settings(token)
        result["worker_error"] = runtime.worker_errors.get("sessions")
        return jsonify(result)

    @app.post("/api/sessions/start")
    @limiter.limit("20 per hour")
    @capability
    def start_session(token):
        data = body({"resume_key"})
        return jsonify(pipeline.sessions.start(token, data.get("resume_key")))

    @app.post("/api/sessions/view")
    @capability
    def view_session(token):
        data = body({"resume_key", "session_id"})
        return jsonify(pipeline.sessions.view(data.get("resume_key"), data.get("session_id"), token))

    @app.post("/api/sessions/message")
    @limiter.limit("30 per minute")
    @capability
    def session_message(token):
        data = body({"resume_key", "session_id", "message_id", "message", "language", "product", "region", "as_of", "entities"})
        return jsonify(pipeline.sessions.message(data, token))

    @app.get("/api/rag/source")
    @capability
    def source(token):
        if set(request.args) - {"path", "version_id", "as_of", "content_hash"}:
            abort(400)
        return jsonify(pipeline.knowledge_source(request.args.get("path", ""), request.args.get("version_id", ""), token,
                      as_of=request.args.get("as_of"), content_hash=request.args.get("content_hash")))

    @app.post("/api/evidence/upload")
    @limiter.limit("6 per minute; 40 per day")
    @capability
    def upload(token):
        data = body({"filename", "content_base64"})
        content = base64.b64decode(data.get("content_base64", ""), validate=True)
        if sum(p.stat().st_size for p in Path(pipeline.config.evidence.upload_dir).rglob("*") if p.is_file()) + len(content) > 100_000_000:
            abort(507)
        ref = pipeline.save_evidence_upload(str(data.get("filename", "upload.txt")), content, token)
        runtime.accounts.own_upload(ref.file_id, g.user["id"], datetime.fromisoformat(ref.delete_after).timestamp())
        return jsonify(file_id=ref.file_id, filename=ref.filename, delete_after=ref.delete_after)

    @app.post("/api/evidence/analyze")
    @limiter.limit("20 per minute")
    @capability
    def analyze(token):
        data = body({"message", "file_refs"})
        runtime.accounts.check_uploads(data.get("file_refs"), g.user["id"])
        result = pipeline.analyze_evidence(str(data.get("message", ""))[:12000], data["file_refs"], token)
        return jsonify(result.to_dict()), 422 if result.status == "rejected" else 200

    @app.post("/api/tickets/from-conversation")
    @limiter.limit("10 per hour")
    @capability
    def ticket(token):
        data = body({"conversation", "resume_key", "session_id"})
        private = pipeline.sessions.view(data.get("resume_key"), data.get("session_id"), token)["session"]
        submitted = data.get("conversation")
        if not isinstance(submitted, dict):
            abort(400)
        refs = submitted.get("file_refs", [])
        runtime.accounts.check_uploads(refs, g.user["id"])
        # Identity and conversation history come from authenticated server state, not user-supplied IDs.
        conversation = {key: str(submitted.get(key, ""))[:4000] for key in ("message", "customer", "contact", "order", "product")}
        conversation.update(customer_id=g.user["id"], conversation_id="C-" + private["id"], unresolved=True,
                            messages=[{"role": "customer", "content": turn["content"]} for turn in private["history"]], file_refs=refs)
        return jsonify(pipeline.tickets.convert(conversation, token))

    @app.get("/api/admin/status")
    @capability
    def admin_status(token):
        return jsonify(**status_payload(pipeline, runtime.config_path), worker_errors=runtime.worker_errors,
                       users=runtime.accounts.users(), quality_cases=json.loads(runtime.cases_path.read_text(encoding="utf-8")),
                       health=pipeline.metrics.health_report(pipeline.store.active_version_id()).to_dict(),
                       tickets=pipeline.tickets.snapshot(token)["tickets"])

    @app.post("/api/admin/accounts")
    def accounts():
        data = body({"username", "password"})
        return jsonify(runtime.accounts.create_user(data.get("username", ""), data.get("password", ""))), 201

    @app.post("/api/admin/accounts/disable")
    def disable():
        data = body({"user_id"})
        runtime.accounts.disable_user(data.get("user_id"), g.user["id"])
        return jsonify(status="disabled")

    @app.post("/api/admin/accounts/password")
    def password():
        data = body({"user_id", "password"})
        runtime.accounts.reset_password(data.get("user_id"), data.get("password"))
        return jsonify(status="password_changed")

    @app.get("/api/admin/document")
    @capability
    def document(token):
        target = safe_document_path(pipeline, request.args.get("path", ""))
        content, metadata = unpack_document(target.read_text(encoding="utf-8"))
        return jsonify(content=content, metadata=metadata)

    @app.post("/api/admin/document")
    @capability
    def save(token):
        data = body({"path", "content", "metadata"})
        return jsonify(save_document(pipeline, data))

    @app.post("/api/admin/quality")
    @capability
    def quality(token):
        data = body({"cases"})
        runtime.save_cases(data.get("cases"))
        return jsonify(status="saved")

    @app.post("/api/admin/schedule")
    @capability
    def schedule(token):
        data = body({"update_time", "maintenance_start", "maintenance_end"})
        for value in data.values():
            parse_hhmm(value)
        for key, value in data.items():
            setattr(pipeline.config.schedule, key, value)
        runtime.persist_config()
        return jsonify(status="saved")

    @app.post("/api/admin/update")
    @limiter.limit("6 per hour")
    @capability
    def update(token):
        data = body({"action"})
        action = data.get("action")
        if action == "evaluate":
            result = pipeline.run_update(token, runtime.quality_cases())
            runtime.scheduler._record_result(pipeline.store.read_scheduler_state(), result, datetime.now(timezone.utc))
        elif action == "activate":
            result = pipeline.activate_pending(token)
            runtime.scheduler._record_result(pipeline.store.read_scheduler_state(), result, datetime.now(timezone.utc))
        elif action == "rollback":
            result = pipeline.rollback(token, reason="Administrator rollback")
        else:
            abort(400)
        runtime.finish_update(result)
        return jsonify(result.to_dict())

    if start_worker:
        runtime.start()
        atexit.register(runtime.stop)
    return app


def main():
    from waitress import serve
    settings = settings_from_environment()
    app = create_app(settings)
    proxy = {"trusted_proxy": "*", "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"},
             "trusted_proxy_count": 1} if settings.get("proxy") else {}
    # Waitress handles HTTP framing, connection limits, timeouts and bounded request bodies.
    serve(app, host="0.0.0.0" if settings["secure"] else "127.0.0.1", port=int(os.environ.get("PORT", "8766")),
          threads=8, max_request_body_size=3_000_000, channel_timeout=30, **proxy)


if __name__ == "__main__":
    main()

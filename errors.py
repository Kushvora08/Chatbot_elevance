class PipelineError(RuntimeError):
    """Base exception for pipeline failures."""


class AccessDenied(PipelineError):
    """Raised when a token is missing or does not allow the requested action."""


class UpdateRejected(PipelineError):
    """Raised when a candidate update fails quality gates."""


class ActivationDeferred(PipelineError):
    """Raised when activation is attempted outside the maintenance window."""


class HealthCheckFailed(PipelineError):
    """Raised when a newly activated version fails health checks."""


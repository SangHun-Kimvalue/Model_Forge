class SemanticValidatorError(Exception):
    """Base class for semantic validator failures."""


class SemanticValidatorConfigError(SemanticValidatorError):
    """Raised when a semantic validator is misconfigured."""


class SemanticValidationPipelineError(SemanticValidatorError):
    """Raised when generated geometry does not satisfy user intent."""

    stage = "semantic_validation"

    def __init__(
        self,
        message: str,
        *,
        missing_features: tuple[str, ...] = (),
        violated_constraints: tuple[str, ...] = (),
        retry_hint: str | None = None,
        decision_metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.missing_features = missing_features
        self.violated_constraints = violated_constraints
        self.retry_hint = retry_hint
        self.decision_metadata = dict(decision_metadata or {})
        self.decision_action = self.decision_metadata.get("action")
        self.decision_reason = self.decision_metadata.get("reason")
        self.decision_detail = self.decision_metadata.get("detail")

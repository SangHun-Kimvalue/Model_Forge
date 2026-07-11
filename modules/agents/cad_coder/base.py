from abc import ABC, abstractmethod

from modules.agents.cad_coder.schemas import CADCoderRequest, CADCoderResponse


class BaseCADCoderAgent(ABC):
    """Contract for mechanical subtask → CAD code generators.

    Preconditions:
        request.description is the mechanical-only portion of the prompt.
        request.dsl is honored by the adapter; the adapter must not silently
        emit code in a different DSL.
    Postconditions:
        response.code is a self-contained script in response.dsl that the
        sandbox runner can execute (response.dsl == request.dsl).
    Raises:
        CADCoderAgentError subclasses on configuration or generation failure.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def generate_code(self, request: CADCoderRequest) -> CADCoderResponse:
        """Produce CAD code for a single mechanical subtask."""

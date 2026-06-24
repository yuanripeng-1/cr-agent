from __future__ import annotations

from typing import Literal


StructuredOutputFailureKind = Literal["unsupported", "timeout", "empty", "error"]


class RuntimeCallError(Exception):
    """Base error for model runtime failures."""


class RuntimeTimeoutError(RuntimeCallError):
    """Raised when a model runtime call exceeds its timeout."""


class RuntimeTokenLimitError(RuntimeCallError):
    """Raised when the model runtime reports a token limit failure."""


class RuntimeContextLimitError(RuntimeCallError):
    """Raised when the model runtime reports a context window failure."""


class StructuredOutputError(RuntimeCallError):
    """LiteLLM json_schema structured output 失败。"""

    def __init__(self, message: str, *, kind: StructuredOutputFailureKind = "error") -> None:
        super().__init__(message)
        self.kind = kind


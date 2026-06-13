from __future__ import annotations


class RuntimeCallError(Exception):
    """Base error for model runtime failures."""


class RuntimeTimeoutError(RuntimeCallError):
    """Raised when a model runtime call exceeds its timeout."""


class RuntimeTokenLimitError(RuntimeCallError):
    """Raised when the model runtime reports a token limit failure."""


class RuntimeContextLimitError(RuntimeCallError):
    """Raised when the model runtime reports a context window failure."""


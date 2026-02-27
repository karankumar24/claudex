"""Abstract base class and result type for provider CLIs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..models import ErrorClass


@dataclass
class ProviderResult:
    """
    Unified result from any provider CLI invocation.
    Callers inspect `success` first, then either `text` or `error_class`.
    """
    success: bool

    # The assistant's full response text (only set when success=True)
    text: Optional[str] = None

    # The session/thread ID returned by the provider (used for resumption)
    session_id: Optional[str] = None

    # Error classification (only set when success=False)
    error_class: Optional[ErrorClass] = None

    # Human-readable error message for display (only set when success=False)
    error_message: Optional[str] = None

    # Raw stdout+stderr concatenated — kept for debugging, never shown by default
    raw_output: Optional[str] = field(default=None, repr=False)


class BaseProvider(ABC):
    """Common interface that all provider implementations must satisfy."""

    name: str  # "claude" or "codex"

    @abstractmethod
    def run(
        self,
        prompt: str,
        session_id: Optional[str],
        config: dict,
    ) -> ProviderResult:
        """
        Execute a single prompt turn against the provider CLI.

        Parameters
        ----------
        prompt:
            Full prompt text to send (may include prepended handoff context).
        session_id:
            If set, attempt to resume this session (provider-specific semantics).
        config:
            Merged config dict from load_config().

        Returns
        -------
        ProviderResult with success=True and text set, or success=False
        with error_class and error_message set.
        """
        ...

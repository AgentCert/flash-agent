"""
Flash Agent – Agent Interface (Contract)
==========================================

Defines the protocol that any agent implementation must satisfy.
A customer can replace FlashAgent with their own implementation
as long as it conforms to this interface.

Usage::

    from agent_interface import AgentInterface

    class MyCustomAgent:
        def scan(self, query: str) -> dict: ...
        def health_check(self) -> bool: ...
        def get_capabilities(self) -> list[str]: ...

    assert isinstance(MyCustomAgent(), AgentInterface)
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class AgentInterface(Protocol):
    """
    Contract for a pluggable analysis agent.

    The orchestrator (main.py) calls agent.scan() on each cycle.
    Everything inside scan() is the agent's business — the harness
    only cares about the returned dict conforming to the analysis schema.
    """

    def scan(self, query: str) -> Dict[str, Any]:
        """
        Execute one full analysis scan cycle.

        Args:
            query: Natural-language description of what to analyse.

        Returns:
            Analysis result dict containing at minimum 'health' and 'issues'.
        """
        ...

    def health_check(self) -> bool:
        """Return True if the agent is ready to accept scan requests."""
        ...

    def get_capabilities(self) -> List[str]:
        """Return list of capability identifiers this agent supports."""
        ...

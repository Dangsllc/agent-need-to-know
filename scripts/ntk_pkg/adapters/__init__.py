"""Adapter registry. One adapter (Claude Code) today."""
from .base import Adapter  # noqa: F401
from .claude_code import ClaudeCodeAdapter

_ADAPTERS = [ClaudeCodeAdapter()]


def active():
    """All registered adapters, in priority order."""
    return list(_ADAPTERS)


def get():
    """The adapter the engine drives. Today there is one; with several, the engine
    would loop over active() and route each capability id to the adapter that owns it."""
    return _ADAPTERS[0]

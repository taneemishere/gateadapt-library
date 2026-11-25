"""Errors the CLI turns into one readable line and a dedicated exit code.

InputError (exit 2) means a file or argument the user supplied is wrong.
AgentError (exit 4) means the user's run() raised or returned a bad score.
Neither may exit 1, which is REJECT: a crash must never read as a verdict.
"""

from __future__ import annotations


class InputError(ValueError):
    """A mutation, task deck, config, scope, cache or ledger is invalid."""


class AgentError(RuntimeError):
    """The user's run(task, config) failed on a task."""

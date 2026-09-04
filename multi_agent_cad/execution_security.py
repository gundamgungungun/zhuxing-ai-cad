"""Security helpers for subprocesses that execute generated CAD code.

The web runner needs model credentials while it talks to an LLM, but generated
Python must never inherit those credentials (or the hosting platform's own
credentials).  Keep the filtering in one place so every execution path applies
the same rule.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


_SENSITIVE_ENV_MARKERS = (
    "API_KEY",
    "ACCESS_KEY",
    "SECRET",
    "SESSION_TOKEN",
    "AUTH_TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)


def is_sensitive_env_name(name: str) -> bool:
    """Return whether an environment variable may contain a credential."""

    upper = name.upper()
    return any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def sanitized_subprocess_env(
    source: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment without credentials, then apply safe overrides."""

    base = os.environ if source is None else source
    result = {key: value for key, value in base.items() if not is_sensitive_env_name(key)}
    if extra:
        result.update(extra)
    return result


@contextmanager
def generated_code_environment(
    *, extra: Mapping[str, str] | None = None
) -> Iterator[dict[str, str]]:
    """Temporarily hide credentials from the parent and its generated child.

    The credentials are restored as soon as the generated process exits so the
    pipeline can continue making LLM calls during later repair iterations.
    """

    removed = {
        key: value for key, value in os.environ.items() if is_sensitive_env_name(key)
    }
    try:
        for key in removed:
            os.environ.pop(key, None)
        yield sanitized_subprocess_env(extra=extra)
    finally:
        os.environ.update(removed)

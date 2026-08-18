from __future__ import annotations

import os


TRUTHY_ENVIRONMENT_VALUES = {"1", "true", "yes", "on"}


def ephemeral_from_environment() -> bool:
    return os.getenv("CODEX_EPHEMERAL", "").strip().lower() in TRUTHY_ENVIRONMENT_VALUES

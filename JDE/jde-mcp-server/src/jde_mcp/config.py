"""Configuration loaded from environment / .env file.

Everything an operator can tune lives here so nothing is hard-coded in the
tool layer. The safety rails (read_only, max_rows) are deliberately read once
at startup rather than per call — an agent should never be able to change them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    ais_url: str
    username: str
    password: str
    device_name: str
    environment: str | None
    role: str | None
    read_only: bool
    max_rows: int
    token_ttl_seconds: int
    verify_tls: bool
    state_db: Path
    objects_file: Path

    @classmethod
    def load(cls) -> "Settings":
        pkg_root = Path(__file__).resolve().parent.parent.parent
        objects_file = Path(
            os.getenv("JDE_OBJECTS_FILE", pkg_root / "config" / "objects.yaml")
        )
        return cls(
            ais_url=os.getenv("JDE_AIS_URL", "").rstrip("/"),
            username=os.getenv("JDE_USERNAME", ""),
            password=os.getenv("JDE_PASSWORD", ""),
            device_name=os.getenv("JDE_DEVICE_NAME", "claude-mcp"),
            environment=os.getenv("JDE_ENVIRONMENT") or None,
            role=os.getenv("JDE_ROLE") or None,
            read_only=_bool("JDE_READ_ONLY", True),
            max_rows=_int("JDE_MAX_ROWS", 500),
            token_ttl_seconds=_int("JDE_TOKEN_TTL_SECONDS", 1500),
            verify_tls=_bool("JDE_VERIFY_TLS", True),
            state_db=Path(os.getenv("JDE_STATE_DB", "./jde_mcp_state.db")),
            objects_file=objects_file,
        )

    def validate(self) -> list[str]:
        """Return a list of problems, empty if the config is usable."""
        problems = []
        if not self.ais_url:
            problems.append("JDE_AIS_URL is not set")
        elif not self.ais_url.startswith(("http://", "https://")):
            problems.append("JDE_AIS_URL must start with http:// or https://")
        if not self.username:
            problems.append("JDE_USERNAME is not set")
        if not self.password:
            problems.append("JDE_PASSWORD is not set")
        if not self.objects_file.exists():
            problems.append(f"semantic model file not found: {self.objects_file}")
        return problems

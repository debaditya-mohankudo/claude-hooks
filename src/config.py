"""Central DB path config for claude-hooks.

All database paths live here. Other configs import from this module.

Environment variables (all optional, prefix CLAUDE_HOOKS_):
    CLAUDE_HOOKS_ICLOUD_DB_DIR   override iCloud Databases directory
    CLAUDE_HOOKS_MEMORY_DB       override MEMORY.sqlite path
"""
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ICLOUD_DEFAULT = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Databases"


class _Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_HOOKS_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    icloud_db_dir: Path = Field(default=_ICLOUD_DEFAULT)
    memory_db: Path = Field(default=Path.home() / ".claude" / "MEMORY.sqlite")

    # Memory scoring tunables (override via CLAUDE_HOOKS_MEMORY_* env vars)
    memory_top_n: int = Field(default=3)
    memory_batch_limit: int = Field(default=500)
    memory_tag_weight: float = Field(default=3.0)
    memory_body_weight: float = Field(default=1.0)
    memory_recency_boost: float = Field(default=1.2)
    memory_recency_penalty: float = Field(default=0.8)
    memory_recency_boost_days: int = Field(default=30)
    memory_recency_penalty_days: int = Field(default=180)
    memory_min_keyword_score: float = Field(default=0.2)

    @computed_field
    @property
    def tool_hints_db(self) -> Path:
        return self.icloud_db_dir / "tool_hints.sqlite"

    @computed_field
    @property
    def log_db(self) -> Path:
        return self.icloud_db_dir / "claude_hooks.sqlite"

    @computed_field
    @property
    def memory_scoring_json(self) -> Path:
        return self.icloud_db_dir / "memory_scoring.json"

    @property
    def memory_valid_types(self) -> list[str]:
        return ["feedback", "user", "project", "reference"]


config = _Config()

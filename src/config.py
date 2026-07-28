"""Central DB path config for claude-hooks.

All database paths live here. Other configs import from this module.

Environment variables (all optional, prefix CLAUDE_HOOKS_):
    CLAUDE_HOOKS_ICLOUD_DB_DIR   override iCloud Databases directory
    CLAUDE_HOOKS_MEMORY_DB       override MEMORY.sqlite path

CWD → domain mapping is loaded (mtime-cached) from cwd_domains.json in
icloud_db_dir — same pattern as memory_scoring.json. Keys are CWD substrings
(matched case-insensitively); first match wins. Add an entry there when
onboarding a new repo — no code change/redeploy needed.
"""
import json
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ICLOUD_DEFAULT = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Databases"

# Seed/fallback used if cwd_domains.json is missing or unreadable.
_CWD_DOMAIN_MAP_DEFAULT: dict[str, str] = {
    "claude-hooks": "claude-hooks",
    "vault": "vault",
    "market-intel": "market-intel",
    "astrology": "astrology",
    "K-mirror": "macos",
    "ACME_Cert_Life_Cycle": "acme",
    "Analyze_docker_logs_with_copilot": "docker-log-analysis",
}

_cwd_domain_map_cache: dict[str, str] = {}
_cwd_domain_map_mtime: float = 0.0


def _load_cwd_domain_map(path: Path) -> dict[str, str]:
    """Return CWD_DOMAIN_MAP, mtime-cached from `path` (falls back to the seed default)."""
    global _cwd_domain_map_cache, _cwd_domain_map_mtime
    try:
        mtime = path.stat().st_mtime
        if mtime != _cwd_domain_map_mtime:
            _cwd_domain_map_cache = json.loads(path.read_text())
            _cwd_domain_map_mtime = mtime
    except Exception:
        if not _cwd_domain_map_cache:
            _cwd_domain_map_cache = dict(_CWD_DOMAIN_MAP_DEFAULT)
    return _cwd_domain_map_cache


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
    tasks_db: Path = Field(default=Path.home() / ".claude" / "proj_tasks.db")

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
    memory_domain_keyword_boost: float = Field(default=0.8)

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

    @computed_field
    @property
    def cwd_domains_json(self) -> Path:
        return self.icloud_db_dir / "cwd_domains.json"

    @property
    def cwd_domain_map(self) -> dict[str, str]:
        return _load_cwd_domain_map(self.cwd_domains_json)

    @property
    def memory_valid_types(self) -> list[str]:
        return ["feedback", "user", "project", "reference"]


config = _Config()

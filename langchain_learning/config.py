"""Config for the langchain_learning package.

DB paths are delegated to src.config. This module adds LC-specific fields.

Environment variables (all optional, prefix LC_):
    LC_TOP_K   max scored memories to return
    LC_MODEL   Claude model for LLM components
"""
import sqlite3

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import config as _base
from src.logger import get_logger

_log = get_logger(__name__)


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LC_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    top_k: int = Field(default=7)
    model: str = Field(default="claude-haiku-4-5-20251001")
    # Set LC_DEV_MODE=true to exit 2 on hook errors instead of silently logging
    dev_mode: bool = Field(default=False)

    # DB paths — delegate to src.config
    @property
    def memory_db(self):
        return _base.memory_db

    @property
    def tool_hints_db(self):
        return _base.tool_hints_db

    @property
    def tasks_db(self):
        return _base.tasks_db

    @property
    def log_db(self):
        return _base.log_db

    @property
    def valid_domains(self) -> frozenset:
        try:
            with sqlite3.connect(self.memory_db) as conn:
                rows = conn.execute("SELECT DISTINCT domain FROM memories WHERE domain IS NOT NULL").fetchall()
        except Exception as exc:
            _log.warning("Failed to load valid_domains from MEMORY.sqlite: %s", exc)
            return frozenset({"global"})
        else:
            return frozenset(r[0] for r in rows)


config = Config()

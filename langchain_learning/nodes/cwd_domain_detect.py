"""CwdDomainDetectNode — deterministic domain detection from CWD map."""
from __future__ import annotations

from src.config import config as _cfg
from langchain_learning.nodes._node_log import entry
from langchain_learning.session_state import SessionState
from src.logger import get_logger

_log = get_logger(__name__)


class CwdDomainDetectNode:
    """Map state["cwd"] to a domain using cwd_domain_map (config.py, JSON-backed).

    Deterministic, zero-cost, sole domain source. CWD comes from SessionState
    (threaded from hook input) — never os.getcwd().

    cwd_domain_map is scoped to repo-less domains (vault, market-intel, astrology)
    — code repos get persistent context via task-framework loop memory instead.

    Tags: domain-classification, cwd, deterministic
    """

    def __call__(self, state: SessionState) -> dict:
        entry("cwd_domain_detect", state, cwd=state.get("cwd", "")[:40])

        detected: list[str] = list(state.get("domains", []))

        cwd = state.get("cwd", "")
        cwd_map = _cfg.cwd_domain_map
        for key, domain in cwd_map.items():
            if key.lower() in cwd.lower():
                if domain not in detected:
                    detected.append(domain)
                _log.info("[cwd_domain_detect] cwd=%r → domain=%s", key, domain)
                break

        return {"domains": detected}

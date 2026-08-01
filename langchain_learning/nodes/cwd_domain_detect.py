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

    Also flags cwd_unmapped=True the first turn of a session when cwd matches no
    entry, so dispatcher.py can nudge the user to add one to cwd_domains.json.
    Fires once per session (cwd_domain_reminder_sent, persisted via checkpoint).

    Tags: domain-classification, cwd, deterministic, onboarding-reminder
    """

    def __call__(self, state: SessionState) -> dict:
        entry("cwd_domain_detect", state, cwd=state.get("cwd", "")[:40])

        detected: list[str] = list(state.get("domains", []))

        cwd = state.get("cwd", "")
        cwd_map = _cfg.cwd_domain_map
        matched = False
        for key, domain in cwd_map.items():
            if key.lower() in cwd.lower():
                if domain not in detected:
                    detected.append(domain)
                _log.info("[cwd_domain_detect] cwd=%r → domain=%s", key, domain)
                matched = True
                break

        reminder_sent = state.get("cwd_domain_reminder_sent", False)
        cwd_unmapped = bool(cwd) and not matched and not reminder_sent
        if cwd_unmapped:
            _log.info("[cwd_domain_detect] cwd=%r unmapped — surfacing onboarding reminder", cwd[:40])

        return {
            "domains": detected,
            "cwd_unmapped": cwd_unmapped,
            "cwd_domain_reminder_sent": reminder_sent or cwd_unmapped,
        }

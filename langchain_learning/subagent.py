"""Standalone subagent — Claude CLI wrapped as a LangChain Runnable.

Uses session auth (keychain/OAuth) — no ANTHROPIC_API_KEY needed.
Suppresses all hooks by passing a minimal settings override from /tmp.
No CLAUDE.md discovery (cwd set to /tmp).

Usage (direct):
    uv run python langchain_learning/subagent.py "What is 12 * 34?"
    uv run python langchain_learning/subagent.py          # REPL

Usage (as Runnable in a chain):
    from langchain_learning.subagent import BareClaudeAgent
    agent = BareClaudeAgent()
    result = agent.invoke("Summarise this: ...")
    chain = prompt_template | agent | StrOutputParser()
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

from langchain_core.runnables import Runnable, RunnableConfig

# Minimal settings — empty hooks suppress all UserPromptSubmit/PreToolUse/etc.
_EMPTY_SETTINGS = {"hooks": {}}

# Written once at import; reused by all BareClaudeAgent instances.
def _write_tmp_settings() -> str:
    fd, path = tempfile.mkstemp(prefix="subagent_settings_", suffix=".json")
    os.write(fd, json.dumps(_EMPTY_SETTINGS).encode())
    os.close(fd)
    return path

_SETTINGS_PATH: str = _write_tmp_settings()


class BareClaudeAgent(Runnable[str, str]):
    """LangChain Runnable wrapping `claude -p` with no hooks and no CLAUDE.md.

    Input:  str  — the user prompt
    Output: str  — the model's text response

    Auth via existing Claude session (keychain/OAuth). A minimal settings file
    written to /tmp suppresses all hooks. cwd is /tmp to avoid CLAUDE.md pickup.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        system_prompt: str = "",
        safe_mode: bool = False,
        no_tools: bool = False,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.safe_mode = safe_mode
        self.no_tools = no_tools

    def _build_cmd(self, prompt: str, stream: bool = False) -> list[str]:
        cmd = [
            "claude", "-p",
            "--settings", _SETTINGS_PATH,
            "--model", self.model,
            "--output-format", "stream-json" if stream else "text",
        ]
        if self.safe_mode:
            cmd.append("--safe-mode")
        if self.no_tools:
            cmd += ["--tools", ""]
        if self.system_prompt:
            cmd += ["--system-prompt", self.system_prompt]
        cmd.append(prompt)
        return cmd

    def invoke(self, input: str, config: Optional[RunnableConfig] = None, **kwargs: Any) -> str:
        result = subprocess.run(
            self._build_cmd(input),
            capture_output=True, text=True,
            cwd="/tmp",  # avoid CLAUDE.md discovery in project dirs
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"claude exited {result.returncode}: {err}")
        return result.stdout.strip()

    def stream(self, input: str, config: Optional[RunnableConfig] = None, **kwargs: Any) -> Iterator[str]:
        with subprocess.Popen(
            self._build_cmd(input, stream=True),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd="/tmp",
        ) as proc:
            assert proc.stdout
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "text":
                    yield event.get("text", "")


# ---------------------------------------------------------------------------
# REPL / CLI entry point
# ---------------------------------------------------------------------------

def _repl(agent: BareClaudeAgent) -> None:
    print(f"BareClaudeAgent ready (model={agent.model}). Type 'exit' to quit.\n")
    history: list[str] = []
    while True:
        try:
            user = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user.lower() in {"exit", "quit"}:
            break
        if not user:
            continue
        full_prompt = "\n\n".join(history + [user]) if history else user
        reply = agent.invoke(full_prompt)
        history.append(f"You: {user}\nAssistant: {reply}")
        print(f"Agent: {reply}\n")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Subagent — session auth, no hooks")
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--system", default="")
    args = parser.parse_args()

    agent = BareClaudeAgent(model=args.model, system_prompt=args.system)
    if args.prompt:
        print(agent.invoke(" ".join(args.prompt)))
    else:
        _repl(agent)


if __name__ == "__main__":
    main()

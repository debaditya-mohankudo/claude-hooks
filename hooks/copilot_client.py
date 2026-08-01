#!/usr/bin/env python3
"""Copilot-friendly bridge for the claude-hooks hook server.

This script exposes a simple CLI for sending prompt/tool events to the same
FastAPI hook server used by Claude Code. It is intentionally lightweight so it
can be called from Copilot-oriented workflows or shell wrappers without needing
Claude Code's native hook payload format.

Examples:
  python3 hooks/copilot_client.py prompt --prompt "summarize this repo"
  python3 hooks/copilot_client.py pre-tool --tool-name imessage__send --session-id demo
  python3 hooks/copilot_client.py post-tool --tool-name imessage__send --session-id demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_SERVER = os.environ.get("CLAUDE_HOOKS_SERVER", "http://127.0.0.1:8766")


def _post_event(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{DEFAULT_SERVER}/hook/{event}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"raw": body}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"claude-hooks: server unreachable for {event}, failing open ({exc})", file=sys.stderr)
        return {}


def _build_prompt_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "session_id": args.session_id or "copilot",
        "prompt": args.prompt or "",
        "cwd": args.cwd or os.getcwd(),
    }


def _build_tool_payload(args: argparse.Namespace, event: str) -> dict[str, Any]:
    return {
        "session_id": args.session_id or "copilot",
        "tool_name": args.tool_name or "",
        "cwd": args.cwd or os.getcwd(),
        "tool_args": args.tool_args,
        "hookEventName": event,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Copilot-compatible hook payloads to claude-hooks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt_parser = subparsers.add_parser("prompt", help="Send a UserPromptSubmit payload")
    prompt_parser.add_argument("--prompt", default="", help="Prompt text to forward")
    prompt_parser.add_argument("--session-id", default=os.environ.get("CLAUDE_SESSION_ID", "copilot"))
    prompt_parser.add_argument("--cwd", default=os.environ.get("CLAUDE_CWD", os.getcwd()))

    tool_parser = subparsers.add_parser("pre-tool", help="Send a PreToolUse payload")
    tool_parser.add_argument("--tool-name", required=True)
    tool_parser.add_argument("--session-id", default=os.environ.get("CLAUDE_SESSION_ID", "copilot"))
    tool_parser.add_argument("--cwd", default=os.environ.get("CLAUDE_CWD", os.getcwd()))
    tool_parser.add_argument("--tool-args", default="{}", help="JSON object for tool args")

    tool_parser = subparsers.add_parser("post-tool", help="Send a PostToolUse payload")
    tool_parser.add_argument("--tool-name", required=True)
    tool_parser.add_argument("--session-id", default=os.environ.get("CLAUDE_SESSION_ID", "copilot"))
    tool_parser.add_argument("--cwd", default=os.environ.get("CLAUDE_CWD", os.getcwd()))
    tool_parser.add_argument("--tool-args", default="{}", help="JSON object for tool args")

    stop_parser = subparsers.add_parser("stop", help="Send a Stop payload")
    stop_parser.add_argument("--session-id", default=os.environ.get("CLAUDE_SESSION_ID", "copilot"))
    stop_parser.add_argument("--cwd", default=os.environ.get("CLAUDE_CWD", os.getcwd()))

    args = parser.parse_args()

    if args.command == "prompt":
        event = "UserPromptSubmit"
        payload = _build_prompt_payload(args)
    elif args.command == "pre-tool":
        event = "PreToolUse"
        try:
            args.tool_args = json.loads(args.tool_args)
        except json.JSONDecodeError:
            print("--tool-args must be valid JSON", file=sys.stderr)
            return 2
        payload = _build_tool_payload(args, event)
    elif args.command == "post-tool":
        event = "PostToolUse"
        try:
            args.tool_args = json.loads(args.tool_args)
        except json.JSONDecodeError:
            print("--tool-args must be valid JSON", file=sys.stderr)
            return 2
        payload = _build_tool_payload(args, event)
    elif args.command == "stop":
        event = "Stop"
        payload = {
            "session_id": args.session_id or "copilot",
            "cwd": args.cwd or os.getcwd(),
        }
    else:
        parser.error("unknown command")

    result = _post_event(event, payload)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

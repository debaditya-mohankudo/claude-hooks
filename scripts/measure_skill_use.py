#!/usr/bin/env python3
"""Skill use in main-session transcripts over a time window (task:df16e538).

How often `git commit`, `mac memory add` and `mac vault` writes ran with their
skill already loaded in the session, and what differed. A skill loaded after the
command, even in the same turn, does not count (task:1b86af46). The window
applies to each command's own timestamp; a skill loaded before it still counts.

The reminder went live at 2026-11-26T07:21:17Z (commit 4b80513): the baseline is
`--until` that time, the rerun is `--since` it. Do not "fix" a pattern here
without re-recording the baseline in docs/skill-usage-review-2026-11-26.md.
"""
import argparse
import collections
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

ACTS = {
    'commit': r'(^|&&|;|\n)\s*(git( -C \S+)? commit|mac git commit)\b',
    'vault-save': r'mac vault (create|append|write|update|edit)\b',
}
GRAPH = {'fed-rate-hike-odds','ai-capex-cycle-n-bubble','dxy-rise-odds-signals','fii-dii-flow-outlook','us-market-open-signal','india-market-open-signal','gold-price-action','india-risk-regime','kospi-open-signal','stock-tracker','index-inflows','gold-silver-correlation','weekly-india-signal','nifty-kospi-pair-lens','retirement-runway'}
SKIP = ('<local-command','<task-notification','<agent-message','<cross-session','[Request interrupted','Caveat:','This session is being continued')


def _time(text):
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _turns(path):
    """The file's turns, each a list of ('skill', name, None) and ('cmd', command, time) in event order."""
    turns = []
    for line in open(path, errors='ignore'):
        try: e = json.loads(line)
        except ValueError: continue
        if e.get('isSidechain') or e.get('isMeta'): continue
        c = (e.get('message') or {}).get('content')
        if e.get('type') == 'user':
            if isinstance(c, list):
                if any(b.get('type') == 'tool_result' for b in c):
                    continue
                c = ' '.join(b.get('text','') for b in c if b.get('type')=='text')
            if not c or c.startswith(SKIP): continue
            m = re.search(r'<command-name>/?(.*?)<', c)
            turns.append([('skill', m.group(1), None)] if m else [])
        elif e.get('type') == 'assistant' and turns and isinstance(c, list):
            for b in c:
                if b.get('type') != 'tool_use': continue
                i = b.get('input') or {}
                if b['name'] == 'Skill':
                    turns[-1].append(('skill', i.get('skill'), None))
                elif b['name'] == 'Bash' and e.get('timestamp'):
                    turns[-1].append(('cmd', i.get('command',''), _time(e['timestamp'])))
                elif b['name'] == 'Read' and re.search(r'skills/.*(skill|SKILL)\.md', i.get('file_path','')):
                    turns[-1].append(('skill', i['file_path'].split('skills/')[1].split('/')[0], None))
    return turns


def measure(paths, since, until):
    """Counters keyed by (action, state) for commands with since <= time < until."""
    q = collections.defaultdict(collections.Counter)
    for path in paths:
        seen = set()  # filled at the Skill call itself, not at its result
        for turn in _turns(path):
            cmds = [x for kind, x, _ in turn if kind == 'cmd']  # whole turn: the test measure ignores order
            earlier = ''  # this turn's commands before the current one
            for kind, value, at in turn:
                if kind == 'skill':
                    if value: seen.add(value)
                    continue
                c, before = value, earlier
                earlier += c + '\n'
                if not since <= at < until: continue
                if re.search(ACTS['commit'], c):
                    k = ('commit', 'loaded' if 'commit' in seen else 'bypassed'); q[k]['n'] += 1
                    q[k]['task_id_visible'] += bool(re.search(r'task:[0-9a-f]{6}|(-F|--file)', c))
                    q[k]['suite_run_same_turn'] += any(re.search(r'pytest|unittest|npm test|swift test', x) for x in cmds)
                add = re.search(r'mac memory add(?!_batch)\b', c)
                if add:
                    k = ('memory add', 'loaded' if 'add-memory' in seen else 'bypassed'); q[k]['n'] += 1
                    q[k]['with --related'] += '--related' in c; q[k]['with --links'] += '--links' in c
                    q[k]['searched_first'] += bool(re.search(r'mac memory (search|get)', before + c[:add.start()]))
                if re.search(ACTS['vault-save'], c):
                    state = 'loaded' if 'vault-save' in seen else ('graph skill' if seen & GRAPH else 'bypassed')
                    q[('vault write', state)]['n'] += 1
    return q


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--since', type=_time, help='ISO time; default --days before --until')
    ap.add_argument('--until', type=_time, default=datetime.now(timezone.utc), help='ISO time; default now')
    ap.add_argument('--days', type=float, default=14)
    a = ap.parse_args()
    since = a.since or a.until - timedelta(days=a.days)
    print(f'window {since.isoformat()} .. {a.until.isoformat()}')
    paths = glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl'))
    for k, v in sorted(measure(paths, since, a.until).items()): print(k, dict(v))


if __name__ == '__main__':
    main()

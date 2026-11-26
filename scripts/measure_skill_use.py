#!/usr/bin/env python3
"""Skill use in the last 14 days of main-session transcripts (task:df16e538).

How often `git commit`, `mac memory add` and `mac vault` writes ran with their
skill loaded earlier in the session, and what differed. The matching is the same
as in the 2026-11-26 review so a rerun compares like with like: do not "fix" a
pattern here without re-recording the baseline in
docs/skill-usage-review-2026-11-26.md.
"""
import collections
import glob
import json
import os
import re
import time

cut = time.time() - 14*86400
prompts = []
for f in glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')):
    if os.path.getmtime(f) < cut: continue
    cur = None
    for line in open(f, errors='ignore'):
        try: e = json.loads(line)
        except ValueError: continue
        if e.get('isSidechain') or e.get('isMeta'): continue
        m = e.get('message') or {}
        c = m.get('content')
        if e.get('type') == 'user':
            if isinstance(c, list):
                if any(b.get('type') == 'tool_result' for b in c):
                    continue
                c = ' '.join(b.get('text','') for b in c if b.get('type')=='text')
            if not c or c.startswith(('<local-command','<task-notification','<agent-message','<cross-session','[Request interrupted','Caveat:','This session is being continued')): continue
            cur = {'p': c, 'f': f, 'skills': [], 'cmds': []}
            prompts.append(cur)
        elif e.get('type') == 'assistant' and cur and isinstance(c, list):
            for b in c:
                if b.get('type') != 'tool_use': continue
                i = b.get('input') or {}
                if b['name'] == 'Skill': cur['skills'].append(i.get('skill'))
                if b['name'] == 'Bash': cur['cmds'].append(i.get('command',''))
                if b['name'] == 'Read' and re.search(r'skills/.*(skill|SKILL)\.md', i.get('file_path','')): cur['skills'].append(i['file_path'].split('skills/')[1].split('/')[0])
acts = {
 'commit': r'(^|&&|;|\n)\s*(git( -C \S+)? commit|mac git commit)\b',
 'add-memory': r'mac memory add(_batch)?\b',
 'vault-save': r'mac vault (create|append|write|update|edit)\b',
}
seen = collections.defaultdict(set); q = collections.defaultdict(collections.Counter)
graph = {'fed-rate-hike-odds','ai-capex-cycle-n-bubble','dxy-rise-odds-signals','fii-dii-flow-outlook','us-market-open-signal','india-market-open-signal','gold-price-action','india-risk-regime','kospi-open-signal','stock-tracker','index-inflows','gold-silver-correlation','weekly-india-signal','nifty-kospi-pair-lens','retirement-runway'}
for p in prompts:
    f=p['f']; seen[f] |= {s for s in p['skills'] if s}
    m = re.search(r'<command-name>/?(.*?)<', p['p'])
    if m: seen[f].add(m.group(1))
    for c in p['cmds']:
        if re.search(acts['commit'], c):
            k=('commit', 'loaded' if 'commit' in seen[f] else 'bypassed'); q[k]['n']+=1
            q[k]['task_id_visible'] += bool(re.search(r'task:[0-9a-f]{6}|(-F|--file)', c))
            q[k]['suite_run_same_turn'] += any(re.search(r'pytest|unittest|npm test|swift test', x) for x in p['cmds'])
        if re.search(r'mac memory add(?!_batch)\b', c):
            k=('memory add', 'loaded' if 'add-memory' in seen[f] else 'bypassed'); q[k]['n']+=1
            q[k]['with --related'] += '--related' in c; q[k]['with --links'] += '--links' in c; q[k]['searched_first'] += any('mac memory search' in x or 'mac memory get' in x for x in p['cmds'])
        if re.search(acts['vault-save'], c):
            q[('vault write', 'loaded' if 'vault-save' in seen[f] else ('graph skill' if seen[f]&graph else 'bypassed'))]['n']+=1
for k,v in sorted(q.items()): print(k, dict(v))


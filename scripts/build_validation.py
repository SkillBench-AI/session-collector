#!/usr/bin/env python3
"""Build validation data from flat and full exports.

Compares dist/skillbench_export.json (flat/gather) against
dist/skillbench_export_full.json (gather --full), computing per-agent
stats, schema checks, and exporting matched session pairs for
side-by-side exploration.

Outputs:
  dist/validation-data.js      — stats, checks, session index (const VDATA)
  dist/validation-sessions.js  — matched session pairs with messages (const VSESSIONS)
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

DIST = os.path.join(os.path.dirname(__file__), '..', 'dist')
FLAT_PATH = os.path.join(DIST, 'skillbench_export.json')
FULL_PATH = os.path.join(DIST, 'skillbench_export_full.json')
OUT_DATA  = os.path.join(DIST, 'validation-data.js')
OUT_SESS  = os.path.join(DIST, 'validation-sessions.js')

VALID_ROLES = {'user', 'agent'}
VALID_BLOCK_TYPES = {
    'text', 'tool_use', 'tool_result', 'thinking',
    'output_text', 'image', 'document',
}
def load(path):
    with open(path) as f:
        return json.load(f)


def index_by_session(convs):
    return {c['session_id']: c for c in convs}


def msg_stats(messages):
    roles = Counter()
    bad_roles = []
    missing_created_at = 0
    missing_content = 0
    for m in messages:
        roles[m['role']] += 1
        if m['role'] not in VALID_ROLES:
            bad_roles.append(m['role'])
        if 'created_at' not in m:
            missing_created_at += 1
        if 'content' not in m:
            missing_content += 1
    return {
        'total': len(messages),
        'roles': dict(roles),
        'bad_roles': list(set(bad_roles)),
        'missing_created_at': missing_created_at,
        'missing_content': missing_content,
    }


def block_stats(messages):
    types = Counter()
    for m in messages:
        content = m.get('content')
        if isinstance(content, list):
            for b in content:
                types[b.get('type', '?')] += 1
        elif isinstance(content, str):
            types['string'] += 1
    return dict(types)


def tool_summary(messages):
    """Count tool_use by name."""
    tools = Counter()
    for m in messages:
        content = m.get('content')
        if isinstance(content, list):
            for b in content:
                if b.get('type') == 'tool_use':
                    tools[b.get('name', '?')] += 1
    return dict(tools)


MAX_FIELD = 2000  # max chars per text field (tool results can be huge)


def cap(s, n=MAX_FIELD):
    """Cap string length for browser performance."""
    if not isinstance(s, str):
        return s
    return s[:n] + (f'... ({len(s)-n} more chars)' if len(s) > n else '')


def export_message(m):
    """Return a copy of a message for export with large fields capped."""
    content = m.get('content', '')
    if isinstance(content, str):
        out_content = cap(content)
    elif isinstance(content, list):
        out_content = []
        for b in content:
            tb = dict(b)
            bt = b.get('type', '?')
            if bt == 'text':
                tb['text'] = cap(b.get('text', ''))
            elif bt == 'tool_use':
                inp = b.get('input', {})
                tb['input'] = cap(json.dumps(inp, ensure_ascii=False))
            elif bt == 'tool_result':
                raw = b.get('content', '')
                if isinstance(raw, list):
                    parts = []
                    for bl in raw:
                        if bl.get('type') == 'text':
                            parts.append(bl.get('text', ''))
                    raw = '\n'.join(parts)
                tb['content'] = cap(str(raw))
            elif bt == 'thinking':
                tb['thinking'] = cap(b.get('thinking', b.get('text', '')))
                tb.pop('text', None)
            elif bt == 'image':
                tb['source'] = {'type': 'base64', 'media_type': b.get('source', {}).get('media_type', '?')}
            out_content.append(tb)
    else:
        out_content = cap(str(content))
    return {
        'role': m['role'],
        'created_at': m.get('created_at', ''),
        'content': out_content,
    }


def build_session_pairs(flat_idx, full_idx):
    """Build matched session pairs for the explorer."""
    pairs = []
    for sid in sorted(flat_idx.keys()):
        fc = flat_idx[sid]
        uc = full_idx.get(sid)
        flat_msgs = [export_message(m) for m in fc['messages']]
        full_msgs = [export_message(m) for m in uc['messages']] if uc else []

        # Compute per-session block stats for full
        full_blocks = {}
        full_tools = {}
        if uc:
            full_blocks = block_stats(uc['messages'])
            full_tools = tool_summary(uc['messages'])

        pairs.append({
            'session_id': sid,
            'agent': fc['agent'],
            'title': fc.get('title', ''),
            'workspace': fc.get('workspace', ''),
            'started_at': fc.get('started_at', ''),
            'full_fidelity': bool(uc and uc.get('full_fidelity')),
            'flat_count': len(fc['messages']),
            'full_count': len(uc['messages']) if uc else 0,
            'flat_msgs': flat_msgs,
            'full_msgs': full_msgs,
            'full_blocks': full_blocks,
            'full_tools': full_tools,
        })
    return pairs


def build():
    print('Loading flat export...')
    flat = load(FLAT_PATH)
    print(f'  {len(flat)} conversations')

    print('Loading full export...')
    full = load(FULL_PATH)
    print(f'  {len(full)} conversations')

    flat_idx = index_by_session(flat)
    full_idx = index_by_session(full)

    agents_flat = sorted(set(c['agent'] for c in flat))
    agents_full = sorted(set(c['agent'] for c in full))
    all_agents = sorted(set(agents_flat + agents_full))

    # Per-agent stats
    agent_stats = {}
    for agent in all_agents:
        flat_convs = [c for c in flat if c['agent'] == agent]
        full_convs = [c for c in full if c['agent'] == agent]
        flat_msgs_all = [m for c in flat_convs for m in c['messages']]
        full_msgs_all = [m for c in full_convs for m in c['messages']]
        agent_stats[agent] = {
            'flat_convs': len(flat_convs),
            'full_convs': len(full_convs),
            'flat': msg_stats(flat_msgs_all),
            'full': msg_stats(full_msgs_all),
            'full_blocks': block_stats(full_msgs_all),
            'full_fidelity_count': sum(1 for c in full_convs if c.get('full_fidelity')),
        }

    all_flat_msgs = [m for c in flat for m in c['messages']]
    all_full_msgs = [m for c in full for m in c['messages']]

    # Schema checks
    checks = []

    flat_bad = set()
    full_bad = set()
    for m in all_flat_msgs:
        if m['role'] not in VALID_ROLES:
            flat_bad.add(m['role'])
    for m in all_full_msgs:
        if m['role'] not in VALID_ROLES:
            full_bad.add(m['role'])
    checks.append({
        'name': 'All roles are "user" or "agent"',
        'flat_pass': len(flat_bad) == 0,
        'full_pass': len(full_bad) == 0,
        'flat_detail': f'Bad roles: {sorted(flat_bad)}' if flat_bad else 'OK',
        'full_detail': f'Bad roles: {sorted(full_bad)}' if full_bad else 'OK',
    })

    flat_asst = sum(1 for m in all_flat_msgs if m['role'] == 'assistant')
    full_asst = sum(1 for m in all_full_msgs if m['role'] == 'assistant')
    checks.append({
        'name': 'No "assistant" role (unified to "agent")',
        'flat_pass': flat_asst == 0,
        'full_pass': full_asst == 0,
        'flat_detail': f'{flat_asst} messages with assistant role' if flat_asst else 'OK',
        'full_detail': f'{full_asst} messages with assistant role' if full_asst else 'OK',
    })

    flat_miss = sum(1 for m in all_flat_msgs if 'created_at' not in m)
    full_miss = sum(1 for m in all_full_msgs if 'created_at' not in m)
    checks.append({
        'name': 'All messages have "created_at"',
        'flat_pass': flat_miss == 0,
        'full_pass': full_miss == 0,
        'flat_detail': f'{flat_miss} missing' if flat_miss else 'OK',
        'full_detail': f'{full_miss} missing' if full_miss else 'OK',
    })

    flat_ts = sum(1 for m in all_flat_msgs if 'timestamp' in m)
    full_ts = sum(1 for m in all_full_msgs if 'timestamp' in m)
    checks.append({
        'name': 'No legacy "timestamp" field',
        'flat_pass': flat_ts == 0,
        'full_pass': full_ts == 0,
        'flat_detail': f'{flat_ts} messages with timestamp' if flat_ts else 'OK',
        'full_detail': f'{full_ts} messages with timestamp' if full_ts else 'OK',
    })

    flat_nc = sum(1 for m in all_flat_msgs if 'content' not in m)
    full_nc = sum(1 for m in all_full_msgs if 'content' not in m)
    checks.append({
        'name': 'All messages have "content" field',
        'flat_pass': flat_nc == 0,
        'full_pass': full_nc == 0,
        'flat_detail': f'{flat_nc} missing' if flat_nc else 'OK',
        'full_detail': f'{full_nc} missing' if full_nc else 'OK',
    })

    ff_count = sum(1 for c in full if c.get('full_fidelity'))
    checks.append({
        'name': 'Full mode: conversations have full_fidelity=true',
        'flat_pass': True,
        'full_pass': ff_count == len(full),
        'flat_detail': 'N/A (flat mode)',
        'full_detail': f'{ff_count}/{len(full)} have full_fidelity' if ff_count != len(full) else 'OK',
    })

    bad_block_types = set()
    for m in all_full_msgs:
        content = m.get('content')
        if isinstance(content, list):
            for b in content:
                bt = b.get('type', '?')
                if bt not in VALID_BLOCK_TYPES:
                    bad_block_types.add(bt)
    checks.append({
        'name': 'Full mode: block types are valid',
        'flat_pass': True,
        'full_pass': len(bad_block_types) == 0,
        'flat_detail': 'N/A (flat mode)',
        'full_detail': f'Unknown types: {sorted(bad_block_types)}' if bad_block_types else 'OK',
    })

    conv_agents_flat = set(c.get('agent', '') for c in flat)
    conv_agents_full = set(c.get('agent', '') for c in full)
    checks.append({
        'name': 'No "claude" agent slug (should be "claude_code")',
        'flat_pass': 'claude' not in conv_agents_flat,
        'full_pass': 'claude' not in conv_agents_full,
        'flat_detail': 'Has "claude" slug' if 'claude' in conv_agents_flat else 'OK',
        'full_detail': 'Has "claude" slug' if 'claude' in conv_agents_full else 'OK',
    })

    # --- Write validation-data.js (stats only, small) ---
    vdata = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'flat_path': os.path.basename(FLAT_PATH),
        'full_path': os.path.basename(FULL_PATH),
        'overview': {
            'flat_convs': len(flat),
            'full_convs': len(full),
            'flat_msgs': len(all_flat_msgs),
            'full_msgs': len(all_full_msgs),
            'agents': all_agents,
            'checks_passed': sum(1 for c in checks if c['flat_pass'] and c['full_pass']),
            'checks_total': len(checks),
        },
        'agents': agent_stats,
        'checks': checks,
    }

    with open(OUT_DATA, 'w') as f:
        f.write('const VDATA = ' + json.dumps(vdata, indent=2) + ';\n')
    data_kb = os.path.getsize(OUT_DATA) / 1024
    print(f'\nWrote {OUT_DATA} ({data_kb:.1f} KB)')

    # --- Build session pairs and write validation-sessions.js ---
    print('Building session pairs...')
    pairs = build_session_pairs(flat_idx, full_idx)

    with open(OUT_SESS, 'w') as f:
        f.write('const VSESSIONS = ')
        json.dump(pairs, f, ensure_ascii=False)
        f.write(';\n')
    sess_kb = os.path.getsize(OUT_SESS) / 1024
    print(f'Wrote {OUT_SESS} ({sess_kb:.1f} KB, {len(pairs)} sessions)')

    print(f'\n  Agents: {all_agents}')
    print(f'  Checks: {vdata["overview"]["checks_passed"]}/{vdata["overview"]["checks_total"]} passed')
    for c in checks:
        if not c['flat_pass'] or not c['full_pass']:
            marker = ''
            if not c['flat_pass']:
                marker += f' [FLAT FAIL: {c["flat_detail"]}]'
            if not c['full_pass']:
                marker += f' [FULL FAIL: {c["full_detail"]}]'
            print(f'  FAIL: {c["name"]}{marker}')


if __name__ == '__main__':
    build()

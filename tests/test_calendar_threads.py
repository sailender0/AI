"""calThreads() — the chat grouping behind the day panel.

The logic lives in the browser, so the check runs the real page script in node
rather than restating it in Python. teams_outlook.js declares functions and
consts at the top level and touches the DOM only inside handlers, so it loads
outside a browser as-is.
"""
import subprocess
import tempfile
from pathlib import Path

PAGE_JS = Path("app/static/pages/teams_outlook.js")
MY_DAY_JS = Path("app/static/pages/my_day.js")

CHECK = """
const assert = require('assert');
const chat = (time, person, from_self = false) =>
  ({ type: 'chat', time, person_id: person, title: person, from_self });

// A burst with one person collapses to a single thread.
let out = calThreads([chat('10:01', 'a'), chat('10:02', 'a', true), chat('10:03', 'a')]);
assert.strictEqual(out.length, 1);
assert.strictEqual(out[0].msgs.length, 3);
assert.strictEqual(out[0].time, '10:01');
assert.strictEqual(out[0].end, '10:03');

// The same person again after a long quiet stretch is a second thread.
out = calThreads([chat('09:00', 'a'), chat('18:30', 'a')]);
assert.strictEqual(out.length, 2);

// Two people talking in the same window stay apart.
out = calThreads([chat('10:00', 'a'), chat('10:01', 'b'), chat('10:02', 'a')]);
assert.strictEqual(out.length, 2);
assert.deepStrictEqual(out.map(t => t.msgs.length), [2, 1]);

// A mail landing mid-conversation neither splits the thread nor gets grouped.
out = calThreads([chat('10:00', 'a'),
                  { type: 'received', time: '10:01', title: 'Someone' },
                  chat('10:02', 'a')]);
assert.strictEqual(out.length, 2);
assert.strictEqual(out[0].msgs.length, 2);
assert.strictEqual(out[1].type, 'received');
assert.strictEqual(out[1].msgs, undefined);

// Exactly at the gap still joins; one minute past it does not.
assert.strictEqual(calThreads([chat('10:00', 'a'), chat('10:30', 'a')]).length, 1);
assert.strictEqual(calThreads([chat('10:00', 'a'), chat('10:31', 'a')]).length, 2);

// Non-chat rows are passed through untouched, in order.
out = calThreads([{ type: 'meeting', time: '09:00', title: 'Standup' },
                  { type: 'call', time: '11:00', title: 'Priya' }]);
assert.deepStrictEqual(out.map(i => i.type), ['meeting', 'call']);
"""


MY_DAY_CHECK = """
const assert = require('assert');
const msg = (occurred_at, title) => ({ source: 'teams_chat', title, occurred_at });

// Newest first, the order renderTimeline() receives.
let out = chatByPerson([msg('2026-07-30T10:23:00Z', 'Priya'),
                        msg('2026-07-30T10:10:00Z', 'Priya'),
                        msg('2026-07-30T10:01:00Z', 'Priya')]);
assert.strictEqual(out.length, 1);
assert.strictEqual(out[0]._n, 3);
assert.strictEqual(out[0].occurred_at, '2026-07-30T10:23:00Z');  // latest kept
assert.strictEqual(out[0]._from, '2026-07-30T10:01:00Z');        // earliest walked back to

// One row per person, in the order each was first seen.
out = chatByPerson([msg('2026-07-30T11:00:00Z', 'Priya'),
                    msg('2026-07-30T10:00:00Z', 'Sam'),
                    msg('2026-07-30T09:00:00Z', 'Priya')]);
assert.deepStrictEqual(out.map(r => [r.title, r._n]), [['Priya', 2], ['Sam', 1]]);

// A lone message still renders as itself — _n of 1 is what the template checks.
out = chatByPerson([msg('2026-07-30T10:00:00Z', 'Sam')]);
assert.strictEqual(out[0]._n, 1);
"""

STUB = "globalThis.window = globalThis; globalThis.location = { search: '' };\n"


def _run_js(node, source: str):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(source)
        path = f.name
    try:
        return subprocess.run([node, path], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)


def test_chat_messages_group_into_threads(node):
    r = _run_js(node, PAGE_JS.read_text(encoding="utf-8") + "\n;\n" + CHECK)
    assert r.returncode == 0, r.stderr[:800]


def test_my_day_collapses_chat_per_person(node):
    r = _run_js(node, STUB + MY_DAY_JS.read_text(encoding="utf-8") + "\n;\n" + MY_DAY_CHECK)
    assert r.returncode == 0, r.stderr[:800]

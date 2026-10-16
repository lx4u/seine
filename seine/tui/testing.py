# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Test cockpit: mirrors build.py's BuildState/start_build() shape, an
# App-level worker running seine.testing.runner through the same
# TextualReporter -- no test-specific reporting code needed.

import os
import time

from rich.style import Style
from rich.text import Text

from seine.progress import elapsed
from seine.testing import runner, testindex
from seine.tui.reporter import TextualReporter
from seine.tui.sanitize import sanitize

# Same marks as the Build/Test screens, so a test reads the same in
# the overview as where it ran. Only passed/failed get a color.
TEST_MARKS = {"pending": "○", "running": "●", "done": "✔", "failed": "✘"}
TEST_MARK_STYLES = {"done": "green", "failed": "red"}

def _test_state_for(qualified, rows):
    row = (rows or {}).get(qualified)
    if row is None:
        return "pending"
    return row.get("state", "pending")

def _tests_under(subpath, test_paths):
    prefix = tuple(subpath)
    found = []
    for qualified, path in (test_paths or {}).items():
        if tuple(path)[:len(prefix)] == prefix:
            found.append(qualified)
    return found

def _aggregate_test_state(qualifieds, rows):
    states = [_test_state_for(q, rows) for q in qualifieds]
    if any(s == "failed" for s in states):
        return "failed"
    if any(s == "running" for s in states):
        return "running"
    if any(s == "pending" for s in states):
        return "pending"
    return "done"

# Index statuses use Robot's own words (PASS/FAIL/SKIP), not the
# session marks above -- mapped here so a past run reads with the
# same green ✔ / red ✘ as a live one.
_INDEX_MARKS = {"PASS": ("✔", "green"), "FAIL": ("✘", "red"),
                "SKIP": ("○", "")}

def _run_time(started):
    if not started:
        return "?"
    return time.strftime("%b %d %H:%M", time.localtime(started))

# Newest-first index entries covering one qualified test: the index
# first, with scan() backfilling pre-index run dirs. 'recorded' and
# 'scanned' are loaded once per pane render and shared by every row.
def _history_for(qualified, recorded, scanned):
    runs = list(testindex.runs_for(qualified, recorded))
    known = {entry.get("dir") for entry in recorded}
    runs += [entry for entry in scanned
             if entry.get("dir") not in known
             and testindex.status_of(entry, qualified) is not None]
    runs.sort(key=lambda entry: entry.get("started") or 0, reverse=True)
    return runs

# One branch child's trailing 'last ✔ Sep 11' -- the newest run
# covering it, or None when it never ran. Only for children leading
# to exactly one test; an aggregate over several has no single run.
def _last_run_suffix(qualified, recorded, scanned):
    runs = _history_for(qualified, recorded, scanned)
    if not runs:
        return None
    row = testindex.status_of(runs[0], qualified) or {}
    mark, _ = _INDEX_MARKS.get(row.get("status"), ("?", ""))
    return "last %s %s" % (mark, _run_time(runs[0].get("started")))

# How many runs' links a test history block shows.
LOGS_SHOWN = 3

# Full per-test history under a single-test node: the last run plus
# the LOGS_SHOWN most recent runs, each with log and replay links --
# the same clickable-span shape _logs_section() uses above.
def _test_history_block(qualified, recorded, scanned):
    runs = _history_for(qualified, recorded, scanned)[:LOGS_SHOWN]
    text = Text()
    if not runs:
        text.append("\nnever run from here yet -- '/test' it once first\n")
        return text
    row = testindex.status_of(runs[0], qualified) or {}
    mark, style = _INDEX_MARKS.get(row.get("status"), ("?", ""))
    text.append("\nLast run: %s " % _run_time(runs[0].get("started")))
    if style:
        text.append(mark + " ", style=style)
    else:
        text.append(mark + " ")
    text.append("%s" % (row.get("status") or "?"))
    if row.get("elapsed"):
        text.append(" (%s)" % elapsed(row["elapsed"]))
    text.append("\nAll runs:\n")
    for entry in runs:
        row = testindex.status_of(entry, qualified) or {}
        mark, style = _INDEX_MARKS.get(row.get("status"), ("?", ""))
        text.append("  - %s " % _run_time(entry.get("started")))
        if style:
            text.append(mark + " ", style=style)
        else:
            text.append(mark + " ")
        log = os.path.join(testindex.resolve(entry.get("dir")), "output.xml")
        if os.path.isfile(log):
            text.append("[log] ", style=Style(meta={"log-click": log}))
        else:
            text.append("[log] ", style=Style(dim=True))
        cast = testindex.cast_for(entry, qualified)
        if cast is not None:
            text.append("[replay]", style=Style(meta={"cast-play": cast}))
        else:
            text.append("[replay]", style=Style(dim=True))
        text.append("\n")
    return text

# A 'test'-branch node with its children's test status, one mark per
# child that leads to test(s). Returns None when 'subpath' holds no
# test, so the caller falls back to render_node().
def render_test_node(node, subpath, test_state):
    subpath = tuple(subpath)
    test_paths = getattr(test_state, "test_paths", None) or {}
    rows = getattr(test_state, "rows", None) or {}
    qualified_here = _tests_under(subpath, test_paths)
    if not qualified_here:
        return None
    by_name = {}
    result = getattr(test_state, "result", None)
    if result is not None:
        by_name = {t.name: t for t in (result.tests or [])}
    recorded = testindex.entries()
    scanned = testindex.scan()
    text = Text()
    state = _aggregate_test_state(qualified_here, rows)
    mark = TEST_MARKS[state]
    style = TEST_MARK_STYLES.get(state, "")
    if style:
        text.append(mark + " ", style=style)
    else:
        text.append(mark + " ")
    text.append("%s\n" % node.data)
    # A single failed test's reason, same as TestState.render() shows
    # under its own row, so a failure always shows a reason here too.
    if len(qualified_here) == 1:
        outcome = by_name.get(qualified_here[0])
        if outcome is not None and outcome.failed and outcome.message:
            text.append("    %s\n" % sanitize(outcome.message))
    if node.children:
        text.append("\n")
        for child in node.children:
            child_sub = subpath + (child.data,)
            qualified_child = _tests_under(child_sub, test_paths)
            if not qualified_child:
                text.append("  %s\n" % child.data)
                continue
            child_state = _aggregate_test_state(qualified_child, rows)
            child_mark = TEST_MARKS[child_state]
            child_style = TEST_MARK_STYLES.get(child_state, "")
            text.append("  ")
            if child_style:
                text.append(child_mark + " ", style=child_style)
            else:
                text.append(child_mark + " ")
            text.append("%s" % child.data)
            # Past runs read dimmed after the live mark, so the two
            # never compete: colour means this session, grey the past.
            if len(qualified_child) == 1:
                suffix = _last_run_suffix(qualified_child[0],
                                          recorded, scanned)
                if suffix is not None:
                    text.append("  %s" % suffix, style=Style(dim=True))
            text.append("\n")
            if len(qualified_child) == 1:
                outcome = by_name.get(qualified_child[0])
                if outcome is not None and outcome.failed and outcome.message:
                    text.append("      %s\n" % sanitize(outcome.message))
    # One test exactly: the full history below its live mark. A
    # branch over several tests already carries each child's own
    # 'last' line above -- a block per test would bury the listing.
    if len(qualified_here) == 1:
        text.append_text(_test_history_block(qualified_here[0],
                                             recorded, scanned))
    return text


class TestState:
    def __init__(self):
        self.files = []
        self.rows = {}
        self.order = []
        self.worker = None
        self.message = None
        self.error = False
        self.done = False
        self.result = None
        # Running test's spec-tree path, keyed like 'rows'; empty when
        # no spec was given to compute it from.
        self.test_paths = {}
        # Every keyword-level log line seen so far; TestScreen's #tail
        # pane tails this list since there's no log file here.
        self.output_lines = []
        # Bumped on every reset() so TestScreen can tell a fresh run
        # apart from one still going, and clear #tail instead of
        # appending to it.
        self.run_id = 0
        # Set by SeineApp.__init__, same shape as BuildState.on_finished.
        self.on_finished = None
        # Same, for the start edge: a fresh run clears the previous
        # per-test states, so whatever is on screen is stale.
        self.on_started = None
        # Same, for every test's own start/finish: one mark flips,
        # so a listing of them is one mark behind.
        self.on_changed = None

    @property
    def running(self):
        return self.worker is not None and self.worker.is_running

    def reset(self, files, spec=None):
        from seine.tui import spectree
        self.files = files
        self.rows = {}
        self.order = []
        self.message = None
        self.error = False
        self.done = False
        self.result = None
        self.test_paths = spectree.test_paths(spec) if spec else {}
        self.output_lines = []
        self.run_id += 1
        if self.on_started:
            self.on_started()

    # Reporter sink: named task_started/task_finished/sampled to match
    # TextualReporter's calls, not the Reporter protocol's own names.
    def task_started(self, name):
        self.rows.setdefault(name, {"state": "pending"})
        self.rows[name]["state"] = "running"
        if name not in self.order:
            self.order.append(name)
        if self.on_changed:
            self.on_changed()

    def task_finished(self, name, failed=False):
        self.rows.setdefault(name, {"state": "pending"})
        self.rows[name]["state"] = "failed" if failed else "done"
        if self.on_changed:
            self.on_changed()

    def say(self, text):
        self.message = text

    def sampled(self, sample):
        pass

    def output(self, name, line):
        # Robot keyword messages can carry the target's own escape
        # sequences (colours, cursor moves); a RichLog is not a
        # terminal, so they'd escape its pane -- sanitize first.
        self.output_lines.append("%s| %s" % (name, sanitize(line)))

    def finished_ok(self, result):
        self.done = True
        self.result = result
        self.error = not result.ok
        self.message = result.summary()
        if self.on_finished:
            self.on_finished()

    def finished_failed(self, text):
        self.done = True
        self.error = True
        self.message = text
        if self.on_finished:
            self.on_finished()

    # A failed row's message goes right under it, same as the CLI and
    # the AI chat's 'run-test' tool, so a failure always shows a reason.
    def render(self):
        if len(self.order) == 0:
            return "no test run yet -- '/test SPEC...'\n"
        marks = {"pending": "○", "running": "●", "done": "✔", "failed": "✘"}
        by_name = {t.name: t for t in self.result.tests} if self.result else {}
        lines = []
        for name in self.order:
            lines.append("%s %s" % (marks[self.rows[name]["state"]], name))
            outcome = by_name.get(name)
            if outcome is not None and outcome.failed and outcome.message:
                lines.append("    %s" % sanitize(outcome.message))
        if self.result is not None:
            outdir = os.path.dirname(self.result.output_xml)
            lines.append("")
            lines.append(self.result.summary())
            lines.append("output under %s" % outdir)
            if not self.result.ok:
                lines.append("see %s/console.log (%s/console.cast) and %s/interactions.json "
                            "for what led up to it" % (outdir, outdir, outdir))
        return "\n".join(lines) + "\n"

# Mirrors start_build()'s shape: a worker thread, a Reporter crossing
# back through call_from_thread. 'spec', already parsed, saves reloading
# a spec the active session has open.
def start_test(app, state, files, spec=None, tags=None, outdir=None):
    if state.running:
        raise RuntimeError("a test run is already running")
    state.reset(files, spec=spec)
    reporter = TextualReporter(app, state)

    def run():
        try:
            result = runner.run_spec(files, spec=spec, tags=tags,
                                     outdir=outdir, reporter=reporter)
        except Exception as e:
            app.call_from_thread(state.finished_failed, "%s: %s" % (type(e).__name__, e))
            return
        app.call_from_thread(state.finished_ok, result)

    state.worker = app.run_worker(run, thread=True, exclusive=True, group="test")

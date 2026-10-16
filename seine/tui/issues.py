# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The '/issues' screen: a per-source CVE/defect matrix beside summary
# stats, no spec tree -- the matrix is the reason to be here. Selecting
# a count swaps the matrix for that cell's own entries; Esc goes back.

import os
import re
import subprocess

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, LoadingIndicator, Static

from seine import bugs as bugs_module
from seine import sbom
from seine import secscan
from seine import secscan as secscan_module
from seine.tui.base import BaseScreen, StaticPane

# Same "exactly one active group" restriction ai.py's tools apply to a
# spec-scoped call; multi-group specifications aren't driven here yet.
def _issues_build(context):
    if not context.active:
        return None, "no active specification -- '/use SPEC...' picks one\n"
    if len(context.builds) != 1:
        return None, ("multi-group specifications ('/use a -- b') aren't "
                      "supported here yet -- '/use' a single one\n")
    return context.builds[0], None

# The active build's SBOM path, or the "not built yet" message, shared
# by both render_issues_* below so the two panes never disagree.
def _issues_sbom_path(build):
    path = sbom.output_path(build.image._output)
    if os.path.isfile(path):
        return path, None
    return None, ("no SBOM for this build yet\n"
                  "run '/build' (or 'seine build --sbom') first\n")

# One row per source package: CVE counts by urgency, defect counts by
# severity. A zero count is blank space, so the eye reads the nonzero
# ones; a nonzero count selects into that cell's own entries.
SEC_LETTERS = (("high", "H"), ("medium", "M"), ("low", "L"),
               ("unimportant", "U"), ("end-of-life", "E"),
               ("not-yet-assigned", "N"))
BUG_LETTERS = (("critical", "C"), ("grave", "G"), ("serious", "S"),
               ("important", "I"))

# Counts print COUNT_WIDTH wide, space-padded on the left; the table
# column is one wider, for breathing room.
COUNT_WIDTH = 4

def _issues_rows(context, package=None, min_urgency=None, min_severity=None,
                rescan=False):
    # (sources, {(source, kind, bucket): [entries]}, bugs_cached) --
    # ValueError (a bad pattern/level) and scan failures propagate as
    # (None, message) instead, so both panes below never disagree.
    build, error = _issues_build(context)
    if error:
        return None, error
    path, error = _issues_sbom_path(build)
    if error:
        return None, error
    release = build.spec["distribution"]["release"]
    try:
        regex = re.compile(package, re.IGNORECASE) if package else None
    except re.error as e:
        return None, "'%s' is not a usable pattern: %s\n" % (package, e)
    try:
        findings = secscan.scan(path, distro=release, rescan=rescan)
        findings = secscan.filter_findings(findings, min_urgency=min_urgency)
    except ValueError as e:
        return None, "%s\n" % e
    except (OSError, subprocess.CalledProcessError) as e:
        return None, "scan failed: %s\n" % e
    # Defects come from the cache only, never a fetch: rendering must
    # stay instant and offline-safe. Only an explicit '/issues --rescan'
    # (which already blocks on the CVE scan above) refreshes both.
    if rescan:
        try:
            bugs = bugs_module.scan(path, distro=release, rescan=True)
        except (OSError, ValueError) as e:
            return None, "defects fetch failed: %s\n" % e
        bugs_cached = True
    else:
        bugs = bugs_module.read_cache(path)
        bugs_cached = bugs is not None
        bugs = bugs or []
    try:
        bugs = bugs_module.filter_bugs(bugs, min_severity=min_severity or
                                       bugs_module.DEFAULT_MIN_SEVERITY)
    except ValueError as e:
        return None, "%s\n" % e
    if regex is not None:
        findings = [f for f in findings if regex.search(f.package)]
        bugs = [b for b in bugs if regex.search(b.source)]
    cells = {}
    for f in findings:
        cells.setdefault((f.package, "security", f.urgency), []).append(f)
    for b in bugs:
        cells.setdefault((b.source, "defects", b.severity), []).append(b)
    sources = sorted({f.package for f in findings} | {b.source for b in bugs})
    return (sources, cells, bugs_cached), None

def issues_matrix_data(context, package=None, min_urgency=None,
                       min_severity=None, rescan=False):
    result, error = _issues_rows(context, package=package,
                                 min_urgency=min_urgency,
                                 min_severity=min_severity, rescan=rescan)
    if error:
        return None, error
    sources, cells, bugs_cached = result
    columns = [("SOURCE", max([len(s) for s in sources] + [len("SOURCE")]), "source")]
    columns += [(letter, COUNT_WIDTH + 1, "security:" + bucket)
                for bucket, letter in SEC_LETTERS]
    columns += [(letter, COUNT_WIDTH + 1, "defects:" + bucket)
                for bucket, letter in BUG_LETTERS]
    rows = []
    for source in sources:
        row = [source]
        for bucket, _ in SEC_LETTERS:
            entries = cells.get((source, "security", bucket), [])
            row.append("%*d" % (COUNT_WIDTH, len(entries)) if entries else "")
        for bucket, _ in BUG_LETTERS:
            entries = cells.get((source, "defects", bucket), [])
            row.append("%*d" % (COUNT_WIDTH, len(entries)) if entries else "")
        rows.append((source, row))
    return (columns, rows, bugs_cached), None

# One clicked cell's own entries, replacing the matrix in the same pane
# until Esc. 'kind' is 'security' or 'defects', 'bucket' one of the
# letters' levels above.
def render_issues_detail(context, source, kind, bucket, package=None,
                         min_urgency=None, min_severity=None):
    result, error = _issues_rows(context, package=package,
                                 min_urgency=min_urgency,
                                 min_severity=min_severity)
    if error:
        return error
    _, cells, _ = result
    entries = cells.get((source, kind, bucket), [])
    if not entries:
        return "no %s/%s entries for '%s' (Esc goes back)\n" % (kind, bucket, source)
    lines = ["%s -- %s/%s (%d, Esc goes back)" % (source, kind, bucket, len(entries)), ""]
    if kind == "security":
        width = max(len(e.cve) for e in entries)
        for e in entries:
            lines.append("%-*s %-12s %-18s %s" % (width, e.cve, e.version,
                                                 e.urgency, e.status))
            if e.tracker:
                lines.append("  %s" % e.tracker)
    else:
        for e in entries:
            lines.append("#%-9d [%-9s/%s] %s" % (e.id, e.severity, e.status, e.title))
    return "\n".join(lines) + "\n"

# Reads whatever render_issues_matrix() left cached rather than
# scanning again; called after it in IssuesScreen.update_body(), so a
# '/issues --rescan' has already refreshed the cache by the time this runs.
def render_issues_stats(context):
    build, error = _issues_build(context)
    if error:
        return error
    path, error = _issues_sbom_path(build)
    if error:
        return error
    release = build.spec["distribution"]["release"]
    try:
        findings = secscan.scan(path, distro=release)
    except (OSError, subprocess.CalledProcessError) as e:
        return "scan failed: %s\n" % e
    data = secscan.stats(findings)
    lines = ["TOTALS", " %d findings" % data["total"],
             " %d unique CVEs" % data["unique_cves"],
             " %d packages affected" % data["packages"], "", "BY URGENCY"]
    for level in secscan.URGENCY_ORDER:
        lines.append(" %-17s %4d" % (level, data["by_urgency"].get(level, 0)))
    lines += ["", "BY STATUS"]
    for status, count in data["by_status"].most_common():
        lines.append(" %-17s %4d" % (status, count))
    lines += ["", "TOP PACKAGES"]
    for pkg, count in data["by_package"].most_common(10):
        lines.append(" %-17s %4d" % (pkg, count))
    lines += ["", "DEFECTS (UDD, severity >= important)"]
    # Cache only, never a fetch: rendering must stay instant and
    # offline-safe. 'seine issues --defects' populates what this reads.
    cached = bugs_module.read_cache(path)
    if cached is None:
        lines.append(" no defect data -- '/issues --rescan'")
    else:
        shown = bugs_module.filter_bugs(cached)
        defects = bugs_module.stats(shown)
        lines.append(" %d bug(s) across %d source package(s)"
                     % (defects["total"], defects["sources"]))
        for level in bugs_module.SEVERITY_ORDER:
            count = defects["by_severity"].get(level, 0)
            if count:
                lines.append(" %-17s %4d" % (level, count))
    return "\n".join(lines) + "\n"

LEGEND = ("H=high M=medium L=low U=unimportant E=end-of-life N=not-yet-assigned\n"
          "C=critical G=grave S=serious I=important -- "
          "Enter/click a count for details, Esc goes back")

# Progress popup for a background '/issues --rescan'. Esc dismisses it
# early -- the scan keeps going and the matrix refreshes when it lands.
class RescanModal(ModalScreen):
    DEFAULT_CSS = """
    RescanModal { align: center middle; }
    #rescanpane {
        width: 60; height: auto;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #rescantitle { text-style: bold; }
    #rescanstatus { height: auto; padding-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss", show=False)]

    def compose(self):
        with Vertical(id="rescanpane"):
            yield Static("Rescanning issues…", id="rescantitle")
            yield LoadingIndicator()
            yield Static("", id="rescanstatus", markup=False)

    def action_dismiss(self):
        self.app.pop_screen()

    # Called from the worker thread via call_from_thread; the modal may
    # already be dismissed by then, in which case there is nothing to
    # update and no crash either.
    def set_status(self, text):
        try:
            self.query_one("#rescanstatus", Static).update(text)
        except NoMatches:
            pass

# Both halves of one rescan, split out so tests can stub the network
# and container without touching the worker/modal around them.
# 'report' takes one status line for the modal.
def _rescan_once(sbom_path, distro, report):
    report("scanning CVEs against the security tracker…")
    secscan_module.scan(sbom_path, distro=distro, rescan=True)
    report("fetching defects from UDD…")
    bugs_module.scan(sbom_path, distro=distro, rescan=True)

class IssuesScreen(BaseScreen):
    HINT_ADD = [("complete", "detail", "Esc back (viewing a detail)")]

    # The stats pane shrink-wraps its own contents -- whatever width
    # its longest line needs, no more -- and the matrix keeps the rest.
    DEFAULT_CSS = """
    #issuestable-pane { width: 1fr; height: 100%; border: round $foreground 40%; }
    #issuelegend { height: auto; padding: 0 1; color: $text-muted; }
    #issuestable { height: 1fr; }
    # Headers read as headers through bold text (and the header's
    # underline rule) alone -- their background stays the table's own,
    # never Textual's contrasting $panel/$secondary-muted fills.
    #issuestable .datatable--header { text-style: bold underline; background: $surface; }
    #issuestable .datatable--fixed { text-style: bold; background: $surface; }
    #issuedetail-pane { height: 1fr; }
    #issuesstats-pane { width: auto; height: 100%; border: round $foreground 40%; }
    #issuesstats { width: auto; }
    """

    # No-op unless a detail view is showing -- same shape as
    # OverviewScreen's own 'escape' -> action_close_log.
    BINDINGS = BaseScreen.BINDINGS + [Binding("escape", "close_detail", show=False)]

    def compose_content(self):
        yield Horizontal(
            Vertical(
                Static(LEGEND, id="issuelegend", markup=False),
                DataTable(id="issuestable", show_cursor=True, fixed_columns=1),
                StaticPane(Static(id="issuedetail", markup=False), id="issuedetail-pane"),
                id="issuestable-pane",
            ),
            StaticPane(Static(id="issuesstats", markup=False), id="issuesstats-pane"),
            id="main",
        )

    def on_mount(self):
        # Set before super().on_mount(): it calls refresh_data() right
        # away, too soon to read this if set after. (source, kind,
        # bucket) triple for the detail shown, or None for the matrix.
        self._detail = None
        # A rescan worker is in flight; a second '/issues --rescan'
        # before it lands is refused rather than stacked.
        self._rescanning = False
        super().on_mount()

    # A count cell's key reads "kind:bucket"; the source column's own
    # key is just "source" and selects nothing.
    def on_data_table_cell_selected(self, event):
        column = event.cell_key.column_key.value
        if column == "source":
            return
        kind, _, bucket = column.partition(":")
        self.action_show_detail(event.cell_key.row_key.value, kind, bucket)

    # Swaps the matrix for one cell's entries -- Esc
    # (action_close_detail) swaps back.
    def action_show_detail(self, source, kind, bucket):
        self._detail = (str(source), str(kind), str(bucket))
        self.update_body()

    def action_close_detail(self):
        if self._detail is not None:
            self._detail = None
            self.update_body()

    # Columns and rows keep their own keys, so a later CellSelected
    # reads (source, kind, bucket) straight from its cell key.
    def _show_matrix(self, context):
        table = self.query_one("#issuestable", DataTable)
        table.clear(columns=True)
        result, error = issues_matrix_data(
            context, package=self.app.issues_filter,
            min_urgency=self.app.issues_min_urgency,
            min_severity=self.app.issues_min_severity)
        if error:
            self.query_one("#issuelegend", Static).update(error)
            return
        columns, rows, bugs_cached = result
        for label, width, key in columns:
            table.add_column(label, width=width, key=key)
        for source, cells in rows:
            table.add_row(*cells, key=source)
        if rows:
            legend = LEGEND
            if not bugs_cached:
                legend += "\ndefect counts are empty -- '/issues --rescan'"
            self.query_one("#issuelegend", Static).update(legend)
        else:
            self.query_one("#issuelegend", Static).update("no known CVEs or defects found")

    # 'rescan' is one-shot: only '/issues --rescan' forces a fresh scan.
    # It runs in a worker thread under a progress modal, so the matrix
    # (and a detail view, if showing) stays up until fresh caches land.
    def update_body(self):
        rescan = self.app.issues_rescan
        self.app.issues_rescan = False
        context = self.app.context
        showing_detail = self._detail is not None
        self.query_one("#issuelegend", Static).display = not showing_detail
        self.query_one("#issuestable", DataTable).display = not showing_detail
        self.query_one("#issuedetail-pane", StaticPane).display = showing_detail
        if showing_detail:
            source, kind, bucket = self._detail
            self.query_one("#issuedetail", Static).update(
                render_issues_detail(context, source, kind, bucket,
                                     package=self.app.issues_filter,
                                     min_urgency=self.app.issues_min_urgency,
                                     min_severity=self.app.issues_min_severity))
        else:
            self._show_matrix(context)
        self.query_one("#issuesstats", Static).update(render_issues_stats(context))
        if rescan:
            if self._rescanning:
                self.say("a rescan is already running")
            else:
                self._start_rescan(context)

    # The SBOM a rescan would scan, or None (with the reason said aloud)
    # when there is nothing to scan yet.
    def _rescan_target(self, context):
        build, error = _issues_build(context)
        if error:
            self.say(error.strip(), error=True)
            return None
        path, error = _issues_sbom_path(build)
        if error:
            self.say(error.strip(), error=True)
            return None
        return path, build.spec["distribution"]["release"]

    def _start_rescan(self, context):
        target = self._rescan_target(context)
        if target is None:
            return
        sbom_path, distro = target
        self._rescanning = True
        modal = RescanModal()
        self.app.push_screen(modal)
        report = lambda text: self.app.call_from_thread(modal.set_status, text)

        def run():
            try:
                _rescan_once(sbom_path, distro, report)
            except Exception as e:
                self.app.call_from_thread(self._rescan_done, str(e))
            else:
                self.app.call_from_thread(self._rescan_done, None)

        self.app.run_worker(run, thread=True, exclusive=True,
                            group="issues-rescan")

    # NoMatches throughout: the screen may be gone already if the
    # session ended mid-scan.
    def _rescan_done(self, error):
        self._rescanning = False
        try:
            if error:
                self.say("rescan failed: %s" % error, error=True)
            else:
                self.update_body()
                self.say("rescan complete")
            if isinstance(self.app.screen, RescanModal):
                self.app.pop_screen()
        except NoMatches:
            pass

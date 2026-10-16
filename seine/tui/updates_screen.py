# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The /updates screen: release lineage, commit details, and delta matrix.

import os
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

from seine.imager import payload
from seine.tui.artifacts import _human_size
from seine.tui.base import BaseScreen, StaticPane


class LineageTable(DataTable):
    can_focus = True


class DeltaMatrix(DataTable):
    can_focus = True


class ActionBanner(Static):
    can_focus = False


class UpdatesScreen(BaseScreen):
    DEFAULT_CSS = """
    #updates-body { height: 1fr; }
    #updates-header { height: 3; padding: 1 2 0 2; }
    #updates-main { height: 1fr; }
    #updates-left { width: 1fr; height: 100%; padding: 0 1; }
    #updates-right { width: 1fr; height: 100%; padding: 0 1; }
    #lineage { height: 1fr; border: round $foreground 40%; }
    #lineage:focus { border: round $border; }
    #detail { height: 8; border: round $foreground 40%; padding: 0 1; }
    #matrix { height: 1fr; border: round $foreground 40%; }
    #matrix:focus { border: round $border; }
    #actions { height: 2; padding: 0 1; }
    .pane-title { padding: 0 1; text-style: bold; color: $accent; }
    """

    BINDINGS = BaseScreen.BINDINGS + [
        Binding("g", "generate_delta", "Generate Delta"),
        Binding("tab", "toggle_focus", "Next Pane"),
        Binding("enter", "inspect_selection", "Inspect", show=False),
    ]

    HINT_ADD = [
        ("complete", "inspect", "Enter Inspect"),
        ("inspect", "generate", "G Generate Delta"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lineage_data = []
        self._deltas_data = {}
        self._selected_version = None

    def compose_content(self):
        yield Vertical(
            Static(id="updates-header", markup=False),
            Horizontal(
                Vertical(
                    Static("Release Lineage", id="lineage-title", classes="pane-title"),
                    LineageTable(id="lineage", cursor_type="row", show_cursor=True),
                    Static("Selected Commit Detail", id="detail-title", classes="pane-title"),
                    StaticPane(Static(id="detail-content", markup=False), id="detail"),
                    id="updates-left",
                ),
                Vertical(
                    Static("Delta Compression Matrix", id="matrix-title", classes="pane-title"),
                    DeltaMatrix(id="matrix", cursor_type="cell", show_cursor=True),
                    ActionBanner(
                        "[bold]Quick Actions:[/] [cyan]G[/] Generate Delta   "
                        "[cyan]Ctrl+B[/] Build Full Disk + Payload   [cyan]Ctrl+U[/] Payload Only",
                        id="actions",
                    ),
                    id="updates-right",
                ),
                id="updates-main",
            ),
            id="updates-body",
        )

    def on_mount(self):
        lineage_table = self.query_one("#lineage", LineageTable)
        lineage_table.add_columns(
            "Version", "Commit Hash", "Timestamp", "Kernel Version",
            "Status", "Signed UKI", "Static Deltas"
        )
        super().on_mount()

    def update_body(self):
        context = self.app.context
        header_widget = self.query_one("#updates-header", Static)
        lineage_table = self.query_one("#lineage", LineageTable)
        matrix_table = self.query_one("#matrix", DeltaMatrix)
        detail_widget = self.query_one("#detail-content", Static)

        lineage_table.clear()
        matrix_table.clear(columns=True)

        if not context.active:
            header_widget.update("no active specification -- '/use SPEC...' picks one")
            detail_widget.update("No active specification.")
            return

        build = context.builds[0]
        ostree_spec = build.spec.get("image", {}).get("ostree")
        if not ostree_spec:
            header_widget.update("active specification does not configure an OSTree update payload")
            detail_widget.update("Active specification does not have an 'image: ostree:' section.")
            return

        ref = ostree_spec.get("ref", "debian/amd64")
        release = build.spec["distribution"]["release"]
        from seine.container import ContainerEngine
        output = getattr(getattr(build, "image", None), "_output", None)
        if not output:
            image_name = build.spec.get("image", {}).get("filename", "image.img")
            output = os.path.join(ContainerEngine.deploy_dir(release), image_name)
        payload_dir = payload.payload_dir(output, ostree_spec.get("payload"), release)

        lineage = payload.list_payload_lineage(payload_dir)
        deltas_info = payload.list_payload_deltas(payload_dir)
        self._lineage_data = lineage
        self._deltas_data = deltas_info

        total_size = sum(e["uki"].get("size", 0) for e in lineage if e.get("uki"))
        total_deltas = len(deltas_info.get("deltas", []))
        total_ukis = len([e for e in lineage if e.get("uki")])
        tip_commit = lineage[-1]["commit_short"] if lineage else "none"

        header_lines = [
            f"Payload Repository: {payload_dir}",
            f"Branch Ref:         {ref:<36} Tip: {tip_commit} (synchronized)",
            f"Total Artifacts:    {len(lineage)} Versions · {total_ukis} UKIs · {total_deltas} Static Deltas · {_human_size(total_size)} Total",
        ]
        header_widget.update("\n".join(header_lines))

        for entry in lineage:
            lineage_table.add_row(
                f"v{entry['version']}",
                entry["commit_short"],
                entry["timestamp"],
                entry["kernel"],
                entry["status"],
                entry["uki_str"],
                entry["deltas"],
                key=entry["version"],
            )

        # Setup delta matrix columns
        versions = deltas_info.get("versions", [])
        matrix = deltas_info.get("matrix", {})
        if versions:
            matrix_table.add_column("From \\ To", width=14, key="from_col")
            for v in versions:
                label = f"v{v} Update" if v != versions[0] else f"v{v} Baseline"
                matrix_table.add_column(label, width=24, key=f"col_v{v}")
            matrix_table.add_column("Status / Path", width=18, key="status_col")

            for v_from in versions:
                row_cells = [f"v{v_from}"]
                row_status = "-"
                for v_to in versions:
                    cell = matrix.get((v_from, v_to), {})
                    txt = cell.get("text", "-")
                    row_cells.append(txt)
                    if cell.get("status") and cell["status"] != "-":
                        row_status = cell["status"]
                if row_status == "-" and v_from == versions[-1]:
                    row_status = "Current HEAD"
                row_cells.append(row_status)
                matrix_table.add_row(*row_cells, key=f"row_v{v_from}")

        # Update commit detail pane
        selected_version = self._selected_version or (lineage[-1]["version"] if lineage else None)
        self._update_commit_detail(selected_version)

    def _update_commit_detail(self, version):
        detail_widget = self.query_one("#detail-content", Static)
        if not version or not self._lineage_data:
            detail_widget.update("No commit selected.")
            return

        entry = next((e for e in self._lineage_data if e["version"] == version), None)
        if not entry:
            detail_widget.update("No commit selected.")
            return

        self._selected_version = version
        lines = [
            f"Author:  {entry.get('author', 'seine imager <builder@local>'):<40} Signed By: {entry.get('signed_by', 'unsigned')}",
            f"Commit:  {entry.get('commit', '')}",
            f"Ref:     {entry.get('ref', '')} (version {entry.get('version')})",
            f"Delta:   {entry.get('deltas', '(factory baseline)')}",
            f"UKI:     {entry.get('uki_str', '(none)')} (Secure Boot signed: {entry.get('uki', {}).get('signed', False)})",
        ]
        detail_widget.update("\n".join(lines))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.data_table.id == "lineage" and event.row_key:
            version = str(event.row_key.value)
            self._update_commit_detail(version)

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted):
        if event.data_table.id == "lineage" and event.cell_key:
            row_key = event.cell_key.row_key
            if row_key:
                version = str(row_key.value)
                self._update_commit_detail(version)

    def action_toggle_focus(self):
        matrix = self.query_one("#matrix", DeltaMatrix)
        lineage = self.query_one("#lineage", LineageTable)
        if matrix.has_focus:
            lineage.focus()
        else:
            matrix.focus()

    def action_generate_delta(self):
        versions = self._deltas_data.get("versions", [])
        if len(versions) < 2:
            self.app.say("delta generate: needs at least 2 versions in payload repository", warning=True)
            return

        matrix = self.query_one("#matrix", DeltaMatrix)
        lineage = self.query_one("#lineage", LineageTable)

        from_ver = None
        to_ver = None

        if matrix.has_focus and matrix.cursor_coordinate:
            row_idx, col_idx = matrix.cursor_coordinate.row, matrix.cursor_coordinate.column
            if 0 <= row_idx < len(versions) and 1 <= col_idx <= len(versions):
                from_ver = versions[row_idx]
                to_ver = versions[col_idx - 1]

        if not from_ver or not to_ver or from_ver == to_ver:
            sel = self._selected_version or (lineage.cursor_row if lineage.cursor_row is not None else None)
            if sel is not None and str(sel) in versions:
                idx = versions.index(str(sel))
                if idx > 0:
                    from_ver = versions[idx - 1]
                    to_ver = versions[idx]
                else:
                    from_ver = versions[0]
                    to_ver = versions[1]
            else:
                from_ver = versions[0]
                to_ver = versions[-1]

        if from_ver == to_ver:
            self.app.say("delta generate: source and target versions must differ", warning=True)
            return

        cmd = f"/delta generate --from={from_ver} --to={to_ver}"
        from seine.tui import commands
        self.app.run_worker(lambda: commands.dispatch(self.app, cmd), thread=True)

    def action_inspect_selection(self):
        if self._selected_version:
            self.app.say(f"selected version v{self._selected_version}")

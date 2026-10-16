# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The /updates screen: release lineage, commit details, and delta matrix.

import os
from rich.text import Text
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
        self._query_target_status()

    def on_screen_resume(self):
        super().on_screen_resume()
        self._query_target_status()

    def _query_target_status(self):
        from seine.tui import target
        if not target.available():
            return

        def _worker():
            try:
                client = getattr(self.app, "_target_client", None)
                if client is None:
                    client = target.get_client(self.app)
                st = target.status(self.app)
                if hasattr(self.app, "target_state"):
                    self.app.target_state.seed(st)
                self.app.call_from_thread(self.update_body)
            except Exception:
                pass

        self.run_worker(_worker, thread=True, group="updates-target-status")

    def update_body(self):
        context = self.app.context
        header_widget = self.query_one("#updates-header", Static)
        lineage_table = self.query_one("#lineage", LineageTable)
        matrix_table = self.query_one("#matrix", DeltaMatrix)
        detail_widget = self.query_one("#detail-content", Static)

        saved_matrix_coord = matrix_table.cursor_coordinate
        saved_lineage_row = lineage_table.cursor_row

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

        target_state = getattr(self.app, "target_state", None)
        active_ver = getattr(target_state, "active_version", None) if target_state else None
        active_commit = getattr(target_state, "active_commit", None) if target_state else None
        rolled_back_ver = getattr(target_state, "rolled_back_version", None) if target_state else None
        failed_ver = getattr(target_state, "failed_version", None) if target_state else None
        lkg_ver = getattr(target_state, "lkg_version", None) if target_state else None

        has_target_info = bool(active_ver or active_commit or rolled_back_ver or failed_ver)

        header_lines = [
            f"Payload Repository: {payload_dir}",
            f"Branch Ref:         {ref:<36} Tip: {tip_commit} (synchronized)",
            f"Total Artifacts:    {len(lineage)} Versions · {total_ukis} UKIs · {total_deltas} Static Deltas · {_human_size(total_size)} Total",
        ]
        if target_state and (active_ver or rolled_back_ver or getattr(target_state, "agent", None)):
            t_parts = []
            if getattr(target_state, "agent", None):
                t_parts.append(f"Agent: {target_state.agent}")
            if active_ver:
                t_parts.append(f"Active: v{active_ver}")
            elif active_commit:
                t_parts.append(f"Active: {active_commit[:10]}")
            if rolled_back_ver or failed_ver:
                t_parts.append(f"Rolled Back: v{rolled_back_ver or failed_ver}")
            if t_parts:
                header_lines[1] = f"Branch Ref:         {ref:<24} Tip: {tip_commit:<10} Target: {' · '.join(t_parts)}"

        header_widget.update("\n".join(header_lines))

        for entry in lineage:
            v_str = str(entry["version"])
            c_full = entry.get("commit", "")
            c_short = entry.get("commit_short", "")

            is_active = False
            if active_ver is not None and str(active_ver) == v_str:
                is_active = True
            elif active_commit is not None and (c_full.startswith(active_commit) or active_commit.startswith(c_short)):
                is_active = True

            is_rolled_back = False
            if rolled_back_ver is not None and str(rolled_back_ver) == v_str:
                is_rolled_back = True
            elif failed_ver is not None and str(failed_ver) == v_str:
                is_rolled_back = True

            is_lkg = False
            if lkg_ver is not None and str(lkg_ver) == v_str:
                is_lkg = True
            elif lkg_ver is None and entry.get("status") == "[LKG]":
                is_lkg = True

            if has_target_info:
                if is_active:
                    pill_text = "● Target (Active) [LKG]" if is_lkg else "● Target (Active)"
                    status_cell = Text(pill_text, style="bold green")
                    version_cell = Text(f"v{entry['version']}", style="bold green")
                elif is_rolled_back:
                    status_cell = Text("[Rolled Back]", style="bold red")
                    version_cell = Text(f"v{entry['version']}", style="red")
                elif is_lkg:
                    status_cell = Text("[LKG]", style="bold cyan")
                    version_cell = f"v{entry['version']}"
                else:
                    status_cell = Text("[Draft]", style="dim")
                    version_cell = f"v{entry['version']}"
            else:
                status_cell = entry["status"]
                version_cell = f"v{entry['version']}"

            lineage_table.add_row(
                version_cell,
                entry["commit_short"],
                entry["timestamp"],
                entry["kernel"],
                status_cell,
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
                    if cell.get("available"):
                        row_cells.append(Text(txt, style="green"))
                    elif txt == "missing":
                        row_cells.append(Text("missing", style="bold yellow"))
                    else:
                        row_cells.append(Text("-", style="dim"))
                    if cell.get("status") and cell["status"] != "-":
                        row_status = cell["status"]
                if row_status == "-" and v_from == versions[-1]:
                    row_status = "Current HEAD"
                if row_status == "Current HEAD":
                    row_cells.append(Text("Current HEAD", style="cyan"))
                elif "ready" in row_status:
                    row_cells.append(Text(row_status, style="green"))
                elif "Missing" in row_status:
                    row_cells.append(Text(row_status, style="yellow"))
                else:
                    row_cells.append(Text(row_status, style="dim"))
                matrix_table.add_row(*row_cells, key=f"row_v{v_from}")

            if saved_matrix_coord and 0 <= saved_matrix_coord.row < len(versions) and 0 <= saved_matrix_coord.column < len(matrix_table.columns):
                matrix_table.move_cursor(row=saved_matrix_coord.row, column=saved_matrix_coord.column)
            if saved_lineage_row is not None and 0 <= saved_lineage_row < len(lineage):
                lineage_table.move_cursor(row=saved_lineage_row)

        if matrix_table.has_focus:
            self._update_matrix_highlight(matrix_table.cursor_coordinate)
        else:
            self._reset_action_banner()

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

        target_state = getattr(self.app, "target_state", None)
        active_ver = getattr(target_state, "active_version", None) if target_state else None
        active_commit = getattr(target_state, "active_commit", None) if target_state else None
        rolled_back_ver = getattr(target_state, "rolled_back_version", None) if target_state else None
        failed_ver = getattr(target_state, "failed_version", None) if target_state else None

        v_str = str(entry["version"])
        c_full = entry.get("commit", "")
        c_short = entry.get("commit_short", "")

        target_desc = None
        if (active_ver is not None and str(active_ver) == v_str) or (active_commit is not None and (c_full.startswith(active_commit) or active_commit.startswith(c_short))):
            target_desc = "● Active hardware deployment"
        elif (rolled_back_ver is not None and str(rolled_back_ver) == v_str) or (failed_ver is not None and str(failed_ver) == v_str):
            target_desc = "⚠ Rolled back on hardware (boot try-counter / watchdog fallback)"
        elif target_state and getattr(target_state, "agent", None) is not None:
            target_desc = "Not deployed on target"

        lines = [
            f"Author:  {entry.get('author', 'seine imager <builder@local>'):<40} Signed By: {entry.get('signed_by', 'unsigned')}",
            f"Commit:  {entry.get('commit', '')}",
            f"Ref:     {entry.get('ref', '')} (version {entry.get('version')})",
            f"Delta:   {entry.get('deltas', '(factory baseline)')}",
            f"UKI:     {entry.get('uki_str', '(none)')} (Secure Boot signed: {entry.get('uki', {}).get('signed', False)})",
        ]
        if target_desc:
            lines.append(f"Target:  {target_desc}")
        detail_widget.update("\n".join(lines))

    def _get_matrix_cell_versions(self, coordinate=None, cell_key=None):
        versions = self._deltas_data.get("versions", [])
        if not versions:
            return None, None

        if cell_key:
            row_key = cell_key.row_key.value if cell_key.row_key else None
            col_key = cell_key.column_key.value if cell_key.column_key else None
            if row_key and str(row_key).startswith("row_v") and col_key and str(col_key).startswith("col_v"):
                return str(row_key)[len("row_v"):], str(col_key)[len("col_v"):]

        coord = coordinate
        if coord is None:
            matrix = self.query_one("#matrix", DeltaMatrix)
            coord = matrix.cursor_coordinate if matrix else None

        if coord is not None:
            row_idx, col_idx = coord.row, coord.column
            if 0 <= row_idx < len(versions) and 1 <= col_idx <= len(versions):
                return versions[row_idx], versions[col_idx - 1]

        return None, None

    def _update_matrix_highlight(self, coordinate=None, cell_key=None):
        banner = self.query_one("#actions", ActionBanner)
        if not banner:
            return
        from_ver, to_ver = self._get_matrix_cell_versions(coordinate=coordinate, cell_key=cell_key)
        if not from_ver or not to_ver:
            self._reset_action_banner()
            return

        from seine.imager.payload import version_key
        if version_key(from_ver) >= version_key(to_ver):
            self._reset_action_banner()
            return

        cell = self._deltas_data.get("matrix", {}).get((from_ver, to_ver), {})
        if cell.get("available"):
            sz = cell.get("size_human", "")
            pct = cell.get("savings_pct")
            pct_str = f", {pct}% saved" if pct is not None else ""
            banner.update(
                f"[bold]Quick Actions:[/] [green]✓[/] Static Delta v{from_ver} -> v{to_ver} Built ({sz}{pct_str})   "
                "[cyan]G[/] Regenerate   [cyan]Ctrl+B[/] Full Disk + Payload   [cyan]Ctrl+U[/] Payload Only"
            )
        else:
            banner.update(
                f"[bold]Quick Actions:[/] [cyan]G / Enter[/] Generate Delta (v{from_ver} -> v{to_ver})   "
                "[cyan]Ctrl+B[/] Full Disk + Payload   [cyan]Ctrl+U[/] Payload Only"
            )

    def _reset_action_banner(self):
        banner = self.query_one("#actions", ActionBanner)
        if banner:
            banner.update(
                "[bold]Quick Actions:[/] [cyan]G[/] Generate Delta   "
                "[cyan]Ctrl+B[/] Build Full Disk + Payload   [cyan]Ctrl+U[/] Payload Only"
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.data_table.id == "lineage" and event.row_key:
            version = str(event.row_key.value)
            self._update_commit_detail(version)
            self._reset_action_banner()

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted):
        if event.data_table.id == "lineage" and event.cell_key:
            row_key = event.cell_key.row_key
            if row_key:
                version = str(row_key.value)
                self._update_commit_detail(version)
            self._reset_action_banner()
        elif event.data_table.id == "matrix":
            self._update_matrix_highlight(coordinate=event.coordinate, cell_key=event.cell_key)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected):
        if event.data_table.id != "matrix":
            return
        from_ver, to_ver = self._get_matrix_cell_versions(coordinate=event.coordinate, cell_key=event.cell_key)
        if not from_ver or not to_ver:
            return
        if from_ver == to_ver:
            self.app.say("delta generate: source and target versions must differ", warning=True)
            return
        from seine.imager.payload import version_key
        if version_key(from_ver) > version_key(to_ver):
            self.app.say("delta generate: target version must be newer than source version", warning=True)
            return

        cell = self._deltas_data.get("matrix", {}).get((from_ver, to_ver), {})
        if cell.get("available"):
            sz = cell.get("size_human", "")
            pct = cell.get("savings_pct")
            pct_str = f", {pct}% saved" if pct is not None else ""
            self.app.say(f"static delta v{from_ver} -> v{to_ver} already available ({sz}{pct_str})")
            return

        self._trigger_delta_generation(from_ver, to_ver)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id == "lineage":
            self.action_inspect_selection()

    def action_toggle_focus(self):
        matrix = self.query_one("#matrix", DeltaMatrix)
        lineage = self.query_one("#lineage", LineageTable)
        if matrix.has_focus:
            lineage.focus()
            self._reset_action_banner()
        else:
            matrix.focus()
            self._update_matrix_highlight()

    def _trigger_delta_generation(self, from_ver, to_ver):
        cmd = f"/delta generate --from={from_ver} --to={to_ver}"
        self.app.say(f"generating static delta v{from_ver} -> v{to_ver}...")
        from seine.tui import commands
        self.app.run_worker(lambda: commands.dispatch(self.app, cmd), thread=True)

    def action_generate_delta(self):
        versions = self._deltas_data.get("versions", [])
        if len(versions) < 2:
            self.app.say("delta generate: needs at least 2 versions in payload repository", warning=True)
            return

        matrix = self.query_one("#matrix", DeltaMatrix)
        lineage = self.query_one("#lineage", LineageTable)

        from_ver = None
        to_ver = None

        if matrix.has_focus:
            from_ver, to_ver = self._get_matrix_cell_versions()
            if not from_ver or not to_ver:
                self.app.say("delta generate: select a delta cell in the matrix", warning=True)
                return
            if from_ver == to_ver:
                self.app.say("delta generate: source and target versions must differ", warning=True)
                return
            from seine.imager.payload import version_key
            if version_key(from_ver) > version_key(to_ver):
                self.app.say("delta generate: target version must be newer than source version", warning=True)
                return
        else:
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

        if not from_ver or not to_ver or from_ver == to_ver:
            self.app.say("delta generate: source and target versions must differ", warning=True)
            return

        self._trigger_delta_generation(from_ver, to_ver)

    def action_inspect_selection(self):
        matrix = self.query_one("#matrix", DeltaMatrix)
        if matrix.has_focus:
            from_ver, to_ver = self._get_matrix_cell_versions()
            if from_ver and to_ver:
                from seine.imager.payload import version_key
                if version_key(from_ver) < version_key(to_ver):
                    cell = self._deltas_data.get("matrix", {}).get((from_ver, to_ver), {})
                    if not cell.get("available"):
                        self._trigger_delta_generation(from_ver, to_ver)
                        return
                    sz = cell.get("size_human", "")
                    pct = cell.get("savings_pct")
                    pct_str = f", {pct}% saved" if pct is not None else ""
                    self.app.say(f"static delta v{from_ver} -> v{to_ver} already available ({sz}{pct_str})")
                    return
        if self._selected_version:
            self.app.say(f"selected version v{self._selected_version}")

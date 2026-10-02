# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, OptionList, Static
from textual.widgets.option_list import Option


class ProjectPicker(ModalScreen):
    """Pick a project; dismisses with (name, make_default), or None when cancelled."""

    DEFAULT_CSS = """
    ProjectPicker { align: center middle; }
    #projpane {
        width: 50; height: auto;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #projtitle { text-style: bold; padding-bottom: 1; }
    #projlist, #projlist:focus { border: none; height: auto; max-height: 12; padding: 0; }
    #projpane Checkbox, #projpane Checkbox:focus {
        border: none; height: 1; padding: 0; margin-top: 1;
    }
    #projhint { color: $text-muted; padding-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, projects, current=None, offer_default=False):
        super().__init__()
        self._projects = projects
        self._current = current
        self._offer_default = offer_default

    def compose(self):
        names = sorted(self._projects)
        with Vertical(id="projpane"):
            yield Static("Project for remote builds", id="projtitle")
            yield OptionList(
                *[Option(f"{name}  ({self._projects[name]})", id=name) for name in names],
                id="projlist")
            yield Checkbox("Make this my default", value=self._offer_default, id="projdefault")
            yield Static("Enter select · Tab to the box, Space toggles · Esc cancel", id="projhint")

    def on_mount(self):
        names = sorted(self._projects)
        picker = self.query_one(OptionList)
        picker.highlighted = names.index(self._current) if self._current in names else 0
        picker.focus()

    def on_option_list_option_selected(self, event):
        self.dismiss((event.option.id, self.query_one(Checkbox).value))

    def action_cancel(self):
        self.dismiss(None)

#!/usr/bin/env python3

import asyncio
import avocado
import contextlib
import os
import sys
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.tui import console
from seine.tui import target
from tests.native_image import native_image

NATIVE_IMAGE = native_image()

def _run(scenario):
    asyncio.run(scenario())

# Same '_tui_required' guard tests/tui/tui.py uses for anything that
# needs the 'tui' extra (textual). Kept self-contained here rather than
# imported, matching test-file isolation elsewhere in tests/tui/.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# ConsoleAdapter needs pyte (setup.py's 'tui' extra, bundled with
# textual/rich) -- cancel rather than error if it truly isn't there.
@contextlib.contextmanager
def _pyte_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("pyte is not installed: %s" % e)

# Stand-in for mtda.client.Client needed by ConsolePaneRawMode to record
# console_send calls without a real mtda service.
class FakeClient:
    def __init__(self, host=None):
        self.host = host
        self.calls = []
        self.started = False
        self.stopped = False
        self.agent = types.SimpleNamespace(remote=None)
        self.console_remote_calls = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def console_remote(self, host, screen):
        self.console_remote_calls.append((host, screen))

    def session(self):
        return "fake-session"

    def console_send(self, data, raw=False):
        self.calls.append(("console_send", data, raw))

class Console(avocado.Test):
    def setUp(self):
        with _pyte_required(self):
            import pyte
        self.pyte = pyte
        self.app = types.SimpleNamespace(target_state=target.TargetState())
        self.app.said_refresh = []
        self.app.refresh_indicators = lambda: self.app.said_refresh.append(1)
        self.app.crossed_threads = []
        def call_from_thread(fn, *a, **kw):
            self.app.crossed_threads.append(1)
            return fn(*a, **kw)
        self.app.call_from_thread = call_from_thread
        self.adapter = console.ConsoleAdapter(self.app)

    def test_print_feeds_pyte_and_decodes_bytes(self):
        self.adapter.print(b"hello\r\n")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "hello")

    def test_print_resets_column_on_bare_linefeed(self):
        self.adapter.print(b"first\nsecond\n")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "first")
        self.assertEqual(self.adapter.screen.display[1].rstrip(), "second")

    # dirty is a plain flag, not a push -- TargetScreen's own tick polls
    # it at a bounded rate instead.
    def test_print_sets_dirty_without_touching_the_thread(self):
        self.assertFalse(self.adapter.dirty)
        self.adapter.print(b"x")
        self.assertTrue(self.adapter.dirty)
        self.assertEqual(self.app.crossed_threads, [])

    def test_on_event_updates_target_state_and_refreshes_indicators(self):
        self.adapter.on_event("POWER ON")
        self.assertEqual(self.app.target_state.power, "ON")
        self.assertEqual(self.app.said_refresh, [1])

    def test_render_console_returns_plain_text_for_uncoloured_output(self):
        self.adapter.print("plain text\r\n")
        rendered = console.render_console(self.adapter.screen)
        self.assertIn("plain text", rendered.plain)

    def test_render_console_max_lines_caps_from_the_top(self):
        for i in range(5):
            self.adapter.print("line %d\r\n" % i)
        rendered = console.render_console(self.adapter.screen, max_lines=2)
        lines = rendered.plain.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("line 0", lines[0])

    def test_render_console_tail_shows_newest_output(self):
        for i in range(5):
            self.adapter.print("line %d\r\n" % i)
        rendered = console.render_console(self.adapter.screen, max_lines=2, tail=True)
        lines = rendered.plain.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("line 4", lines[1])

    def test_render_console_carries_colour_as_a_rich_style(self):
        self.adapter.print("\x1b[31mred\x1b[0m\r\n")
        rendered = console.render_console(self.adapter.screen)
        # First three characters ('red') should carry a red foreground.
        spans = [s for s in rendered.spans if s.start == 0]
        self.assertTrue(any(s.style.color is not None and
                            "red" in str(s.style.color) for s in spans))

    # A real terminal clips when its display is narrower than its own
    # width, it doesn't reflow -- rewrapping here would scramble the
    # fixed-column grid a BIOS/serial console assumes.
    def test_render_console_does_not_rewrap(self):
        rendered = console.render_console(self.adapter.screen)
        self.assertTrue(rendered.no_wrap)
        self.assertEqual(rendered.overflow, "crop")

    # See console.py's own CONSOLE_LINES comment: this firmware's own
    # boot-menu screen addresses absolute rows past 31.
    def test_console_screen_is_80_columns_wide_and_tall_enough_for_this_firmware(self):
        self.assertEqual(self.adapter.screen.columns, 80)
        self.assertGreater(self.adapter.screen.lines, 31)

    # Isolates the scroll-drift mechanism CONSOLE_LINES=40 fixes: an
    # absolute-position redraw, plain scrolling log lines, then the
    # identical redraw again. On a too-short screen the log lines
    # scroll the first redraw off its row while the second still lands
    # at the same absolute position -- two writes, two final positions,
    # looking like duplicated text.
    def test_a_too_short_screen_would_duplicate_via_scroll_drift(self):
        import pyte
        short_screen = pyte.Screen(80, 25)
        short_stream = pyte.Stream(short_screen)
        short_stream.feed("\x1b[10;4HDevice Manager")
        for i in range(20):
            short_stream.feed("log line %d\r\n" % i)
        short_stream.feed("\x1b[10;4HDevice Manager")
        occurrences = [l for l in short_screen.display if "Device Manager" in l]
        self.assertEqual(len(occurrences), 2, "expected the drift bug on a 25-line screen")

        self.adapter.print("\x1b[10;4HDevice Manager")
        for i in range(20):
            self.adapter.print("log line %d\r\n" % i)
        self.adapter.print("\x1b[10;4HDevice Manager")
        occurrences = [l for l in self.adapter.screen.display if "Device Manager" in l]
        self.assertEqual(len(occurrences), 1)

    # See console.py's _UNSUPPORTED_CSI: 'CSI = <n> <letter>' is a
    # legacy BIOS/DOS-ANSI mode-set convention pyte's parser leaks as
    # literal text; stripped since '=' has no safe recovery.
    def test_unsupported_csi_equals_sequence_is_stripped(self):
        self.adapter.print("before\x1b[=3hafter")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "beforeafter")

    def test_unsupported_csi_equals_sequence_split_across_chunks(self):
        self.adapter.print("before\x1b[=3")
        self.adapter.print("hafter")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "beforeafter")

    # Recovered, not just stripped: ':' is ECMA-48's own sub-parameter
    # separator, so deleting it reconstructs the 'CSI 25;0H' cursor
    # move the firmware meant, moving the cursor there instead of
    # leaving 'after' wherever it happened to land.
    def test_unsupported_csi_stray_colon_sequence_recovers_cursor_position(self):
        self.adapter.print("before\x1b[25;:0Hafter")
        self.assertNotIn("0H", "\n".join(self.adapter.screen.display))
        self.assertEqual(self.adapter.screen.display[24][:5], "after")

    def test_unsupported_csi_stray_colon_sequence_split_across_chunks(self):
        self.adapter.print("before\x1b[25;:")
        self.adapter.print("0Hafter")
        self.assertNotIn("0H", "\n".join(self.adapter.screen.display))
        self.assertEqual(self.adapter.screen.display[24][:5], "after")

    # A normal, pyte-supported private-mode sequence ('?') and an
    # ordinary multi-parameter cursor-position sequence must both keep
    # working -- this fix only targets a genuinely unexpected character
    # in the parameter area, not '?' or the usual digits/';'.
    def test_supported_dec_private_mode_is_unaffected(self):
        self.adapter.print("before\x1b[?25hafter")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "beforeafter")

    def test_ordinary_cursor_position_is_unaffected(self):
        self.adapter.print("before\x1b[08;71Hafter")
        # row 8, column 71 (1-indexed) -> row 7, column 70 zero-indexed.
        self.assertEqual(self.adapter.screen.display[7][70:75], "after")

    # Decoding each chunk independently corrupts a multi-byte character
    # split across a chunk boundary into U+FFFD on both sides of the
    # cut; pyte.ByteStream's own incremental decoder holds the partial
    # bytes across feed() calls instead.
    def test_multibyte_utf8_split_across_chunks_decodes_correctly(self):
        data = "─".encode("utf-8")
        self.adapter.print(data[:1])
        self.adapter.print(data[1:])
        self.assertEqual(self.adapter.screen.display[0][:1], "─")
        self.assertNotIn("\ufffd", self.adapter.screen.display[0])

    # Real captured BIOS/UEFI output (console_dump(), two captures) --
    # a regression fixture replaying all three bugs above together.
    def test_real_bios_capture_has_no_leaked_csi_or_replacement_chars(self):
        boot = ('\x1b[2J\x1b[01;01H\x1b[=3h\x1b[2J\x1b[01;01H\x1b[2J\x1b[01;01H'
               '\x1b[=3h\x1b[2J\x1b[01;01H\x1b[2J\x1b[01;01H\x1b[=3h\x1b[2J'
               '\x1b[01;01HBdsDxe: loading Boot0002 "UEFI QEMU HARDDISK '
               'QM00003 " from PciRoot(0x0)/Pci(0x1,0x1)/Ata(Secondary,'
               'Master,0x0)\n\x1b[2J\x1b[01;01H\x1b[0m\x1b[36m\x1b[40m'
               'EFI Boot Guard v0.22\n')
        menu = '\x1b[29;03H↑↓=Move Highlight              '
        # From the second capture (the boot-menu screen, taken after
        # navigating with the arrow keys) -- the stray-':' shape, real,
        # not synthesised.
        highlight = '\x1b[25;:0H\x1b[08;71H'
        # Fed in small, arbitrarily-cut chunks -- deliberately not
        # aligned to escape-sequence or character boundaries, the same
        # way mtda's own reader thread delivers hundreds of small,
        # arbitrarily-sized chunks a second.
        raw = (boot + menu + highlight).encode("utf-8")
        chunk = 7
        for i in range(0, len(raw), chunk):
            self.adapter.print(raw[i:i + chunk])
        text = "\n".join(self.adapter.screen.display)
        self.assertNotIn("=3h", text)
        self.assertNotIn("0H", text)
        self.assertNotIn("\ufffd", text)
        self.assertIn("Move Highlight", text)

# _key_to_bytes() is a pure function -- a plain SimpleNamespace stands
# in for textual.events.Key, which only ever needs .key/.character read.
class RawKeyMapping(avocado.Test):
    def setUp(self):
        with _tui_required(self):
            from seine.tui.target_screen import _key_to_bytes
        self._key_to_bytes = _key_to_bytes

    def _event(self, key, character=None):
        return types.SimpleNamespace(key=key, character=character)

    def test_escape_is_the_bios_key(self):
        self.assertEqual(self._key_to_bytes(self._event("escape")), b"\x1b")

    def test_named_keys_map_to_their_ansi_sequence(self):
        self.assertEqual(self._key_to_bytes(self._event("up")), b"\x1b[A")
        self.assertEqual(self._key_to_bytes(self._event("f5")), b"\x1b[15~")

    def test_ctrl_letter_maps_to_its_control_code(self):
        # Ctrl-C is 0x03, same as a real terminal.
        self.assertEqual(self._key_to_bytes(self._event("ctrl+c")), bytes([3]))

    def test_a_plain_character_is_encoded_as_is(self):
        self.assertEqual(self._key_to_bytes(self._event("a", character="a")), b"a")

    # Tab keeps switching panes app-wide -- raw mode does not steal it.
    def test_tab_is_not_mapped(self):
        self.assertIsNone(self._key_to_bytes(self._event("tab")))

    def test_an_unmapped_control_key_with_no_character_is_none(self):
        self.assertIsNone(self._key_to_bytes(self._event("shift+tab")))

class ConsolePaneRawMode(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
            from seine.tui.target_screen import ConsolePane
        self.SeineApp = SeineApp
        self.ConsolePane = ConsolePane

        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        mtda_pkg = types.ModuleType("mtda")
        mtda_client_mod = types.SimpleNamespace(Client=FakeClient)
        mtda_pkg.client = mtda_client_mod
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_client_mod

        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        # See TargetScreenIntegration.setUp()'s own comment -- same
        # real-SeineApp, same reason.
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")

    def tearDown(self):
        target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client
        os.environ.pop("SEINE_HISTORY_FILE", None)

    # No confirmation: focusing the pane is already deliberate, and
    # confirming every keystroke individually would be unusable.
    def test_focusing_the_pane_enables_raw_mode_immediately(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                self.assertTrue(pane.raw_mode)
        _run(scenario)

    # A transient reminder, not a permanent banner -- gone again after
    # the couple of seconds it's given.
    def test_focusing_shows_a_transient_warning_then_clears_it(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                status = app.screen.query_one("#status")
                self.assertIn("raw keystrokes", status.renderable)
                self.assertTrue(status.has_class("warning"))
                await asyncio.sleep(2.6)
                self.assertEqual(status.renderable, "")
                self.assertFalse(status.has_class("warning"))
        _run(scenario)

    def test_escape_reaches_the_target_once_raw_mode_is_on(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                await pilot.press("escape")
                for _ in range(50):
                    if ("console_send", b"\x1b", True) in app._target_client.calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn(("console_send", b"\x1b", True), app._target_client.calls)
        _run(scenario)

    def test_blurring_turns_raw_mode_off(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                app.screen.query_one("#prompt").focus()
                await pilot.pause()
                self.assertFalse(pane.raw_mode)
        _run(scenario)

#!/usr/bin/env python3

import asyncio
import avocado
import contextlib
import os
import sys
import threading

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from tests.native_image import native_image

NATIVE_IMAGE = native_image()

def _run(scenario):
    asyncio.run(scenario())

# Same '_tui_required' guard tests/tui/tui.py uses for anything that
# needs the 'tui' extra (textual). Kept self-contained here rather than
# imported from tui.py, same reason tui.py gives for not sharing helpers
# across test files.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# Two independently-packaged groups sharing one disk -- the same spec
# shape as tests/image/multiconfig.py's TwoSourcesOnOneDisk, minus the
# playbook/packages content, since nothing here actually builds
# (SpecTree only ever reads a parsed spec).
def _write_two_group_spec(workdir):
    def _group(name):
        path = os.path.join(workdir, "%s.yaml" % name)
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: amd64\n"
                "image:\n"
                "    filename: %s.img\n"
                "    table: gpt\n"
                "    partitions:\n"
                "        - label: rootfs\n"
                "          where: /\n"
                "          size: 256MiB\n"
                % name)
        return path
    main = _group("main")
    recovery = _group("recovery")
    outer = os.path.join(workdir, "outer.yaml")
    with open(outer, "w") as f:
        f.write(
            "distribution:\n"
            "    release: trixie\n"
            "    architecture: amd64\n"
            "multiconfig:\n"
            "    main:\n"
            "        - %s\n"
            "    recovery:\n"
            "        - %s\n"
            "image:\n"
            "    filename: disk.img\n"
            "    table: gpt\n"
            "    partitions:\n"
            "        - label: main-root\n"
            "          source: main\n"
            "          where: /\n"
            "          size: 256MiB\n"
            "        - label: recovery-root\n"
            "          source: recovery\n"
            "          where: /\n"
            "          size: 256MiB\n"
            % (main, recovery))
    return outer

def _child(node, label):
    for child in node.children:
        if child.data == label:
            return child
    return None

# Vendor-only (no 'image:'): parses fine on its own, same shape as
# tests/tui/tui.py's _write_vendor_only_spec, kept self-contained here
# for the same reason that file gives for not sharing test helpers.
def _write_release_spec(workdir, name, release):
    path = os.path.join(workdir, "%s.yaml" % name)
    with open(path, "w") as f:
        f.write(
            "distribution:\n"
            "    release: %s\n"
            "    architecture: amd64\n"
            "    uri: http://example.com/debian\n"
            "vendor:\n"
            "    - name: openssl\n" % release)
    return path

# A shared feeds file (debian-feeds.yaml's own shape): two releases'
# feeds in one list, only one of which is this spec's own.
def _write_feeds_spec(workdir):
    path = os.path.join(workdir, "feeds.yaml")
    with open(path, "w") as f:
        f.write(
            "distribution:\n"
            "    release: bookworm\n"
            "    architecture: amd64\n"
            "    uri: http://example.com/debian\n"
            "    feeds:\n"
            "        - suite: bookworm\n"
            "          release: bookworm\n"
            "        - suite: trixie\n"
            "          release: trixie\n"
            "vendor:\n"
            "    - name: openssl\n")
    return path

# Descends 'labels' from 'node', asserting each step actually matched --
# a broken chain fails at the label that went missing, not with an
# AttributeError three calls later.
def _descend(test, node, *labels):
    for label in labels:
        child = _child(node, label)
        test.assertIsNotNone(child, "no '%s' branch under '%s'" % (label, node.data))
        node = child
    return node

# What SpecTree.load() does with a spec's own 'multiconfig:' key -- no
# running App needed, same style as tests/tui/tui.py's
# ActiveSpecification/Rendering classes.
class NestedGroups(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.spectree import SpecTree
        self.Context = Context
        self.SpecTree = SpecTree
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def _tree(self, files):
        context = self.Context()
        context.use(files)
        tree = self.SpecTree()
        tree.load(context)
        return tree

    def test_groups_render_as_nested_branches_with_own_partitions(self):
        outer = _write_two_group_spec(self.workdir)
        tree = self._tree([outer])
        root = tree.root.children[0]
        for name in ("main", "recovery"):
            group = _descend(self, root, "multiconfig", name)
            root_part = _descend(self, group, "image", "partitions", "rootfs")
            self.assertIsNotNone(_child(root_part, "where: /"),
                                 "%s's own 'where: /' leaf missing" % name)

    # source:/where: on the outer disk's own partitions -- unaffected by
    # nesting each group's own spec under 'multiconfig', still exactly
    # where the generic walker always put them.
    def test_outer_partitions_show_which_group_they_source_from(self):
        outer = _write_two_group_spec(self.workdir)
        tree = self._tree([outer])
        root = tree.root.children[0]
        main_root = _descend(self, root, "image", "partitions", "main-root")
        self.assertIsNotNone(_child(main_root, "source: main"))
        self.assertIsNotNone(_child(main_root, "where: /"))

    def test_a_single_build_spec_is_unaffected(self):
        tree = self._tree([NATIVE_IMAGE])
        root = tree.root.children[0]
        self.assertIsNone(_child(root, "multiconfig"))

# path_for()/node_for(): how OverviewScreen's right pane keeps a
# selection across load() rebuilding every node from scratch -- no
# running App needed, same style as NestedGroups above.
class PathResolution(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.spectree import SpecTree
        self.Context = Context
        self.SpecTree = SpecTree
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def _tree(self, files):
        context = self.Context()
        context.use(files)
        tree = self.SpecTree()
        tree.load(context)
        return tree

    def test_path_for_and_node_for_round_trip(self):
        tree = self._tree([NATIVE_IMAGE])
        root = tree.root.children[0]
        distribution = _descend(self, root, "distribution")
        release_leaf = next(
            (c for c in distribution.children if c.data.startswith("release:")), None)
        self.assertIsNotNone(release_leaf, "no 'release:' leaf under distribution")
        path = tree.path_for(release_leaf)
        self.assertEqual(tree.node_for(path), release_leaf)

    # A stale path finds no node after reload, not a wrong one
    # with the same prefix.
    def test_stale_path_resolves_to_none_after_reload(self):
        spec_a = _write_release_spec(self.workdir, "a", "bookworm")
        spec_b = _write_release_spec(self.workdir, "b", "trixie")
        tree = self._tree([spec_a])
        root = tree.root.children[0]
        distribution = _descend(self, root, "distribution")
        release_leaf = _child(distribution, "release: bookworm")
        self.assertIsNotNone(release_leaf)
        path = tree.path_for(release_leaf)
        context = self.Context()
        context.use([spec_b])
        tree.load(context)
        self.assertIsNone(tree.node_for(path))

# A feed for another release (release_feeds() in seine/utils.py) is
# still shown, but marked as not used by this build -- no running App
# needed, same style as NestedGroups above.
class InapplicableFeeds(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.spectree import SpecTree, INAPPLICABLE_STYLE
        self.Context = Context
        self.SpecTree = SpecTree
        self.INAPPLICABLE_STYLE = INAPPLICABLE_STYLE
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def _tree(self, files):
        context = self.Context()
        context.use(files)
        tree = self.SpecTree()
        tree.load(context)
        return tree

    def test_a_feed_of_another_release_is_struck_through(self):
        spec = _write_feeds_spec(self.workdir)
        tree = self._tree([spec])
        root = tree.root.children[0]
        feeds = _descend(self, root, "distribution", "feeds")
        trixie = _child(feeds, "trixie")
        self.assertIsNotNone(trixie)
        self.assertEqual(trixie.label.style, self.INAPPLICABLE_STYLE)

    def test_this_build_s_own_feed_is_unaffected(self):
        spec = _write_feeds_spec(self.workdir)
        tree = self._tree([spec])
        root = tree.root.children[0]
        feeds = _descend(self, root, "distribution", "feeds")
        bookworm = _child(feeds, "bookworm")
        self.assertIsNotNone(bookworm)
        self.assertNotEqual(bookworm.label.style, self.INAPPLICABLE_STYLE)

# Playbooks sit under their wave branch, waves and plays in the order
# they run: 'late' is listed first but must come after 'early'.
class PlaybookWaves(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.spectree import SpecTree
        self.Context = Context
        self.SpecTree = SpecTree
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def _tree(self, plays):
        path = os.path.join(self.workdir, "waves.yaml")
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: amd64\n"
                "playbook:\n" + plays)
        context = self.Context()
        context.use([path])
        tree = self.SpecTree()
        tree.load(context)
        return tree

    def test_plays_are_grouped_by_wave_in_execution_order(self):
        tree = self._tree(
            "    - name: b\n      wave: late\n      after: early\n      tasks: []\n"
            "    - name: a2\n      wave: early\n      priority: 600\n      tasks: []\n"
            "    - name: a1\n      wave: early\n      tasks: []\n")
        playbook = _descend(self, tree.root.children[0], "playbook")
        self.assertEqual([n.data for n in playbook.children], ["early", "late"])
        self.assertEqual([n.data for n in playbook.children[0].children], ["a1", "a2"])
        self.assertEqual(tree.play_path("b"), ["playbook", "late", "b"])

    def test_a_single_wave_keeps_the_flat_list(self):
        tree = self._tree("    - name: a\n      tasks: []\n")
        playbook = _descend(self, tree.root.children[0], "playbook")
        self.assertEqual([n.data for n in playbook.children], ["a"])
        self.assertEqual(tree.play_path("a"), ["playbook", "a"])

# Full app, real Textual event loop -- highlight_active()/branch_for()
# only prove they route a namespaced task name to the right subtree when
# a build is actually running and ticking the tree, same as
# tests/tui/tui.py's own spectree-highlighting test.
class HighlightsNamespacedGroupTasks(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.image import Image
            from seine.tui.app import OverviewScreen, SeineApp
            from seine.tui.spectree import SpecTree
        self.Image = Image
        self.OverviewScreen = OverviewScreen
        self.SeineApp = SeineApp
        self.SpecTree = SpecTree
        self.real_build = Image.build
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")

    def tearDown(self):
        from seine import tasks
        self.Image.build = self.real_build
        tasks._interrupted.clear()
        os.environ.pop("SEINE_HISTORY_FILE", None)

    def test_a_groups_own_task_highlights_under_its_own_branch_only(self):
        from seine import tasks

        proceed = threading.Event()

        def gated_build(image, reporter=None):
            for step in tasks.ordered(image.tasks()):
                reporter.started(step.name)
                proceed.wait()
                proceed.clear()
                reporter.finished(step.name, failed=False)
        self.Image.build = gated_build

        outer = _write_two_group_spec(self.workdir)

        async def scenario():
            app = self.SeineApp(files=[outer])
            async with app.run_test() as pilot:
                # In a finally: run_test()'s teardown joins the worker
                # thread, and a failed assertion would otherwise leave
                # it blocked on proceed.wait() forever.
                try:
                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "/build"
                    await pilot.press("enter")
                    await pilot.pause()
                    for _ in range(400):
                        if app.build_state.current == "recovery:rootfs":
                            break
                        proceed.set()
                        await asyncio.sleep(0.01)
                    self.assertEqual(app.build_state.current, "recovery:rootfs")

                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "/overview"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, self.OverviewScreen)
                    tree = app.screen.query_one(self.SpecTree)
                    for _ in range(100):
                        if tree.active_keys():
                            break
                        await asyncio.sleep(0.02)
                        await pilot.pause()
                    self.assertTrue(tree.active_keys())

                    root = tree.root.children[0]
                    recovery = _descend(self, root, "multiconfig", "recovery")
                    main = _descend(self, root, "multiconfig", "main")
                    leaf = tree.leaf("recovery:rootfs")
                    self.assertIsNotNone(leaf, "recovery:rootfs never became active")
                    ancestors = set()
                    node = leaf
                    while node is not None:
                        ancestors.add(node)
                        node = node.parent
                    self.assertIn(recovery, ancestors)
                    self.assertNotIn(main, ancestors)
                finally:
                    for _ in range(400):
                        if not app.build_state.running:
                            break
                        proceed.set()
                        await asyncio.sleep(0.01)
                self.assertTrue(app.build_state.done)
        _run(scenario)

class ScreenRatioTest(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import (
                AnalyzeScreen, CacheScreen, DoctorScreen,
                FilesystemScreen, OverviewScreen, PackagesScreen,
                PlanScreen, SeineApp
            )
        self.SeineApp = SeineApp
        self.OverviewScreen = OverviewScreen
        self.PlanScreen = PlanScreen
        self.FilesystemScreen = FilesystemScreen
        self.PackagesScreen = PackagesScreen
        self.AnalyzeScreen = AnalyzeScreen
        self.CacheScreen = CacheScreen
        self.DoctorScreen = DoctorScreen

    def test_screen_pane_ratios(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test(size=(100, 30)) as pilot:
                # Overview has 2:1 ratio (spectree > cmd)
                app.switch_screen(self.OverviewScreen())
                await pilot.pause()
                tree_w = app.screen.query_one("#spectree").size.width
                cmd_w = app.screen.query_one("#cmd").size.width
                self.assertGreater(tree_w, cmd_w)

                # 50/50 screens have equal widths (tree_w == cmd_w within 1 cell)
                screens_50_50 = [
                    self.PlanScreen,
                    self.FilesystemScreen,
                    self.PackagesScreen,
                    self.AnalyzeScreen,
                    self.CacheScreen,
                    self.DoctorScreen,
                ]
                for screen_cls in screens_50_50:
                    app.switch_screen(screen_cls())
                    await pilot.pause()
                    tw = app.screen.query_one("#spectree").size.width
                    cw = app.screen.query_one("#cmd").size.width
                    self.assertAlmostEqual(tw, cw, delta=1,
                                           msg=f"{screen_cls.__name__} spectree ({tw}) != cmd ({cw})")
        _run(scenario)

if __name__ == "__main__":
    avocado.main()

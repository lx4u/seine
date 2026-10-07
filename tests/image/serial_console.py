#!/usr/bin/env python3

import avocado
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

import qemu_guest

# A plain shell stands in for the guest's serial console.
class ConsoleDriver(avocado.Test):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="seine-test-console-")
        self.console_log = os.path.join(self.dir, "console.log")
        self.console = qemu_guest.Console(["sh"], self.console_log)

    def tearDown(self):
        self.console.close()
        shutil.rmtree(self.dir)

    def test_a_command_gives_its_output_and_status(self):
        self.assertEqual(self.console.run("echo one; echo two"), (0, "one\ntwo"))

    def test_the_exit_status_is_returned(self):
        self.assertEqual(self.console.run("echo bad; false"), (1, "bad"))

    def test_the_state_of_the_shell_is_kept_between_commands(self):
        self.console.run("X=kept")
        self.assertEqual(self.console.run("echo $X"), (0, "kept"))

    def test_output_without_a_final_newline_is_kept(self):
        self.assertEqual(self.console.run("printf abc"), (0, "abc"))

    def test_a_busy_shell_is_waited_for(self):
        self.console.send("sleep 2\n")
        self.console.wait_shell()
        self.assertEqual(self.console.run("echo up"), (0, "up"))

    def test_only_enter_is_sent_until_the_prompt_shows(self):
        self.console.close()
        self.console = qemu_guest.Console(
            ["sh", "-c", "head -c 1 >/dev/null; printf 'root@h:~# '; exec sh"],
            self.console_log)
        self.console.wait_shell(prompt=qemu_guest.PROMPT)
        self.assertEqual(self.console.run("echo up"), (0, "up"))

    def test_expect_returns_the_match_and_moves_on(self):
        self.console.send("echo first; echo second\n")
        self.assertEqual(self.console.expect(r"fir\w+").group(0), "first")
        self.assertEqual(self.console.expect(r"sec\w+").group(0), "second")

    def test_a_missing_pattern_times_out_with_the_output_in_the_message(self):
        self.console.send("echo hello\n")
        with self.assertRaisesRegex(TimeoutError, "hello"):
            self.console.expect("never", timeout=2)

    def test_terminal_escapes_are_not_part_of_the_text(self):
        self.console.send("printf '\\033[?2004lREADY\\n'\n")
        self.console.expect(r"(?m)^READY$")

    def test_the_output_goes_to_the_log(self):
        self.console.run("echo logged")
        self.assertIn("logged", open(self.console_log).read())

class QemuCommand(avocado.Test):
    def test_the_plain_firmware_has_no_smm(self):
        argv = qemu_guest.qemu_argv("d.qcow2", "vars.fd")
        self.assertIn("q35", argv)
        self.assertTrue(any(a.endswith("OVMF_CODE_4M.fd") for a in argv))
        self.assertNotIn("-global", argv)

    def test_secure_boot_needs_smm_and_a_locked_flash(self):
        argv = qemu_guest.qemu_argv("d.qcow2", "vars.fd", secure_boot=True)
        self.assertIn("q35,smm=on", argv)
        self.assertTrue(any(a.endswith("OVMF_CODE_4M.secboot.fd") for a in argv))
        self.assertIn("driver=cfi.pflash01,property=secure,value=on", argv)

    def test_a_share_is_given_to_the_guest_as_9p(self):
        argv = qemu_guest.qemu_argv("d.qcow2", "vars.fd", share="/x")
        self.assertIn("local,path=/x,mount_tag=host,security_model=none,id=host", argv)

    def test_watchdog_adds_i6300esb_and_reset_action(self):
        argv = qemu_guest.qemu_argv("d.qcow2", "vars.fd", watchdog=True)
        self.assertIn("-device", argv)
        idx = argv.index("-device")
        self.assertEqual(argv[idx + 1], "i6300esb")
        self.assertIn("-watchdog-action", argv)
        idx_act = argv.index("-watchdog-action")
        self.assertEqual(argv[idx_act + 1], "reset")

    def test_watchdog_named_device_on_arm64(self):
        argv = qemu_guest.qemu_argv("d.qcow2", "vars.fd", watchdog="sbsa-gwdt", arch="arm64")
        self.assertIn("qemu-system-aarch64", argv)
        self.assertIn("virt", argv)
        self.assertIn("-device", argv)
        idx = argv.index("-device")
        self.assertEqual(argv[idx + 1], "sbsa-gwdt")
        self.assertIn("-watchdog-action", argv)
        idx_act = argv.index("-watchdog-action")
        self.assertEqual(argv[idx_act + 1], "reset")

    def test_arm64_plain_firmware_and_virt_machine(self):
        argv = qemu_guest.qemu_argv("d.qcow2", "vars.fd", arch="arm64")
        self.assertIn("qemu-system-aarch64", argv)
        self.assertIn("virt", argv)
        self.assertTrue(any(a.endswith("AAVMF_CODE.fd") for a in argv))

    def test_arm64_secure_boot_needs_virt_secure(self):
        argv = qemu_guest.qemu_argv("d.qcow2", "vars.fd", secure_boot=True, arch="arm64")
        self.assertIn("virt,secure=on", argv)
        self.assertTrue(any(a.endswith("AAVMF_CODE.secboot.fd") for a in argv))

class Overlay(avocado.Test):
    def setUp(self):
        if shutil.which("qemu-img") is None:
            self.cancel("qemu-img is needed")
        self.dir = tempfile.mkdtemp(prefix="seine-test-overlay-")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_the_base_stays_as_it_was(self):
        base = os.path.join(self.dir, "base.img")
        with open(base, "wb") as f:
            f.write(b"x" * 65536)
        qemu_guest.overlay(base, os.path.join(self.dir, "o.qcow2"))
        info = subprocess.run(
            ["qemu-img", "info", os.path.join(self.dir, "o.qcow2")],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("backing file: %s" % base, info)
        self.assertEqual(os.path.getsize(base), 65536)

#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

import qemu_guest

PROMPT = "\x1b]3008;start=1;type=shell\x1b\\root@host:~# "

# A console reading what a program prints, here a 'printf'.
class ConsoleEscapes(avocado.Test):
    def read(self, *chunks):
        script = "import sys,time\nfor c in %r:\n sys.stdout.buffer.write(c.encode()); sys.stdout.flush(); time.sleep(0.2)\n" % (chunks,)
        console = qemu_guest.Console(
            [sys.executable, "-c", script], os.path.join(self.workdir, "log"))
        try:
            console.process.wait()
            console.expect("END", 5)
            return console.text
        finally:
            console.close()

    def test_a_marked_prompt_is_plain_text(self):
        text = self.read(PROMPT, "READY\n\x1b]3008;end=1\x1b\\END")
        self.assertIn("root@host:~# READY\n", text)
        self.assertNotIn("3008", text)

    def test_a_string_split_between_reads_is_removed(self):
        text = self.read("a\x1b]3008;sta", "rt=1\x1b", "\\READY\nEND")
        self.assertIn("aREADY\n", text)
        self.assertNotIn("3008", text)

    def test_dcs_queries_and_csi_with_intermediates_are_removed(self):
        text = self.read("x\x1bP+q6E616D65\x1b\\y\x1b[!pz\nEND")
        self.assertIn("xyz\n", text)

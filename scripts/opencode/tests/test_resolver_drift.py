#!/usr/bin/env python3
# Payload resolver drift Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5.2)
# Generated: 2026-09-24 | Budget Used: 2/3 integration (F1 resolver block), 0/2 E2E
"""Contract tests: every TS port carries the canonical payload resolver.

No AC names this suite directly. It is the Design Doc's mitigation for R9 ("Resolver copies drift") and the
Maintainability NFR ("the resolver has a drift test"). It makes the bun results for AC-003..AC-006, which run on
commit-guard and three other ports, hold for all 7 ports.

Ports not converted yet sit in PENDING_PORTS. Each still gets its subTest, skipped with the converting task's id,
so the pending count stays visible in the skip total until the set is empty.

  python3 -m unittest discover -s scripts/opencode/tests
"""

import difflib
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL = REPO_ROOT / "scripts" / "opencode" / "payload-resolver.ts.txt"
CANONICAL_NAME = "scripts/opencode/payload-resolver.ts.txt"
MAPPING = REPO_ROOT / "scripts" / "opencode" / "mapping.json"

OPEN_MARKER = "// <payload-resolver v2>"
CLOSE_MARKER = "// </payload-resolver>"

# Design Doc F1 table: port file -> plugin.json name (PLUGIN_ID).
PORTS = {
    "plugin-commit-guard/plugins/opencode-commit-guard.ts": "commit-guard",
    "plugin-memory-guard/plugins/opencode-memory-guard.ts": "memory-guard",
    "plugin-markdown-format/plugins/opencode-markdown-format.ts": "markdown-format",
    "plugin-token-saver/plugins/opencode-token-saver.ts": "token-saver",
    "plugin-ruby-lsp/plugins/opencode-ruby-lsp.ts": "ruby-lsp",
    "plugin-mempalace-docker/plugins/opencode-mempalace-docker.ts": "mempalace-docker",
    "plugin-qa/plugins/opencode-coverage-quality.ts": "qa",
}

# Plugin ids whose port does not carry resolver v2 yet -> the task that converts it (and removes its entry).
PENDING_PORTS = {
    "ruby-lsp": "5.3",
    "mempalace-docker": "5.4",
    "qa": "6.5",
}

# F1 "Required imports": value imports, by name and unaliased, outside the block.
REQUIRED_IMPORTS = {
    "node:fs": {"existsSync"},
    "node:os": {"homedir"},
    "node:path": {"dirname", "join"},
    "node:url": {"fileURLToPath"},
}

IMPORT_STATEMENT = re.compile(r'^import(\s+type\b)?\s*\{([^}]*)\}\s*from\s*"([^"]+)"', re.MULTILINE)


def read_source(path):
    """The file's text with its line endings untouched, so the comparison stays byte for byte."""
    return Path(path).read_bytes().decode("utf-8")


def split_resolver_block(text):
    """Return (block, outside): the whole lines from the open marker through the close marker, and the rest.

    Raises ValueError unless each marker occurs exactly once, the open marker first.
    """
    for marker in (OPEN_MARKER, CLOSE_MARKER):
        count = text.count(marker)
        if count != 1:
            raise ValueError(f"{marker!r} occurs {count} times; expected exactly once")
    open_at, close_at = text.index(OPEN_MARKER), text.index(CLOSE_MARKER)
    if close_at < open_at:
        raise ValueError(f"{CLOSE_MARKER!r} comes before {OPEN_MARKER!r}")
    start = text.rfind("\n", 0, open_at) + 1
    newline = text.find("\n", close_at)
    end = len(text) if newline == -1 else newline + 1
    return text[start:end], text[:start] + text[end:]


def block_diff(canonical, block, port):
    """A unified diff from the canonical block to the port's block; empty when they are identical."""
    return "".join(difflib.unified_diff(canonical.splitlines(keepends=True), block.splitlines(keepends=True),
                                        fromfile=CANONICAL_NAME, tofile=port))


def imported_names(text):
    """Map each module to the entries of its value imports, as written ("join", "existsSync as exists")."""
    names = {}
    for match in IMPORT_STATEMENT.finditer(text):
        if match.group(1):
            continue
        for item in match.group(2).split(","):
            entry = " ".join(item.split())
            if entry:
                names.setdefault(match.group(3), set()).add(entry)
    return names


def missing_imports(text):
    """The required names TEXT does not import, as sorted "name from module" strings."""
    names = imported_names(text)
    return sorted(f"{name} from {module}" for module, required in REQUIRED_IMPORTS.items()
                  for name in required - names.get(module, set()))


def string_constant(text, name):
    """The value of the one line `const NAME = "..."` in TEXT; ValueError unless there is exactly one."""
    values = re.findall(rf'^const {name} = "([^"\\]*)"$', text, re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f'expected exactly one line `const {name} = "..."`, found {len(values)}')
    return values[0]


def pending_reason(plugin_id):
    """The skip reason for a port that is not converted yet, or None for a port that must be checked."""
    task = PENDING_PORTS.get(plugin_id)
    return f"pending: converted in Task {task}" if task else None


def skip_if_pending(test, plugin_id):
    reason = pending_reason(plugin_id)
    if reason:
        test.skipTest(reason)


def ports_checked_first():
    """PORTS items with the checked ports first, each group in PORTS order.

    Under `unittest -f` a skipped subTest ends its test method, so a pending port listed before a checked one would
    hide the checked one from a failfast run.
    """
    return sorted(PORTS.items(), key=lambda item: pending_reason(item[1]) is not None)


def stale_pending_ports(root=REPO_ROOT):
    """Pending plugin ids whose port file under ROOT already carries the open marker, sorted."""
    return sorted(plugin_id for port, plugin_id in PORTS.items()
                  if pending_reason(plugin_id) and (root / port).is_file() and OPEN_MARKER in read_source(root / port))


class ResolverDriftTests(unittest.TestCase):
    # F1: "Canonical block in scripts/opencode/payload-resolver.ts.txt, copied byte-for-byte between the markers in
    #   each port"
    # Supports: AC-003, AC-004, AC-005, AC-006 (D4) across all ports; R9
    # Given: CANONICAL and every file in PORTS.
    # When: the text from "// <payload-resolver v2>" through "// </payload-resolver>" is sliced out of each port.
    # Then: every port carries the identical block.
    # Verification items (one subTest per port):
    #   - each marker appears exactly once in the port, open before close
    #   - the sliced block equals CANONICAL byte for byte (a unified diff is shown on failure)
    # Pass criteria: 7 of 7 ports match (4 active and 3 pending after Task 1.7).
    # ROI: 68 (BV:6 x Freq:10 + Legal:0 + Defect:8) | runs on every change to a port
    # @category: integration
    # @dependency: payload-resolver.ts.txt, 7 TS ports
    # @real-dependency: filesystem (repo sources)
    # @complexity: low
    def test_every_port_resolver_block_is_byte_identical_to_the_canonical_file(self):
        canonical = read_source(CANONICAL)
        for port, plugin_id in ports_checked_first():
            with self.subTest(port=port):
                skip_if_pending(self, plugin_id)
                block, _outside = split_resolver_block(read_source(REPO_ROOT / port))
                diff = block_diff(canonical, block, port)
                self.assertEqual(diff, "", f"{port}: the resolver block drifted from {CANONICAL_NAME}\n{diff}")

    # F1: "The drift test asserts two things per port: the block between the markers is byte-identical to
    #   payload-resolver.ts.txt, and the file imports all five names."; D003: one plugin id everywhere
    # Supports: AC-003..AC-006 (D4), AC-055 (D003: payload dirs are llm-agent-workflow/<plugin.json name>)
    # Given: every file in PORTS, each plugin's .claude-plugin/plugin.json, and MAPPING.
    # When: each port's import section and its PLUGIN_ID and PAYLOAD_MARKER constants are read.
    # Then: every port can run the block, and it asks for the right payload.
    # Verification items (one subTest per port):
    #   - outside the block, the port imports existsSync from "node:fs", homedir from "node:os", dirname and join
    #     from "node:path", and fileURLToPath from "node:url"
    #   - PLUGIN_ID equals the value in PORTS, and that equals the plugin's plugin.json name
    #   - PAYLOAD_MARKER equals mapping.json payloads[PLUGIN_ID].marker
    # Pass criteria: 7 of 7 ports pass all three checks (4 active and 3 pending after Task 1.7).
    # ROI: 60 (BV:6 x Freq:9 + Legal:0 + Defect:6)
    # @category: integration
    # @dependency: 7 TS ports, plugin.json files, mapping.json
    # @real-dependency: filesystem (repo sources)
    # @complexity: low
    def test_every_port_imports_the_resolver_names_and_uses_its_plugin_id(self):
        payloads = json.loads(read_source(MAPPING))["payloads"]
        for port, plugin_id in ports_checked_first():
            with self.subTest(port=port):
                skip_if_pending(self, plugin_id)
                _block, outside = split_resolver_block(read_source(REPO_ROOT / port))
                manifest = json.loads(read_source(REPO_ROOT / Path(port).parts[0] / ".claude-plugin" / "plugin.json"))
                self.assertEqual(missing_imports(outside), [], f"{port}: missing value imports outside the block")
                self.assertEqual(string_constant(outside, "PLUGIN_ID"), plugin_id)
                self.assertEqual(manifest["name"], plugin_id)
                self.assertEqual(string_constant(outside, "PAYLOAD_MARKER"), payloads[plugin_id]["marker"])

    def test_pending_ports_are_known_ids_that_do_not_carry_the_resolver_yet(self):
        # A converted port left in PENDING_PORTS would never be checked, so its entry must go with the conversion.
        self.assertLessEqual(set(PENDING_PORTS), set(PORTS.values()))
        self.assertIsNone(pending_reason("commit-guard"))
        self.assertIsNone(pending_reason("markdown-format"))
        self.assertIsNone(pending_reason("token-saver"))
        self.assertIsNone(pending_reason("memory-guard"))
        self.assertEqual(pending_reason("qa"), "pending: converted in Task 6.5")
        self.assertEqual(stale_pending_ports(), [], "these ports carry resolver v2; remove them from PENDING_PORTS")
        # The default root is the real tree: a converted port marked pending there is reported.
        converted = {"commit-guard": "9.9", "markdown-format": "9.9", "token-saver": "9.9", "memory-guard": "9.9"}
        with mock.patch.dict(PENDING_PORTS, converted):
            self.assertEqual(stale_pending_ports(), ["commit-guard", "markdown-format", "memory-guard", "token-saver"])


class CanonicalFileTests(unittest.TestCase):
    # The sha256 of the Design Doc F1 ```ts block (revision 1.5.2), which docs/ keeps out of the repo. A new resolver
    # revision changes the markers (v3) and this pin together; the ports then follow through the drift test.
    # To recompute: copy the new F1 block, open marker through close marker, plus one trailing newline into
    # scripts/opencode/payload-resolver.ts.txt, then run `sha256sum scripts/opencode/payload-resolver.ts.txt`.
    CANONICAL_SHA256 = "ad38de5ea0d72cc2df46bcaab3597d4eb43430d8077526f206b44ebcf240e87d"

    def test_the_canonical_file_is_the_design_doc_block(self):
        self.assertEqual(hashlib.sha256(CANONICAL.read_bytes()).hexdigest(), self.CANONICAL_SHA256)

    def test_the_canonical_file_is_exactly_one_marked_block_with_a_trailing_newline(self):
        canonical = read_source(CANONICAL)

        self.assertEqual(split_resolver_block(canonical), (canonical, ""))
        self.assertTrue(canonical.startswith(OPEN_MARKER + " "), canonical[:60])
        self.assertTrue(canonical.endswith("\n}\n" + CLOSE_MARKER + "\n"), canonical[-60:])


class DriftHelperTests(unittest.TestCase):
    """The drift checks themselves, on synthetic ports: each must reject what the contract rules out."""

    BODY = "const A = 1\n"

    def block_text(self, body=BODY):
        return f"{OPEN_MARKER} note\n{body}{CLOSE_MARKER}\n"

    def test_read_source_keeps_crlf_line_endings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "port.ts"
            path.write_bytes(b"a\r\nb\n")

            self.assertEqual(read_source(path), "a\r\nb\n")

    def test_a_missing_or_repeated_marker_is_an_error(self):
        block = self.block_text()
        cases = {
            "no open marker": (f"x\n{self.BODY}{CLOSE_MARKER}\n", f"{OPEN_MARKER!r} occurs 0 times"),
            "two open markers": (f"{OPEN_MARKER}\n{block}", f"{OPEN_MARKER!r} occurs 2 times"),
            "no close marker": (f"{OPEN_MARKER}\n{self.BODY}", f"{CLOSE_MARKER!r} occurs 0 times"),
            "two close markers": (f"{block}{CLOSE_MARKER}\n", f"{CLOSE_MARKER!r} occurs 2 times"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    split_resolver_block(text)

                self.assertEqual(str(caught.exception), f"{message}; expected exactly once")

    def test_a_close_marker_before_the_open_marker_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            split_resolver_block(f"{CLOSE_MARKER}\n{self.BODY}{OPEN_MARKER}\n")

        self.assertEqual(str(caught.exception), f"{CLOSE_MARKER!r} comes before {OPEN_MARKER!r}")

    def test_the_block_is_whole_lines_so_text_beside_a_marker_is_part_of_it(self):
        text = f"head\nx {OPEN_MARKER} y\n{self.BODY}{CLOSE_MARKER} z\ntail\n"

        self.assertEqual(split_resolver_block(text),
                         (f"x {OPEN_MARKER} y\n{self.BODY}{CLOSE_MARKER} z\n", "head\ntail\n"))

    def test_a_block_at_the_end_of_the_file_without_a_newline_stays_without_one(self):
        text = f"head\n{OPEN_MARKER}\n{self.BODY}{CLOSE_MARKER}"

        self.assertEqual(split_resolver_block(text), (f"{OPEN_MARKER}\n{self.BODY}{CLOSE_MARKER}", "head\n"))

    def test_block_diff_is_empty_only_for_an_identical_block(self):
        canonical = self.block_text()
        drifted = self.block_text("const A = 2\n")

        self.assertEqual(block_diff(canonical, canonical, "port.ts"), "")
        self.assertNotEqual(block_diff(canonical, canonical.replace("\n", "\r\n"), "port.ts"), "")
        self.assertEqual(block_diff(canonical, drifted, "port.ts").splitlines(), [
            f"--- {CANONICAL_NAME}", "+++ port.ts", "@@ -1,3 +1,3 @@", f" {OPEN_MARKER} note",
            "-const A = 1", "+const A = 2", f" {CLOSE_MARKER}",
        ])

    def test_imported_names_counts_value_imports_as_written(self):
        text = (
            'import { spawnSync } from "node:child_process"\n'
            'import {\n  dirname,\n  join,\n} from "node:path"\n'
            'import { existsSync as exists } from "node:fs"\n'
            'import type { Plugin } from "@opencode-ai/plugin"\n'
            'import type { homedir } from "node:os"\n'
        )

        self.assertEqual(imported_names(text), {
            "node:child_process": {"spawnSync"},
            "node:path": {"dirname", "join"},
            "node:fs": {"existsSync as exists"},
        })

    def test_commented_and_inline_type_imports_are_not_value_imports(self):
        text = '// import { homedir } from "node:os"\nimport { type existsSync, join } from "node:fs"\n'

        self.assertEqual(imported_names(text), {"node:fs": {"type existsSync", "join"}})
        self.assertIn("existsSync from node:fs", missing_imports(text))
        self.assertIn("homedir from node:os", missing_imports(text))

    def test_a_missing_join_is_reported(self):
        text = ('import { existsSync } from "node:fs"\nimport { homedir } from "node:os"\n'
                'import { dirname } from "node:path"\nimport { fileURLToPath } from "node:url"\n')

        self.assertEqual(missing_imports(text), ["join from node:path"])

    def test_missing_imports_names_each_required_name_not_imported(self):
        text = 'import { join } from "node:path"\nimport { existsSync as exists } from "node:fs"\n'

        self.assertEqual(missing_imports(text), [
            "dirname from node:path", "existsSync from node:fs", "fileURLToPath from node:url", "homedir from node:os",
        ])
        self.assertEqual(missing_imports(
            'import { existsSync } from "node:fs"\nimport { dirname, homedir, join } from "node:path"\n'
            'import { fileURLToPath } from "node:url"\n'), ["homedir from node:os"])
        self.assertEqual(missing_imports(
            'import { existsSync } from "node:fs"\nimport { homedir } from "node:os"\n'
            'import { dirname, join } from "node:path"\nimport { fileURLToPath } from "node:url"\n'), [])

    def test_checked_ports_come_first_and_each_group_keeps_the_ports_order(self):
        with mock.patch.dict(PENDING_PORTS, {"commit-guard": "9.9", "markdown-format": "9.9", "qa": "9.9"}, clear=True):
            order = [plugin_id for _port, plugin_id in ports_checked_first()]

        self.assertEqual(order, ["memory-guard", "token-saver", "ruby-lsp", "mempalace-docker",
                                 "commit-guard", "markdown-format", "qa"])

    def test_stale_pending_ports_names_a_pending_port_that_already_carries_the_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for port, text in {
                "plugin-markdown-format/plugins/opencode-markdown-format.ts": self.block_text(),
                "plugin-token-saver/plugins/opencode-token-saver.ts": "// no resolver yet\n",
                "plugin-commit-guard/plugins/opencode-commit-guard.ts": self.block_text(),
            }.items():
                (root / port).parent.mkdir(parents=True)
                (root / port).write_text(text)

            # Pinned here, so the live pending set can shrink without changing what this test checks.
            with mock.patch.dict(PENDING_PORTS, {"markdown-format": "9.9", "token-saver": "9.9"}, clear=True):
                self.assertEqual(stale_pending_ports(root), ["markdown-format"])

    def test_a_pending_port_carrying_only_the_open_marker_is_stale(self):
        # The contract is "carries the open marker": a half-converted port is already stale.
        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp) / "plugin-markdown-format/plugins/opencode-markdown-format.ts"
            port.parent.mkdir(parents=True)
            port.write_text(f"{OPEN_MARKER} note\n{self.BODY}")

            with mock.patch.dict(PENDING_PORTS, {"markdown-format": "9.9"}, clear=True):
                self.assertEqual(stale_pending_ports(Path(tmp)), ["markdown-format"])

    def test_string_constant_needs_exactly_one_top_level_line(self):
        self.assertEqual(string_constant('const PLUGIN_ID = "commit-guard"\n', "PLUGIN_ID"), "commit-guard")
        cases = {
            "missing": ('const OTHER = "x"\n', 0),
            "repeated": ('const PLUGIN_ID = "a"\nconst PLUGIN_ID = "b"\n', 2),
            "indented": ('  const PLUGIN_ID = "a"\n', 0),
            "exported": ('export const PLUGIN_ID = "a"\n', 0),
            "trailing text": ('const PLUGIN_ID = "a" // id\n', 0),
        }
        for name, (text, found) in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    string_constant(text, "PLUGIN_ID")

                self.assertEqual(str(caught.exception),
                                 f'expected exactly one line `const PLUGIN_ID = "..."`, found {found}')


if __name__ == "__main__":
    unittest.main()

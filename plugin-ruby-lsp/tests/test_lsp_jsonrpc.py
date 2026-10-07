#!/usr/bin/env python3
"""Framing and position-math tests for scripts/lsp_jsonrpc.py.

  python3 -m unittest discover -s plugin-ruby-lsp/tests
"""

import asyncio
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lsp_jsonrpc as rpc  # noqa: E402


def pos(line, character):
    return {"line": line, "character": character}


class FramingTests(unittest.TestCase):
    def test_round_trip_sync(self):
        stream = io.BytesIO(rpc.encode({"id": 1, "method": "x", "params": {"t": "😀"}}) + rpc.encode({"id": 2}))
        self.assertEqual(rpc.read_message_sync(stream), {"id": 1, "method": "x", "params": {"t": "😀"}})
        self.assertEqual(rpc.read_message_sync(stream), {"id": 2})
        self.assertIsNone(rpc.read_message_sync(stream))

    def test_content_length_counts_bytes_not_characters(self):
        frame = rpc.encode({"t": "é"})
        header, body = frame.split(b"\r\n\r\n", 1)
        self.assertEqual(int(header.split(b":")[1]), len(body))

    def test_round_trip_async(self):
        async def read_two():
            reader = asyncio.StreamReader()
            reader.feed_data(rpc.encode({"id": 1}) + rpc.encode({"id": 2}))
            reader.feed_eof()
            return [await rpc.read_message(reader), await rpc.read_message(reader), await rpc.read_message(reader)]

        self.assertEqual(asyncio.run(read_two()), [{"id": 1}, {"id": 2}, None])

    def test_missing_content_length_is_framing_error(self):
        with self.assertRaises(rpc.FramingError):
            rpc.read_message_sync(io.BytesIO(b"Content-Type: x\r\n\r\n{}"))

    def test_truncated_body_is_framing_error(self):
        with self.assertRaises(rpc.FramingError):
            rpc.read_message_sync(io.BytesIO(b"Content-Length: 10\r\n\r\n{}"))

    def test_non_object_body_is_framing_error(self):
        with self.assertRaises(rpc.FramingError):
            rpc.read_message_sync(io.BytesIO(b"Content-Length: 2\r\n\r\n[]"))

    def test_truncated_async_body_is_framing_error(self):
        async def read():
            reader = asyncio.StreamReader()
            reader.feed_data(b"Content-Length: 10\r\n\r\n{}")
            reader.feed_eof()
            return await rpc.read_message(reader)

        with self.assertRaises(rpc.FramingError):
            asyncio.run(read())


class PositionTests(unittest.TestCase):
    def test_emoji_is_two_utf16_units(self):
        # "😀" is one Python character but two UTF-16 code units.
        self.assertEqual(rpc.end_position("a\n😀b"), pos(1, 3))
        self.assertEqual(rpc.end_position("a\n😀b", "utf-8"), pos(1, 5))
        self.assertEqual(rpc.end_position("a\n😀b", "utf-32"), pos(1, 2))

    def test_offset_after_emoji(self):
        self.assertEqual(rpc.offset_of("😀b", pos(0, 2)), 1)
        self.assertEqual(rpc.offset_of("😀b", pos(0, 3)), 2)

    def test_offsets_clamp_past_line_and_file_end(self):
        text = "ab\ncd"
        self.assertEqual(rpc.offset_of(text, pos(0, 99)), 2)
        self.assertEqual(rpc.offset_of(text, pos(9, 0)), len(text))

    def test_crlf_and_cr_end_lines(self):
        self.assertEqual(rpc.end_position("a\r\nb\rc"), pos(2, 1))
        self.assertEqual(rpc.offset_of("a\r\nb", pos(1, 0)), 3)

    def test_trailing_newline_ends_on_empty_line(self):
        self.assertEqual(rpc.end_position("x = 1\n"), pos(1, 0))


class ApplyChangesTests(unittest.TestCase):
    def test_whole_text_change_replaces(self):
        self.assertEqual(rpc.apply_changes("old", [{"text": "new"}]), "new")

    def test_ranged_insert_after_emoji(self):
        change = {"range": {"start": pos(0, 2), "end": pos(0, 2)}, "text": "X"}
        self.assertEqual(rpc.apply_changes("😀b", [change]), "😀Xb")

    def test_whole_document_range_replacement(self):
        old = "a\n😀b"
        change = {"range": {"start": pos(0, 0), "end": rpc.end_position(old)}, "text": "c\n"}
        self.assertEqual(rpc.apply_changes(old, [change]), "c\n")

    def test_changes_apply_in_order(self):
        changes = [
            {"range": {"start": pos(0, 0), "end": pos(0, 1)}, "text": "b"},
            {"range": {"start": pos(0, 1), "end": pos(0, 1)}, "text": "c"},
        ]
        self.assertEqual(rpc.apply_changes("a", changes), "bc")

    def test_reversed_range_is_normalised(self):
        change = {"range": {"start": pos(0, 3), "end": pos(0, 1)}, "text": "-"}
        self.assertEqual(rpc.apply_changes("abcd", [change]), "a-d")


if __name__ == "__main__":
    unittest.main()

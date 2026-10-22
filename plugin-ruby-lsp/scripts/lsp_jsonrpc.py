#!/usr/bin/env python3
"""JSON-RPC framing and LSP text math shared by the ruby-lsp hub and bridge.

Standard library only. Positions follow LSP: zero-based lines, and a character
offset counted in the negotiated position encoding (UTF-16 code units unless
the server picked "utf-8" or "utf-32").
"""

import json

MAX_CONTENT_LENGTH = 64 * 1024 * 1024


class FramingError(Exception):
    """A peer sent bytes that are not a valid LSP frame."""


def encode(message):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def _content_length(header_lines):
    length = None
    for line in header_lines:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                raise FramingError("bad Content-Length: %r" % value) from None
    if length is None or length < 0 or length > MAX_CONTENT_LENGTH:
        raise FramingError("missing or out-of-range Content-Length")
    return length


def _decode(body):
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FramingError("body is not JSON: %s" % exc) from None
    if not isinstance(message, dict):
        raise FramingError("body is not a JSON object")
    return message


async def read_message(reader):
    """Read one message from an asyncio.StreamReader. None on clean EOF."""
    header_lines = []
    while True:
        line = await reader.readline()
        if not line:
            if header_lines:
                raise FramingError("EOF inside headers")
            return None
        line = line.rstrip(b"\r\n")
        if not line:
            if not header_lines:
                continue
            break
        header_lines.append(line)
    length = _content_length(header_lines)
    try:
        body = await reader.readexactly(length)
    except Exception as exc:  # asyncio.IncompleteReadError
        raise FramingError("EOF inside body") from exc
    return _decode(body)


def read_message_sync(stream):
    """Read one message from a binary file object. None on clean EOF."""
    header_lines = []
    while True:
        line = stream.readline()
        if not line:
            if header_lines:
                raise FramingError("EOF inside headers")
            return None
        line = line.rstrip(b"\r\n")
        if not line:
            if not header_lines:
                continue
            break
        header_lines.append(line)
    length = _content_length(header_lines)
    body = stream.read(length)
    if len(body) != length:
        raise FramingError("EOF inside body")
    return _decode(body)


def _units(char, encoding):
    if encoding == "utf-8":
        return len(char.encode("utf-8"))
    if encoding == "utf-32":
        return 1
    return 2 if ord(char) > 0xFFFF else 1


def _line_starts(text):
    """Index of the first character of every line (\\n, \\r\\n and \\r end a line)."""
    starts = [0]
    i = 0
    size = len(text)
    while i < size:
        char = text[i]
        if char == "\r":
            if i + 1 < size and text[i + 1] == "\n":
                i += 1
            starts.append(i + 1)
        elif char == "\n":
            starts.append(i + 1)
        i += 1
    return starts


def _line_end(text, start):
    """Index just past the last character of the line, before its terminator."""
    end = start
    while end < len(text) and text[end] not in "\r\n":
        end += 1
    return end


def offset_of(text, position, encoding="utf-16"):
    """String index for an LSP position. Out-of-range values clamp, as LSP asks."""
    starts = _line_starts(text)
    line = position.get("line", 0)
    if line >= len(starts):
        return len(text)
    start = starts[line]
    end = _line_end(text, start)
    remaining = position.get("character", 0)
    index = start
    while index < end and remaining > 0:
        remaining -= _units(text[index], encoding)
        index += 1
    return index


def end_position(text, encoding="utf-16"):
    """LSP position just past the last character of text."""
    starts = _line_starts(text)
    last = starts[-1]
    return {
        "line": len(starts) - 1,
        "character": sum(_units(char, encoding) for char in text[last:]),
    }


def apply_changes(text, changes, encoding="utf-16"):
    """Apply didChange contentChanges, both whole-text and ranged, in order."""
    for change in changes:
        replacement = change.get("text", "")
        change_range = change.get("range")
        if change_range is None:
            text = replacement
            continue
        start = offset_of(text, change_range["start"], encoding)
        end = offset_of(text, change_range["end"], encoding)
        if end < start:
            start, end = end, start
        text = text[:start] + replacement + text[end:]
    return text

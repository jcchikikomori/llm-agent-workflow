"""Tracker v2 (Design Doc F4, revision 1.5): the installer's record of every unit it wrote, one file per scope.

  # setup-opencode.sh tracker v2
  # <key>: <value>                                        every HEADER_KEYS entry, always present
  type<TAB>plugin<TAB>path<TAB>realpath<TAB>hash<TAB>repo   one row per unit, sorted, keyed by (type, path)

A v1 tracker (`# setup-opencode.sh tracker v1`, then one scope-relative path per line) is read as rows
`file ? <path> <current realpath> - <repo>`; a line without a tab is such a v1 row in any tracker. Reading never
writes. A merge always writes a v2 file (the v1 upgrade path) through a temp file and `os.replace`, or deletes the file
when no rows remain.

Every read validates the whole file. A structural problem raises mapping.ConvertError, printed as
`ERROR <file>[:<line>]: tracker: <problem>`, before anything is written. The structural rules a row must pass:

1. exactly 6 tab-separated columns, none holding a control character, and a type of `file`, `dir` or `config`
1. a scope-relative path: not absolute, no `..` anywhere, no empty or `.` segment; `file` and `dir` paths start with
   a writable scope dir and name something below it, `config` paths are `opencode.json#/<pointer>`
1. a hash of `sha256:<64 lowercase hex>` or `-`
1. an absolute realpath that does not resolve (guard.resolve_target) to a G0 containment root itself
1. no second row with the same (type, path)

A row that passes them but whose realpath resolves outside every current containment root (a scope symlink retargeted
after install) is not an error: it is a kept row. It stays in the file byte for byte, `read` leaves it out of its rows
and warns about it, and only `--remove` or an added row with its (type, path) drops it, so it can never become a
delete. `--add` rows must lie inside the roots.

The containment roots come from the tracker's own directory, never from its header, so a tampered header cannot widen
the scope. Messages name the file and line, never a rejected realpath or a header value.

Stdlib only; runs on Python 3.10 and later.
"""

import os
import re
import tempfile
import unicodedata
from dataclasses import astuple, dataclass, field
from pathlib import Path

import guard
import mapping

RULE = "tracker"
MAGIC_V1 = "# setup-opencode.sh tracker v1"
MAGIC_V2 = "# setup-opencode.sh tracker v2"
COLUMNS = ("type", "plugin", "path", "realpath", "hash", "repo")
COLUMNS_KEY = "columns"
COLUMNS_VALUE = " ".join(COLUMNS)
SETTABLE_HEADER_KEYS = ("installed_at", "repo_root", "scope", "scope_root", "payload_root", "recipe_policy",
                        "mcp_aliases", "allowed_repos", "skills_md")
HEADER_KEYS = SETTABLE_HEADER_KEYS + (COLUMNS_KEY,)
V1_HEADER_KEYS = ("installed_at", "repo_root", "scope")
ROW_TYPES = ("file", "dir", "config")
CONFIG_TYPE = "config"
CONFIG_FILE_SEGMENT = "opencode.json#"
V1_TYPE = "file"
V1_PLUGIN = "?"
NO_VALUE = guard.NO_VALUE
HEADER_ARGUMENT = "--header"
MERGE_COMMAND = "tracker merge"

_HEADER_LINE = re.compile(r"# (?P<key>[a-z_]+):(?: (?P<value>.*))?")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
# Characters that could end a line in some reader: C0/C1 controls and the Unicode line and paragraph separators.
_LINE_BREAKING_CATEGORIES = ("Cc", "Zl", "Zp")

MALFORMED_HEADER = "malformed header"
NOT_A_TRACKER = f"{MALFORMED_HEADER}: line 1 is not a tracker v1 or v2 line"
EXPECTED_HEADER_LINE = f'{MALFORMED_HEADER}: expected "# <key>: <value>"'
COMMENT_AFTER_ROWS = f"{MALFORMED_HEADER}: comment line after the first row"
UNKNOWN_TYPE = f"unknown type (expected {', '.join(ROW_TYPES[:-1])} or {ROW_TYPES[-1]})"
ABSOLUTE_PATH = "absolute path"
DOT_DOT_PATH = "path contains .."
UNNORMALIZED_PATH = "path not normalized"
PREFIX_NOT_ALLOWED = "path prefix not allowed"
MALFORMED_HASH = "malformed hash (expected sha256:<64 lowercase hex> or -)"
RELATIVE_REALPATH = "realpath not absolute"
ROOT_REALPATH = "realpath is a scope root"
OUTSIDE_SCOPE = "realpath outside the scope"
KEPT_OUTSIDE_SCOPE = f"{OUTSIDE_SCOPE} (kept)"
DUPLICATE_ROW = "duplicate row"
IN_ADD_AND_REMOVE = "a (type, path) is in both --add and --remove"


def _error(where, message):
    return mapping.ConvertError(where, RULE, message)


def _control_character(key):
    return f'the value of "{key}" has a control character'


def _column_control_character(column):
    return f'the "{column}" column has a control character'


def has_line_break(value):
    """True when VALUE holds a character some reader could take as a line end (convert.py plan uses it too)."""
    return any(unicodedata.category(char) in _LINE_BREAKING_CATEGORIES for char in value)


# -- data -----------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """Where a tracker's rows may point: below the tracker's own directory or a writable scope dir."""

    root: str
    writable_dirs: tuple
    roots: tuple

    @classmethod
    def of_tracker(cls, tracker_path, writable_scope_dirs):
        root = os.path.dirname(os.path.abspath(os.fspath(tracker_path)))
        dirs = tuple(writable_scope_dirs)
        return cls(root, dirs, tuple(guard.containment_roots(root, dirs)))


@dataclass(frozen=True, order=True)
class Row:
    """One unit; rows sort by their columns in order, which is the sort order of their tab-joined lines."""

    type: str
    plugin: str
    path: str
    realpath: str
    hash: str
    repo: str

    @property
    def key(self):
        return (self.type, self.path)

    def line(self):
        return "\t".join(astuple(self))


@dataclass(frozen=True)
class KeptRow:
    """A stored row whose realpath lies outside the current containment roots: written back as LINE, never read out."""

    row: Row
    line: str
    where: str


@dataclass
class Tracker:
    """A parsed tracker: header values by key, rows by (type, path), and kept rows by (type, path).

    `rows` holds only rows inside the current roots (v1 rows already normalized); `kept` rows are never part of it.
    """

    header: dict = field(default_factory=dict)
    rows: dict = field(default_factory=dict)
    kept: dict = field(default_factory=dict)

    def sorted_rows(self, plugin=None):
        return sorted(row for row in self.rows.values() if plugin is None or row.plugin == plugin)

    def warnings(self):
        """One WARN line per kept row, in file order."""
        return [f"WARN {entry.where}: {RULE}: {KEPT_OUTSIDE_SCOPE}" for entry in self.kept.values()]


# -- row validation -------------------------------------------------------------


def _check_columns(fields, where):
    """Raise when a column holds a control character (the header-value rule, applied to every column)."""
    for column, value in zip(COLUMNS, fields):
        if has_line_break(value):
            raise _error(where, _column_control_character(column))


def _split_row(line, where):
    fields = line.split("\t")
    if len(fields) != len(COLUMNS):
        raise _error(where, f"wrong column count: {len(fields)}, expected {len(COLUMNS)}")
    _check_columns(fields, where)
    return fields


def check_key(row_type, path, scope, where):
    """Raise unless ROW_TYPE is known and PATH is a scope-relative path with an allowed prefix."""
    if row_type not in ROW_TYPES:
        raise _error(where, UNKNOWN_TYPE)
    if path.startswith("/"):
        raise _error(where, ABSOLUTE_PATH)
    if ".." in path:
        raise _error(where, DOT_DOT_PATH)
    segments = path.split("/")
    if "" in segments or "." in segments:
        raise _error(where, UNNORMALIZED_PATH)
    allowed = (CONFIG_FILE_SEGMENT,) if row_type == CONFIG_TYPE else scope.writable_dirs
    if len(segments) < 2 or segments[0] not in allowed:
        raise _error(where, PREFIX_NOT_ALLOWED)


def realpath_in_scope(realpath, scope, where):
    """True when REALPATH resolves below a containment root (G0), False when it lies outside them all.

    A relative realpath, or one that resolves to a root itself, is structural and raises.
    """
    if not os.path.isabs(realpath):
        raise _error(where, RELATIVE_REALPATH)
    resolved = guard.resolve_target(realpath)
    if resolved in scope.roots:
        raise _error(where, ROOT_REALPATH)
    return guard.is_contained(resolved, scope.roots)


def _structural_row(line, scope, where):
    """A 6-column row whose columns pass every structural check; its realpath may still lie outside the roots."""
    row = Row(*_split_row(line, where))
    check_key(row.type, row.path, scope, where)
    if row.hash != NO_VALUE and _HASH.fullmatch(row.hash) is None:
        raise _error(where, MALFORMED_HASH)
    return row


def parse_row(line, scope, where):
    """An --add row: structurally valid, with a realpath inside the roots."""
    row = _structural_row(line, scope, where)
    if not realpath_in_scope(row.realpath, scope, where):
        raise _error(where, OUTSIDE_SCOPE)
    return row


def _repo_of(realpath, git, where):
    """The git toplevel holding REALPATH, or `-`; fails closed below a `.git` ancestor git cannot answer for (IP-9)."""
    directory = guard.lookup_dir(realpath)
    toplevel = git.toplevel(directory)
    if toplevel:
        return toplevel
    if guard.has_git_ancestor(directory):
        raise _error(where, f"repo unknown: {git.failure(directory) or 'git gave no work tree'}")
    return NO_VALUE


def parse_v1_row(line, scope, git, where):
    """(row, inside) for a v1 line: `file ? <line> <current realpath> - <repo>`; a kept row gets no repo lookup."""
    if has_line_break(line):
        raise _error(where, _column_control_character("path"))
    check_key(V1_TYPE, line, scope, where)
    realpath = guard.resolve_target(os.path.join(scope.root, line))
    if not realpath_in_scope(realpath, scope, where):
        return Row(V1_TYPE, V1_PLUGIN, line, realpath, NO_VALUE, NO_VALUE), False
    return Row(V1_TYPE, V1_PLUGIN, line, realpath, NO_VALUE, _repo_of(realpath, git, where)), True


def parse_stored_row(line, scope, git, where):
    """(row, inside) for a tracker line, a 6-column row or a v1 line."""
    if "\t" not in line:
        return parse_v1_row(line, scope, git, where)
    row = _structural_row(line, scope, where)
    return row, realpath_in_scope(row.realpath, scope, where)


def _add_row(rows, row, where):
    if row.key in rows:
        raise _error(where, DUPLICATE_ROW)
    rows[row.key] = row


# -- reading --------------------------------------------------------------------


def _read_lines(path):
    """The lines of PATH split on `\\n` only, without the final newline."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise _error(path, f"unreadable: {error.strerror or error}") from error
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _error(path, "unreadable: not UTF-8") from error
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _numbered(path, lines, start):
    """(where, line) for each non-empty line from index START on."""
    for index in range(start, len(lines)):
        if lines[index]:
            yield f"{path}:{index + 1}", lines[index]


def _header_end(lines):
    """The index of the first line after the magic line that is neither empty nor a comment."""
    end = 1
    while end < len(lines) and (not lines[end] or lines[end].startswith("#")):
        end += 1
    return end


def _parse_header(path, lines, end, allowed_keys):
    header = {}
    for where, line in _numbered(path, lines[:end], 1):
        match = _HEADER_LINE.fullmatch(line)
        if match is None:
            raise _error(where, EXPECTED_HEADER_LINE)
        key = match["key"]
        if key not in allowed_keys:
            raise _error(where, f'{MALFORMED_HEADER}: unknown key "{key}"')
        if key in header:
            raise _error(where, f'{MALFORMED_HEADER}: key "{key}" appears twice')
        value = match["value"] or ""
        if has_line_break(value):
            raise _error(where, f"{MALFORMED_HEADER}: {_control_character(key)}")
        if key == COLUMNS_KEY and value != COLUMNS_VALUE:
            raise _error(where, f'{MALFORMED_HEADER}: columns must be "{COLUMNS_VALUE}"')
        header[key] = value
    return header


def _check_complete(path, header):
    for key in HEADER_KEYS:
        if key not in header:
            raise _error(path, f'{MALFORMED_HEADER}: missing key "{key}"')


def _parse_body(path, lines, start, scope, git):
    """(rows, kept) from the lines after the header; every key is unique across both."""
    seen, rows, kept = {}, {}, {}
    for where, line in _numbered(path, lines, start):
        if line.startswith("#"):
            raise _error(where, COMMENT_AFTER_ROWS)
        row, inside = parse_stored_row(line, scope, git, where)
        _add_row(seen, row, where)
        if inside:
            rows[row.key] = row
        else:
            kept[row.key] = KeptRow(row, line, where)
    return rows, kept


def parse(path, lines, scope, git):
    """Validate the lines of the tracker at PATH and return it as a Tracker."""
    where = f"{path}:1"
    if not lines or lines[0] not in (MAGIC_V1, MAGIC_V2):
        raise _error(where, NOT_A_TRACKER)
    is_v1 = lines[0] == MAGIC_V1
    end = _header_end(lines)
    header = _parse_header(path, lines, end, V1_HEADER_KEYS if is_v1 else HEADER_KEYS)
    if not is_v1:
        _check_complete(path, header)
    rows, kept = _parse_body(path, lines, end, scope, git)
    return Tracker(header, rows, kept)


def load(path, scope, git=None):
    """Read and validate the tracker at PATH; a missing file is an empty v2 tracker."""
    path = os.fspath(path)
    if not os.path.lexists(path):
        return Tracker()
    return parse(path, _read_lines(path), scope, guard.GitLookup() if git is None else git)


def read_rows_file(path, scope):
    """The rows of an --add ROWS file, each validated like a tracker row and inside the roots; empty holds none."""
    path = os.fspath(path)
    rows = {}
    for where, line in _numbered(path, _read_lines(path), 0):
        _add_row(rows, parse_row(line, scope, where), where)
    return list(rows.values())


def read_remove_keys(path, scope):
    """The (type, path) keys of a --remove ROWS file; its other columns are ignored."""
    path = os.fspath(path)
    keys = set()
    for where, line in _numbered(path, _read_lines(path), 0):
        fields = _split_row(line, where)
        check_key(fields[0], fields[2], scope, where)
        keys.add((fields[0], fields[2]))
    return keys


def parse_header_args(pairs):
    """{key: value} from --header KEY=VALUE arguments."""
    updates = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator:
            raise _error(HEADER_ARGUMENT, "expected KEY=VALUE")
        if key not in SETTABLE_HEADER_KEYS:
            raise _error(HEADER_ARGUMENT, f'not a settable header key: "{key}"')
        if key in updates:
            raise _error(HEADER_ARGUMENT, f'"{key}" is given twice')
        if has_line_break(value):
            raise _error(HEADER_ARGUMENT, _control_character(key))
        updates[key] = value
    return updates


# -- merging and writing --------------------------------------------------------


def merge(tracker, add_rows, remove_keys, header_updates):
    """TRACKER without REMOVE_KEYS, plus ADD_ROWS (a row replaces the stored or kept row with its key), as a v2 Tracker.

    Kept rows stay as they are unless removed or replaced. Header keys in HEADER_UPDATES replace the stored values;
    the others are kept, and missing ones are empty.
    """
    if remove_keys & {row.key for row in add_rows}:
        raise _error(MERGE_COMMAND, IN_ADD_AND_REMOVE)
    rows = {key: row for key, row in tracker.rows.items() if key not in remove_keys}
    rows.update((row.key, row) for row in add_rows)
    kept = {key: entry for key, entry in tracker.kept.items() if key not in remove_keys and key not in rows}
    header = {key: header_updates.get(key, tracker.header.get(key, "")) for key in SETTABLE_HEADER_KEYS}
    header[COLUMNS_KEY] = COLUMNS_VALUE
    return Tracker(header, rows, kept)


def render(tracker):
    """The v2 file text: magic line, every header key in order, then rows and kept lines sorted together."""
    lines = [MAGIC_V2]
    for key in HEADER_KEYS:
        value = COLUMNS_VALUE if key == COLUMNS_KEY else tracker.header.get(key, "")
        lines.append(f"# {key}: {value}" if value else f"# {key}:")
    stored = [(row, row.line()) for row in tracker.rows.values()]
    stored += [(entry.row, entry.line) for entry in tracker.kept.values()]
    lines.extend(line for _row, line in sorted(stored))
    return "".join(f"{line}\n" for line in lines)


def _replace_atomically(path, text):
    """Write TEXT to a temp file beside PATH, then rename it over PATH; the temp file never outlives the call."""
    directory, name = os.path.split(os.path.abspath(path))
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=directory, prefix=f".{name}.",
                                         suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise


def save(path, tracker):
    """Write TRACKER to PATH atomically, or delete PATH when no rows (kept rows included) remain."""
    path = os.fspath(path)
    try:
        if tracker.rows or tracker.kept:
            _replace_atomically(path, render(tracker))
        elif os.path.lexists(path):
            os.remove(path)
    except OSError as error:
        raise _error(path, f"cannot write: {error.strerror or error}") from error

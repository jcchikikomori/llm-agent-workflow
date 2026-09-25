"""Load and validate scripts/opencode/mapping.json (Design Doc "mapping.json schema (version 2)").

SCHEMA declares every key of the Design Doc schema block, plus `snippets` (the per-plugin config override used from
Task 5.1), with its JSON type. A key that no task fills yet is optional; a key that is present must match its declared
type. Every violation is fatal and reads `ERROR mapping.json: <rule>: <message>`, where the message names the JSON
pointer of the offending value.

Stdlib only; runs on Python 3.10 and later.
"""

import json
from pathlib import Path, PurePosixPath

LABEL = "mapping.json"
MAPPING_RELPATH = PurePosixPath("scripts/opencode/mapping.json")
DEFAULT_PATH = Path(__file__).resolve().with_name("mapping.json")


class ConvertError(Exception):
    """A fatal input problem, printed as `ERROR <where>: <rule>: <message>`."""

    def __init__(self, where, rule, message):
        super().__init__(f"ERROR {where}: {rule}: {message}")
        self.where = where
        self.rule = rule
        self.message = message


def mapping_error(rule, message):
    return ConvertError(LABEL, rule, message)


def repo_mapping_path(repo):
    """The mapping.json a `--repo DIR` run reads."""
    return Path(repo) / MAPPING_RELPATH


# -- schema nodes -------------------------------------------------------------


def json_type(value):
    """The JSON type name of a parsed value; bool is never an integer."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _pointer(parent, key):
    """Append KEY to a JSON pointer, escaping `~` and `/` (RFC 6901)."""
    return f"{parent}/{str(key).replace('~', '~0').replace('/', '~1')}"


class _Node:
    """One schema node: a JSON type name plus checks on the value's contents."""

    type_name = ""

    def check(self, value, pointer):
        actual = json_type(value)
        if actual != self.type_name:
            raise mapping_error("wrong-type", f"{pointer or '/'} must be {_article(self.type_name)}, got {actual}")
        self._check_contents(value, pointer)

    def _check_contents(self, value, pointer):
        """Subclasses validate what is inside a value of the right type."""


def _article(type_name):
    return f"an {type_name}" if type_name[0] in "aeiou" else f"a {type_name}"


class _Scalar(_Node):
    def __init__(self, type_name, allowed=()):
        self.type_name = type_name
        self.allowed = tuple(allowed)

    def _check_contents(self, value, pointer):
        if self.allowed and value not in self.allowed:
            expected = " or ".join(json.dumps(choice) for choice in self.allowed)
            raise mapping_error("wrong-value", f"{pointer} must be {expected}, got {json.dumps(value)}")


class _RelativePath(_Scalar):
    """A repo- or plugin-relative POSIX path (or glob): not empty, not absolute, no `..` segment."""

    def __init__(self):
        super().__init__("string")

    def _check_contents(self, value, pointer):
        parts = value.split("/")
        if not value or value.startswith("/") or ".." in parts or "\\" in value:
            raise mapping_error("relative-path",
                                f"{pointer} must be a relative path without '..', got {json.dumps(value)}")


class _List(_Node):
    type_name = "array"

    def __init__(self, item):
        self.item = item

    def _check_contents(self, value, pointer):
        for index, item in enumerate(value):
            self.item.check(item, _pointer(pointer, index))


class _Map(_Node):
    """An object with free-form keys whose values all share one schema."""

    type_name = "object"

    def __init__(self, value_node):
        self.value_node = value_node

    def _check_contents(self, value, pointer):
        for key, item in value.items():
            self.value_node.check(item, _pointer(pointer, key))


class _AnyObject(_Node):
    """An object whose contents are not constrained here (validated by the task that consumes it)."""

    type_name = "object"


class _Record(_Node):
    """An object with a fixed key set; unknown keys and missing required keys are fatal."""

    type_name = "object"

    def __init__(self, fields, required=()):
        self.fields = fields
        self.required = tuple(required)

    def _check_contents(self, value, pointer):
        for key in value:
            if key not in self.fields:
                raise mapping_error("unknown-key", f"{_pointer(pointer, key)} is not a known key")
        for key in self.required:
            if key not in value:
                raise mapping_error("missing-key", f"{_pointer(pointer, key)} is required")
        for key, item in value.items():
            self.fields[key].check(item, _pointer(pointer, key))


class _OneOf(_Node):
    """A value that may take one of several JSON types, each with its own schema."""

    def __init__(self, *options):
        self.options = {option.type_name: option for option in options}

    def check(self, value, pointer):
        option = self.options.get(json_type(value))
        if option is None:
            expected = " or ".join(_article(name) for name in self.options)
            raise mapping_error("wrong-type", f"{pointer} must be {expected}, got {json_type(value)}")
        option.check(value, pointer)


class _PathRule(_Record):
    """A `paths` rewrite entry: it either rewrites (`to`) or protects (`preserve`), never both."""

    def _check_contents(self, value, pointer):
        super()._check_contents(value, pointer)
        if "preserve" in value and "to" in value:
            raise mapping_error("paths-preserve-and-to", f"{pointer} has both \"preserve\" and \"to\"")
        if "preserve" not in value and "to" not in value:
            raise mapping_error("paths-preserve-or-to", f"{pointer} needs \"to\" or \"preserve\"")


# -- the schema ---------------------------------------------------------------

_STRING = _Scalar("string")
_INTEGER = _Scalar("integer")
_BOOLEAN = _Scalar("boolean")
_STRINGS = _List(_STRING)
_STRING_MAP = _Map(_STRING)
_RELPATH = _RelativePath()
_RELPATHS = _List(_RELPATH)

_EXTRA_SOURCE = _Record({"manifest": _RELPATH, "plugins": _RELPATHS, "skills": _RELPATHS}, required=("manifest",))
_PHRASE = _Record({"from": _STRING, "to": _STRING}, required=("from", "to"))
_PATH_RULE = _PathRule(
    {"id": _STRING, "from": _OneOf(_STRING, _STRINGS), "to": _STRING, "match": _STRING,
     "preserve": _Scalar("boolean", (True,)), "only_files": _RELPATHS, "exclude_files": _RELPATHS},
    required=("id", "from"),
)
_MCP = _Record({"known_servers": _STRINGS, "aliases": _STRING_MAP, "aliases_apply_to_bodies": _BOOLEAN,
                "max_tool_key": _INTEGER})
_EXTERNAL_DIRECTORY = _Record({"skills_root_for_agents_with_skills": _BOOLEAN, "per_agent": _Map(_STRINGS)})
_AGENTS = _Record({"drop_keys": _STRINGS, "description_max_chars": _INTEGER})
_SKILLS = _Record({"install_root": _STRING, "name_pattern": _STRING, "description_max_bytes": _INTEGER,
                   "strip_keys": _STRINGS, "exclude": _STRINGS, "scanned_roots": _STRINGS})
_RECIPE_POLICY = _Record({"deny_agents": _STRINGS, "allow_agents": _STRINGS, "patterns": _STRINGS})
_PAYLOAD = _Record({"include": _RELPATHS, "exclude": _RELPATHS, "marker": _RELPATH}, required=("include", "marker"))
_GUARD_KEYS = ("blocked_origin_repo_names", "blocked_scope_dirs", "writable_scope_dirs", "submodules")
_GUARD = _Record({"blocked_origin_repo_names": _STRINGS, "blocked_scope_dirs": _STRINGS,
                  "writable_scope_dirs": _STRINGS, "submodules": _Scalar("string", ("approval",))},
                 required=_GUARD_KEYS)
_LEGACY = _Record({"path": _RELPATH, "plugin": _STRING}, required=("path", "plugin"))

# The keys filled so far; later tasks move a key here when they fill it.
REQUIRED_KEYS = ("schema_version", "payload_namespace", "plugin_id_source", "extra_sources", "exclude_plugins",
                 "exclude_agents", "payloads", "guard", "hand_written_agent_prefix")

SCHEMA = _Record(
    {
        "schema_version": _Scalar("integer", (2,)),
        "payload_namespace": _STRING,
        "plugin_id_source": _Scalar("string", ("plugin.json#name",)),
        "extra_sources": _Map(_EXTRA_SOURCE),
        "exclude_plugins": _STRINGS,
        "exclude_agents": _RELPATHS,
        "tools": _STRING_MAP,
        "identifiers": _STRING_MAP,
        "backticked": _STRING_MAP,
        "phrases": _List(_PHRASE),
        "memory_root": _STRING,
        "paths": _List(_PATH_RULE),
        "agent_namespaces": _STRINGS,
        "subagent_type_ignore": _STRINGS,
        "mcp": _MCP,
        "external_directory": _EXTERNAL_DIRECTORY,
        "agents": _AGENTS,
        "skills": _SKILLS,
        "recipe_policy": _RECIPE_POLICY,
        "payloads": _Map(_PAYLOAD),
        "guard": _GUARD,
        "atomic_pointers": _STRINGS,
        "append_pointers": _STRINGS,
        "legacy": _List(_LEGACY),
        "hand_written_agent_prefix": _STRING,
        "snippets": _Map(_AnyObject()),
    },
    required=REQUIRED_KEYS,
)


# -- loading ------------------------------------------------------------------


def _reject_duplicate_keys(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise mapping_error("duplicate-key", f"{json.dumps(key)} appears twice in one object")
        data[key] = value
    return data


def validate(data):
    """Raise ConvertError unless DATA (parsed mapping.json) matches SCHEMA."""
    SCHEMA.check(data, "")


def load(path=DEFAULT_PATH):
    """Read, parse and validate the mapping at PATH; return it as a dict."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise mapping_error("unreadable", f"cannot read {path}: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise mapping_error("invalid-json", f"{path} is not UTF-8: {error.reason}") from error
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise mapping_error("invalid-json", f"line {error.lineno} column {error.colno}: {error.msg}") from error
    validate(data)
    return data

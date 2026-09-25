"""Install units (Design Doc F2 "Unit order", F1 payload includes): the manifest `plan` stages, and the tree hash.

A unit is one thing the installer writes into a scope, named by its scope-relative `target_rel`:

- `dir`: one per `mapping.json` payload, `<payload_namespace>/<id>`, holding each `include` path at its path relative
  to the plugin dir. `__pycache__/`, `evals/` and `*.pyc` are always left out, and so is every path in the entry's own
  `exclude`. A symlink or special file in a payload rejects the unit.
- `file`: a plugin `.ts` (`plugins/<basename>`), a hand-written `opencode-*.md` agent (`agents/<basename>`) or a
  command (`commands/<basename>`), copied verbatim.

Units sort `dir` first, then `file` units by kind (plugins, agents, commands); within a kind by plugin id, then
`target_rel`. Staging copies a unit to `<stage>/<target_rel>`, exec bits included.

`tree_sha256` is the one tree hash: sha256 over the sorted `<relpath>\\0<exec bit 0|1>\\0<sha256 of file>\\n` entries of
the regular files below a dir. A symlink is hashed as `<relpath>\\0l\\0<sha256 of its target text>\\n` and any other
entry (fifo, socket, device) as `<relpath>\\0o\\0-\\n`. Neither is followed or read, and a staged tree holds regular
files only, so a target holding one never hashes equal to a staged unit.

Stdlib only; runs on Python 3.10 and later.
"""

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

import guard
import mapping

DIR_UNIT = "dir"
FILE_UNIT = "file"
HASH_PREFIX = "sha256:"
PAYLOAD_KIND = "payload"
PLUGINS_KIND = "plugins"
AGENTS_KIND = "agents"
# The `list` kinds that become file units, in install order; each installs into the scope dir of the same name.
FILE_KINDS = (PLUGINS_KIND, AGENTS_KIND, "commands")
ALWAYS_EXCLUDED_DIRS = frozenset(("__pycache__", "evals"))
ALWAYS_EXCLUDED_SUFFIXES = (".pyc",)
SYMLINK_FLAG = "l"
OTHER_FLAG = "o"
NO_DIGEST = "-"
READ_CHUNK_BYTES = 1 << 16
STAGE_RULE = "plan-stage"


class UnitRejected(mapping.ConvertError):
    """One unit cannot be staged; `plan` reports it on stderr and stages the other units (exit 3)."""


@dataclass(frozen=True)
class Unit:
    """One install unit; SOURCES holds (repo-relative source, unit-relative destination) pairs."""

    plugin: str
    unit: str
    kind: str
    target_rel: str
    sources: tuple
    excludes: tuple = ()

    @property
    def stage_rel(self):
        """Where the unit is staged below the stage dir: the stage mirrors the scope layout."""
        return self.target_rel

    @property
    def origin(self):
        """The repo-relative path that names this unit in messages."""
        return self.sources[0][0]

    def sort_key(self):
        rank = 0 if self.unit == DIR_UNIT else 1 + FILE_KINDS.index(self.kind)
        return (rank, self.plugin, self.target_rel)


# -- manifest -------------------------------------------------------------------


def payload_unit(plugin_id, relpaths, plugin_root, data):
    """The `dir` unit of PLUGIN_ID, holding each payload source at its path relative to PLUGIN_ROOT."""
    root = PurePosixPath(plugin_root)
    sources = tuple((relpath, PurePosixPath(relpath).relative_to(root).as_posix()) for relpath in sorted(relpaths))
    excludes = tuple(data["payloads"][plugin_id].get("exclude", []))
    target_rel = f"{data['payload_namespace']}/{plugin_id}"
    return Unit(plugin_id, DIR_UNIT, PAYLOAD_KIND, target_rel, sources, excludes)


def file_unit(plugin_id, kind, relpath):
    """A verbatim `file` unit: <kind>/<basename>."""
    return Unit(plugin_id, FILE_UNIT, kind, f"{kind}/{PurePosixPath(relpath).name}", ((relpath, ""),))


def _installs_as_file(kind, relpath, data):
    """Plugins and commands always; agents only when hand-written (Claude-format agents are converted later)."""
    if kind not in FILE_KINDS:
        return False
    return kind != AGENTS_KIND or PurePosixPath(relpath).name.startswith(data["hand_written_agent_prefix"])


def _check_unique_targets(units):
    claimed = {}
    for unit in units:
        other = claimed.setdefault(unit.target_rel, unit)
        if other is not unit:
            raise mapping.ConvertError(unit.origin, "duplicate-target",
                                       f"{unit.target_rel} is also the target of {other.origin}")


def build_units(sources, data, plugin_roots):
    """The sorted units for SOURCES (`convert.py list` rows); PLUGIN_ROOTS maps an id to its plugin dir relpath.

    Two units with one target_rel are fatal: neither could be installed without overwriting the other.
    """
    payloads = {}
    units = []
    for plugin_id, kind, relpath in sources:
        if kind == PAYLOAD_KIND:
            payloads.setdefault(plugin_id, []).append(relpath)
        elif _installs_as_file(kind, relpath, data):
            units.append(file_unit(plugin_id, kind, relpath))
    units += [payload_unit(plugin_id, relpaths, plugin_roots[plugin_id], data)
              for plugin_id, relpaths in payloads.items()]
    _check_unique_targets(units)
    return sorted(units, key=Unit.sort_key)


# -- staging --------------------------------------------------------------------


def check_stage(stage, forbidden):
    """The realpath of STAGE, which must be an existing empty dir outside every (label, roots) pair in FORBIDDEN."""
    real = os.path.realpath(stage)
    if not os.path.isdir(real):
        raise mapping.ConvertError(stage, STAGE_RULE, "not a directory")
    if os.listdir(real):
        raise mapping.ConvertError(stage, STAGE_RULE, "not empty")
    for label, roots in forbidden:
        if any(guard.is_within(real, root) for root in roots):
            raise mapping.ConvertError(stage, STAGE_RULE, f"inside {label}")
    return real


def _is_excluded(unit_rel, name, is_dir, excludes):
    if name in ALWAYS_EXCLUDED_DIRS and is_dir:
        return True
    if name.endswith(ALWAYS_EXCLUDED_SUFFIXES) and not is_dir:
        return True
    return any(unit_rel == exclude or unit_rel.startswith(f"{exclude}/") for exclude in excludes)


def _copy_payload(source, dest, unit_rel, excludes, where):
    """Copy SOURCE (a regular file or a dir) to DEST without its excluded entries."""
    if os.path.islink(source) or not (os.path.isfile(source) or os.path.isdir(source)):
        raise UnitRejected(where, "payload-entry", "a payload holds regular files and dirs only, not a symlink or "
                                                   "special file")
    if os.path.isfile(source):
        shutil.copy2(source, dest)
        return
    os.makedirs(dest, exist_ok=True)
    for name in sorted(os.listdir(source)):
        child = os.path.join(source, name)
        child_rel = f"{unit_rel}/{name}"
        if _is_excluded(child_rel, name, os.path.isdir(child) and not os.path.islink(child), excludes):
            continue
        _copy_payload(child, os.path.join(dest, name), child_rel, excludes, f"{where}/{name}")


def stage_unit(repo, unit, stage):
    """Copy UNIT from REPO to <STAGE>/<stage_rel> and return its `sha256:<hex>`.

    Raises UnitRejected for a payload entry that is not a regular file or dir; a partial copy is removed first.
    """
    dest = os.path.join(stage, unit.stage_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if unit.unit == FILE_UNIT:
        shutil.copy2(os.path.join(repo, unit.origin), dest)
        return HASH_PREFIX + file_sha256(dest)
    os.makedirs(dest)
    try:
        for source_rel, unit_rel in unit.sources:
            os.makedirs(os.path.dirname(os.path.join(dest, unit_rel)), exist_ok=True)
            _copy_payload(os.path.join(repo, source_rel), os.path.join(dest, unit_rel), unit_rel, unit.excludes,
                          source_rel)
    except UnitRejected:
        shutil.rmtree(dest)
        raise
    return HASH_PREFIX + tree_sha256(dest)


# -- hashing --------------------------------------------------------------------


def file_sha256(path):
    """The sha256 hex digest of the file at PATH."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_entry(path, relpath):
    """(relpath, flag, digest) for one entry; only a regular file is read, a symlink is never followed."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return relpath, SYMLINK_FLAG, hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
    if stat.S_ISREG(info.st_mode):
        return relpath, "1" if info.st_mode & stat.S_IXUSR else "0", file_sha256(path)
    return relpath, OTHER_FLAG, NO_DIGEST


def _raise(error):
    raise error


def tree_sha256(path):
    """The tree hash of the dir at PATH (see the module docstring); an unreadable subdir raises OSError."""
    entries = []
    for root, dirs, files in os.walk(path, onerror=_raise):
        rel_root = os.path.relpath(root, path).replace(os.sep, "/")
        linked_dirs = [name for name in dirs if os.path.islink(os.path.join(root, name))]
        for name in files + linked_dirs:
            relpath = name if rel_root == "." else f"{rel_root}/{name}"
            entries.append(_tree_entry(os.path.join(root, name), relpath))
    lines = sorted(b"\0".join(os.fsencode(field) for field in entry) + b"\n" for entry in entries)
    return hashlib.sha256(b"".join(lines)).hexdigest()


def path_sha256(path):
    """`sha256:<hex>` of PATH: the file hash of a file, the tree hash of a dir (both through a symlinked PATH)."""
    try:
        if os.path.isdir(path):
            return HASH_PREFIX + tree_sha256(path)
        if os.path.isfile(path):
            return HASH_PREFIX + file_sha256(path)
    except OSError as error:
        raise mapping.ConvertError(path, "hash", f"unreadable: {error.strerror or error}") from error
    problem = "not a file or directory" if os.path.lexists(path) else "no such file or directory"
    raise mapping.ConvertError(path, "hash", problem)

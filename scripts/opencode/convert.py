#!/usr/bin/env python3
"""opencode converter CLI (Design Doc "Interface Contracts": Converter CLI).

  python3 scripts/opencode/convert.py list  --repo DIR [--plugin NAME]...
  python3 scripts/opencode/convert.py plan  --repo DIR --scope-root DIR --scope global|project --stage DIR
                                            [--project-dir DIR] [--plugin ID]... [--allow-repo DIR]...
  python3 scripts/opencode/convert.py hash  PATH...
  python3 scripts/opencode/convert.py guard --scope-root DIR --op write|delete [--allow-repo DIR]... PATH...
  python3 scripts/opencode/convert.py tracker merge --tracker FILE --add ROWS [--remove ROWS] [--header KEY=VALUE...]
  python3 scripts/opencode/convert.py tracker read  --tracker FILE [--plugin ID]

stdout carries data rows only; WARN and ERROR lines go to stderr as `WARN|ERROR path[:line]: rule: message`.
Exit codes: 0 success, 1 fatal error, 2 usage error (argparse, or a `list --plugin` NAME that names no plugin),
3 `plan` rejected some units (the others are staged), 4 a guard target is blocked or needs approval.

Two additive helpers go beyond the Design Doc contract table: `hash`, so the installer never re-implements the tree
hash, and `list --plugin NAME`, the one place a dir-derived plugin name (plugin-<NAME>) maps to its plugin.json id.

Stdlib only; runs on Python 3.10 and later.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import guard
import mapping
import tracker
import units

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_GUARD = 4
PLAN_COLUMNS = ("plugin", "unit", "stage_rel", "target_rel", "hash", "realpath", "repo", "verdict")
SCOPES = ("global", "project")

# `list` kinds in unit order, the secondary sort key after the plugin id.
KINDS = ("payload", "plugins", "skills", "agents", "commands", "config")
CONFIG_FILES = (".lsp.json", ".mcp.json")
MANIFEST_RELPATH = Path(".claude-plugin") / "plugin.json"
PLUGIN_DIR_PREFIX = "plugin-"
PLUGIN_DIR_GLOB = f"{PLUGIN_DIR_PREFIX}*"


# -- list -----------------------------------------------------------------------


def _relpath(repo, path):
    return path.relative_to(repo).as_posix()


def read_plugin_id(repo, manifest):
    """The plugin id: the `name` in a .claude-plugin/plugin.json (D003)."""
    where = _relpath(repo, manifest)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as error:
        raise mapping.ConvertError(where, "manifest-unreadable", error.strerror or str(error)) from error
    except json.JSONDecodeError as error:
        raise mapping.ConvertError(f"{where}:{error.lineno}", "manifest-invalid", error.msg) from error
    except UnicodeDecodeError as error:
        raise mapping.ConvertError(where, "manifest-invalid", f"not UTF-8: {error.reason}") from error
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        raise mapping.ConvertError(where, "manifest-name", '"name" must be a non-empty string')
    return name


def discover_plugin_dirs(repo):
    """Map each plugin id to its plugin-* dir; a dir without a manifest is not a plugin."""
    plugins = {}
    for manifest in sorted(repo.glob(f"{PLUGIN_DIR_GLOB}/{MANIFEST_RELPATH.as_posix()}")):
        plugin_id = read_plugin_id(repo, manifest)
        plugin_dir = manifest.parent.parent
        if plugin_id in plugins:
            raise mapping.ConvertError(_relpath(repo, manifest), "duplicate-plugin-id",
                                       f"{plugin_id!r} is also the id of {_relpath(repo, plugins[plugin_id])}")
        plugins[plugin_id] = plugin_dir
    return plugins


def _files(directory, pattern):
    return [path for path in sorted(directory.glob(pattern)) if path.is_file()]


def _dirs(directory, pattern):
    return [path for path in sorted(directory.glob(pattern)) if path.is_dir()]


def _agent_sources(repo, plugin_dir, data):
    """Hand-written opencode-X.md agents, plus Claude-format agents minus their twins and exclude_agents."""
    prefix = data["hand_written_agent_prefix"]
    excluded = set(data["exclude_agents"])
    agents = _files(plugin_dir / "agents", "*.md")
    names = {path.name for path in agents}
    selected = []
    for path in agents:
        if _relpath(repo, path) in excluded:
            continue
        if path.name.startswith(prefix) or f"{prefix}{path.name}" not in names:
            selected.append(path)
    return selected


def _payload_sources(repo, plugin_id, plugin_dir, payload):
    sources = []
    for include in payload["include"]:
        source = plugin_dir / include
        if not source.exists():
            raise mapping.ConvertError(mapping.LABEL, "payload-include-missing",
                                       f"/payloads/{plugin_id}/include names {_relpath(repo, source)}, which does "
                                       "not exist")
        sources.append(source)
    return sources


def _plugin_dir_sources(repo, plugin_id, plugin_dir, data):
    """(kind, path) pairs for one plugin-* dir."""
    payload = data["payloads"].get(plugin_id)
    sources = [("plugins", path) for path in _files(plugin_dir / "plugins", "*.ts")]
    sources += [("skills", path) for path in _dirs(plugin_dir / "skills", "*")]
    sources += [("agents", path) for path in _agent_sources(repo, plugin_dir, data)]
    sources += [("commands", path) for path in _files(plugin_dir / "commands", "*.md")]
    if payload is not None:
        sources += [("payload", path) for path in _payload_sources(repo, plugin_id, plugin_dir, payload)]
        sources += [("config", plugin_dir / name) for name in CONFIG_FILES if (plugin_dir / name).is_file()]
    return sources


def _extra_sources(repo, source_key, source):
    """The plugin id and (kind, path) pairs of one `extra_sources` entry (wandavision)."""
    plugin_id = read_plugin_id(repo, repo / source["manifest"])
    if plugin_id != source_key:
        raise mapping.ConvertError(mapping.LABEL, "extra-source-id",
                                   f"/extra_sources/{source_key} names manifest {source['manifest']}, whose name is "
                                   f"{plugin_id!r}")
    pairs = [("plugins", path) for pattern in source.get("plugins", []) for path in _files(repo, pattern)]
    pairs += [("skills", path) for pattern in source.get("skills", []) for path in _dirs(repo, pattern)]
    return plugin_id, pairs


def list_sources(repo, data):
    """Sorted (id, kind, relpath) rows, one per install source (Design Doc F3 "Discovery")."""
    excluded = set(data["exclude_plugins"])
    plugin_dirs = {key: path for key, path in discover_plugin_dirs(repo).items() if key not in excluded}
    unknown = sorted(set(data["payloads"]) - set(plugin_dirs))
    if unknown:
        raise mapping.ConvertError(mapping.LABEL, "payload-unknown-plugin",
                                   f"/payloads names {', '.join(unknown)}, not a plugin id in {repo}")
    rows = set()
    for plugin_id, plugin_dir in plugin_dirs.items():
        for kind, path in _plugin_dir_sources(repo, plugin_id, plugin_dir, data):
            rows.add((plugin_id, kind, _relpath(repo, path)))
    for source_key, source in data["extra_sources"].items():
        plugin_id, pairs = _extra_sources(repo, source_key, source)
        if plugin_id not in excluded:
            rows.update((plugin_id, kind, _relpath(repo, path)) for kind, path in pairs)
    return sorted(rows, key=lambda row: (row[0], KINDS.index(row[1]), row[2]))


class PluginUnknown(mapping.ConvertError):
    """A --plugin NAME that is neither a listed plugin id nor the dir-derived name of one (exit 2)."""


def dir_aliases(plugin_dirs):
    """{name: id} for each plugin-<name> dir, read from the dirs at run time (Interface Change Matrix "Dir-name
    alias"); a name equal to its id never gets a WARN."""
    return {plugin_dir.name[len(PLUGIN_DIR_PREFIX):]: plugin_id for plugin_id, plugin_dir in plugin_dirs.items()}


def resolve_plugins(names, known_ids, aliases):
    """(ids, warnings) for the --plugin NAMES: an id is used as is and wins over a dir-derived name, which maps to
    its id with one `WARN: use <id>` however often it is given. Raises PluginUnknown for any other name."""
    ids, warnings = set(), []
    for name in names:
        plugin_id = name if name in known_ids else aliases.get(name)
        if plugin_id not in known_ids:
            raise PluginUnknown(f"--plugin {name!r}", "plugin-unknown", "names no OpenCode-compatible plugin; plugin "
                                f"ids: {', '.join(sorted(known_ids))}")
        warning = f"WARN: use {plugin_id}"
        if plugin_id != name and warning not in warnings:
            warnings.append(warning)
        ids.add(plugin_id)
    return ids, warnings


def run_list(args):
    """Print the rows of every plugin, or only of the --plugin selection (a WARN per dir-derived name)."""
    repo = Path(args.repo).absolute()
    data = mapping.load(mapping.repo_mapping_path(repo))
    rows = list_sources(repo, data)
    if args.plugin:
        try:
            ids, warnings = resolve_plugins(args.plugin, {row[0] for row in rows},
                                            dir_aliases(discover_plugin_dirs(repo)))
        except PluginUnknown as error:
            print(error, file=sys.stderr)
            return EXIT_USAGE
        for warning in warnings:
            print(warning, file=sys.stderr)
        rows = [row for row in rows if row[0] in ids]
    for row in rows:
        print("|".join(row))
    return EXIT_OK


# -- plan -----------------------------------------------------------------------


def _check_plan_args(args):
    if args.scope == "project" and args.project_dir is None:
        args.parser.error("--scope project needs --project-dir")
    if args.scope == "global" and args.project_dir is not None:
        args.parser.error("--project-dir is only for --scope project")


def _selected_sources(args, sources):
    """SOURCES limited to the --plugin ids; an id that names no plugin with install sources is a usage error."""
    known = sorted({row[0] for row in sources})
    for plugin_id in args.plugin:
        if plugin_id not in known:
            args.parser.error(f"unknown plugin id {plugin_id!r}; plugin ids: {', '.join(known)}")
    return [row for row in sources if not args.plugin or row[0] in args.plugin]


def _plan_row(unit, digest, verdict):
    columns = (unit.plugin, unit.unit, unit.stage_rel, unit.target_rel, digest, verdict.realpath, verdict.repo,
               verdict.verdict)
    for name, value in zip(PLAN_COLUMNS, columns):
        if tracker.has_line_break(value):
            raise mapping.ConvertError("plan", "plan-column", f'the "{name}" column of a unit has a control character')
    return "\t".join(columns)


def _stage(repo, unit, stage):
    """units.stage_unit, with a failed copy into the stage as a fatal error."""
    try:
        return units.stage_unit(repo, unit, stage)
    except OSError as error:
        raise mapping.ConvertError(stage, "stage-failed", error.strerror or str(error)) from error


def _stage_units(repo, unit_list, stage, checker, scope_root):
    """Stage each unit, then print its row with the guard verdict for its target; rejected units are skipped.

    A plugin `.ts` whose payload unit was rejected is rejected too: it would run without its payload.
    """
    exit_code = EXIT_OK
    rejected_payloads = set()
    for unit in unit_list:
        if unit.kind == units.PLUGINS_KIND and unit.plugin in rejected_payloads:
            print(mapping.ConvertError(unit.origin, "payload-rejected", f"the payload unit of {unit.plugin} was "
                                       "rejected"), file=sys.stderr)
            exit_code = EXIT_REJECTED
            continue
        try:
            digest = _stage(repo, unit, stage)
        except units.UnitRejected as error:
            print(error, file=sys.stderr)
            rejected_payloads.add(unit.plugin)
            exit_code = EXIT_REJECTED
            continue
        verdict = checker.check(os.path.join(scope_root, unit.target_rel))
        if verdict.reason:
            print(f"WARN {verdict.realpath}: {verdict.rule}: {verdict.reason}", file=sys.stderr)
        print(_plan_row(unit, digest, verdict))
    return exit_code


def run_plan(args):
    """Stage the manifest's units and print one row per unit (Design Doc "Interface Contracts": plan)."""
    _check_plan_args(args)
    repo = Path(args.repo).absolute()
    data = mapping.load(mapping.repo_mapping_path(repo))
    sources = _selected_sources(args, list_sources(repo, data))
    plugin_roots = {plugin_id: _relpath(repo, path) for plugin_id, path in discover_plugin_dirs(repo).items()}
    unit_list = units.build_units(sources, data, plugin_roots)
    policy = guard.GuardPolicy.from_mapping(data["guard"], args.scope_root, args.allow_repo)
    scope_roots = guard.containment_roots(args.scope_root, policy.writable_scope_dirs)
    stage = units.check_stage(args.stage, (("the repo", [os.path.realpath(repo)]), ("the scope", scope_roots)))
    return _stage_units(repo, unit_list, stage, guard.Guard(policy), args.scope_root)


# -- hash -----------------------------------------------------------------------


def run_hash(args):
    """Print `sha256:<hex><TAB>PATH` per PATH, once every PATH has hashed (a missing one prints nothing)."""
    _check_guard_paths(args.paths)
    digests = [(units.path_sha256(path), path) for path in args.paths]
    for digest, path in digests:
        print(f"{digest}\t{path}")
    return EXIT_OK


# -- guard ----------------------------------------------------------------------


def _check_guard_paths(paths):
    for path in paths:
        if "\t" in path or "\n" in path:
            raise mapping.ConvertError(repr(path), "guard-path", "a PATH must not contain a tab or a newline")


def run_guard(args):
    _check_guard_paths(args.paths)
    data = mapping.load()
    policy = guard.GuardPolicy.from_mapping(data["guard"], args.scope_root, args.allow_repo)
    checker = guard.Guard(policy)
    exit_code = EXIT_OK
    for path in args.paths:
        verdict = checker.check(path)
        print(verdict.row())
        if verdict.reason:
            print(f"WARN {verdict.realpath}: {verdict.rule}: {verdict.reason}", file=sys.stderr)
        if not verdict.ok:
            exit_code = EXIT_GUARD
    return exit_code


# -- tracker ----------------------------------------------------------------------


def _tracker_scope(tracker_path):
    """The tracker's scope: its own directory and the writable scope dirs from mapping.json."""
    return tracker.Scope.of_tracker(tracker_path, mapping.load()["guard"]["writable_scope_dirs"])


def run_tracker_read(args):
    """Print the rows inside the scope; a kept row (realpath outside the roots) only gets a WARN on stderr."""
    state = tracker.load(args.tracker, _tracker_scope(args.tracker))
    for warning in state.warnings():
        print(warning, file=sys.stderr)
    for row in state.sorted_rows(args.plugin):
        print(row.line())
    return EXIT_OK


def run_tracker_merge(args):
    """Validate the stored tracker and every input first, then write once (or delete when no rows remain)."""
    scope = _tracker_scope(args.tracker)
    state = tracker.load(args.tracker, scope)
    add_rows = tracker.read_rows_file(args.add, scope)
    remove_keys = tracker.read_remove_keys(args.remove, scope) if args.remove is not None else set()
    header_updates = tracker.parse_header_args(args.header)
    tracker.save(args.tracker, tracker.merge(state, add_rows, remove_keys, header_updates))
    return EXIT_OK


def _add_tracker_parser(commands):
    tracker_parser = commands.add_parser("tracker", help="read or merge the F4 tracker v2")
    subcommands = tracker_parser.add_subparsers(dest="tracker_command", metavar="SUBCOMMAND")
    subcommands.required = True

    merge_parser = subcommands.add_parser("merge", help="merge ROWS into the tracker and write it atomically")
    merge_parser.add_argument("--tracker", required=True, metavar="FILE", help="<scope root>/.opencode-setup-tracker")
    merge_parser.add_argument("--add", required=True, metavar="ROWS", help="a file of 6-column rows to add or replace")
    merge_parser.add_argument("--remove", metavar="ROWS", help="a file of rows to remove by (type, path)")
    merge_parser.add_argument("--header", action="extend", nargs="+", default=[], metavar="KEY=VALUE",
                              help="a header value to set; unset keys keep their stored value")
    merge_parser.set_defaults(handler=run_tracker_merge)

    read_parser = subcommands.add_parser("read", help="print the tracker as normalized v2 rows")
    read_parser.add_argument("--tracker", required=True, metavar="FILE", help="<scope root>/.opencode-setup-tracker")
    read_parser.add_argument("--plugin", metavar="ID", help="print only the rows of this plugin.json id")
    read_parser.set_defaults(handler=run_tracker_read)


def _add_plan_parser(commands):
    plan_parser = commands.add_parser("plan", help="stage the install units and print one row per unit")
    plan_parser.add_argument("--repo", required=True, metavar="DIR", help="the llm-agent-workflow checkout")
    plan_parser.add_argument("--scope-root", required=True, metavar="DIR", help="the opencode scope root")
    plan_parser.add_argument("--scope", required=True, choices=SCOPES)
    plan_parser.add_argument("--stage", required=True, metavar="DIR",
                             help="an empty dir outside the repo and the scope; units are staged below it")
    plan_parser.add_argument("--project-dir", metavar="DIR", help="the project dir (--scope project only)")
    plan_parser.add_argument("--plugin", action="append", default=[], metavar="ID",
                             help="stage only this plugin.json id (repeatable)")
    plan_parser.add_argument("--allow-repo", action="append", default=[], metavar="DIR",
                             help="a submodule toplevel approved for writes (answers G3 only)")
    plan_parser.set_defaults(handler=run_plan, parser=plan_parser)


# -- CLI ------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="convert.py", description="Convert llm-agent-workflow plugins for opencode.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    list_parser = commands.add_parser("list", help="print id|kind|relpath per install source")
    list_parser.add_argument("--repo", required=True, metavar="DIR", help="the llm-agent-workflow checkout")
    list_parser.add_argument("--plugin", action="append", default=[], metavar="NAME",
                             help="print only this plugin.json id, or plugin-<NAME> dir, with a WARN (repeatable)")
    list_parser.set_defaults(handler=run_list)

    _add_plan_parser(commands)

    hash_parser = commands.add_parser("hash", help="print sha256:<hex><TAB>PATH per PATH (file hash or tree hash)")
    hash_parser.add_argument("paths", nargs="+", metavar="PATH")
    hash_parser.set_defaults(handler=run_hash)

    guard_parser = commands.add_parser("guard", help="print the F8 repository guard verdict per PATH")
    guard_parser.add_argument("--scope-root", required=True, metavar="DIR", help="the opencode scope root")
    guard_parser.add_argument("--op", required=True, choices=("write", "delete"),
                              help="the operation the PATHs are checked for (same rules)")
    guard_parser.add_argument("--allow-repo", action="append", default=[], metavar="DIR",
                              help="a submodule toplevel approved for writes (answers G3 only)")
    guard_parser.add_argument("paths", nargs="+", metavar="PATH")
    guard_parser.set_defaults(handler=run_guard)

    _add_tracker_parser(commands)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except mapping.ConvertError as error:
        print(error, file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""ruby-lsp PostToolUse hook: advisory Reek smells after a Ruby edit.

Reek has no LSP, so this hook fills the gap. After Write/Edit/MultiEdit on a
Ruby file it runs reek through scripts/run-ruby-tool.sh (Docker first, host
fallback) and hands the smells back to Claude as PostToolUse
`additionalContext`.

Advisory only: always exits 0. No smells -> no output, zero tokens spent.
Failures (tool missing, timeout, bad JSON) print one stderr line and never
block the edit.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

MAX_SMELLS = 10
TIMEOUT_SECONDS = 60
RUBY_SUFFIXES = (".rb", ".rake")
SKIP_PARTS = ("db/migrate/", "vendor/", "spec/", "test/", "node_modules/")
SKIP_FILES = ("db/schema.rb",)
PROJECT_REEK_CONFIGS = (".reek.yml", "config.reek", ".reek")

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
WRAPPER = PLUGIN_ROOT / "scripts" / "run-ruby-tool.sh"
BUNDLED_CONFIG = PLUGIN_ROOT / "config" / ".reek.yml"


def log(message):
    print(f"[ruby-lsp] {message}", file=sys.stderr)


def find_project_root(file_path):
    """Nearest ancestor holding a Gemfile, else CLAUDE_PROJECT_DIR, else the file's dir."""
    for parent in file_path.parents:
        if (parent / "Gemfile").is_file():
            return parent
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env_root) if env_root else file_path.parent


def relative_to_root(file_path, root):
    try:
        return file_path.relative_to(root).as_posix()
    except ValueError:
        return file_path.as_posix()


def should_check(rel_path):
    if not rel_path.endswith(RUBY_SUFFIXES):
        return False
    if rel_path in SKIP_FILES:
        return False
    return not any(rel_path.startswith(part) or f"/{part}" in rel_path for part in SKIP_PARTS)


def build_command(file_path, root):
    cmd = ["bash", str(WRAPPER), "reek", "--format", "json", "--no-progress"]
    has_project_config = any((root / name).is_file() for name in PROJECT_REEK_CONFIGS)
    if not has_project_config and BUNDLED_CONFIG.is_file():
        cmd += ["-c", str(BUNDLED_CONFIG)]
    cmd.append(str(file_path))
    return cmd


def parse_smells(stdout):
    """Reek JSON is a list of smell dicts. Returns None when output is not that shape."""
    try:
        data = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def format_smells(smells, rel_path):
    lines = []
    for smell in smells[:MAX_SMELLS]:
        line_nums = smell.get("lines") or ["?"]
        smell_type = smell.get("smell_type", "Smell")
        context = smell.get("context", "")
        message = smell.get("message", "")
        where = f"{context} " if context else ""
        lines.append(f"- {rel_path}:{line_nums[0]} {smell_type}: {where}{message}")
    extra = len(smells) - MAX_SMELLS
    if extra > 0:
        lines.append(f"- (+{extra} more)")
    header = (
        f"[ruby-lsp] Reek (advisory) found {len(smells)} smell(s) in {rel_path}. "
        "Fix the ones in code you just wrote; leave pre-existing ones unless asked."
    )
    return "\n".join([header, *lines])


def hint_once(session_id, message):
    """Print an install hint at most once per session."""
    state_dir = Path.home() / ".claude" / ".ruby-lsp-plugin"
    marker = state_dir / f"hinted-{session_id or 'unknown'}"
    if marker.exists():
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass
    log(message)


def emit(text):
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}
    }))


def run(data):
    raw_path = (data.get("tool_input") or {}).get("file_path") or ""
    if not raw_path:
        return
    file_path = Path(raw_path).resolve()
    root = find_project_root(file_path)
    rel_path = relative_to_root(file_path, root)
    if not should_check(rel_path) or not file_path.is_file():
        return

    try:
        result = subprocess.run(
            build_command(file_path, root),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log(f"reek timed out after {TIMEOUT_SECONDS}s on {rel_path}")
        return

    # reek exit codes: 0 = clean, 2 = smells found, 1 = error. 127 = wrapper found no reek.
    if result.returncode == 127:
        hint_once(data.get("session_id"), "reek not installed; Reek advisories disabled. "
                  "Add `gem 'reek', require: false` to the :development group.")
        return
    if result.returncode not in (0, 2):
        log(f"reek exit {result.returncode}: {result.stderr.strip()[-200:]}")
        return

    smells = parse_smells(result.stdout)
    if smells is None:
        log("reek returned unparseable output")
        return
    if smells:
        emit(format_smells(smells, rel_path))


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)
    try:
        run(data)
    except Exception as exc:  # advisory hook: never block an edit
        log(f"error: {exc}")
    sys.exit(0)


if __name__ == "__main__":
    main()

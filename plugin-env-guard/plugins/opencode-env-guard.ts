import type { Plugin } from "@opencode-ai/plugin"

/**
 * opencode-env-guard plugin.
 *
 * Silent gate that blocks OpenCode from reading, writing, or editing
 * sensitive credential files (.env, SSH keys, cloud credentials, tokens)
 * and from running shell commands that print or read secrets.
 *
 * OpenCode port of the Claude Code PreToolUse hook
 * (plugin-env-guard/hooks/env_guard_hook.py):
 *   - `tool.execute.before` for `read|write|edit` -> was `Read|Write|Edit|MultiEdit` matcher branch
 *   - `tool.execute.before` for `bash`            -> was `Bash` matcher branch
 *   - `throw new Error(...)`                      -> was `sys.exit(2)` block
 *   - `client.app.log(...)` on allow-listed hits  -> was silent `sys.exit(0)`
 *
 * OpenCode has no MultiEdit — patches use `write` or `edit`, so those two
 * tools carry the full Read/Write/Edit/MultiEdit enforcement.
 */

// ---------------------------------------------------------------------------
// Sensitive FILE path patterns (regex applied to the resolved file path)
// ---------------------------------------------------------------------------
// Each pattern is tested against the full file path (forward-slash normalized).
// Explicit allow-list exceptions (.env.example, .env.sample) are checked first.

const ALLOWED_FILE_PATTERNS = [
  // Template / example dotenv files are safe — they contain no real secrets
  /(^|\/)\.env\.(example|sample|template)$/,
  /(^|\/)\.env\.example\./,
]

const SENSITIVE_FILE_PATTERNS = [
  // dotenv files: .env, .env.local, .env.production, app.env, etc.
  /(^|\/)\.env($|\.(?!example|sample|template))/,
  /(^|\/)[^/]+\.env$/,
  // Cloud credentials
  /(^|\/)credentials(\.json)?$/,
  /\.aws\/credentials$/,
  /\.azure\/credentials$/,
  /(^|\/)service[_-]account.*\.json$/,
  /gcloud\/.*credentials/,
  // SSH private keys
  /\.ssh\/(id_rsa|id_dsa|id_ecdsa|id_ed25519|id_ed25519_sk|id_ecdsa_sk)$/,
  /(^|\/)id_rsa$/,
  /\.(pem|key)$/,
  // TLS / PKI
  /\.(pfx|p12)$/,
  /(^|\/)(server|private)\.key$/,
  // Token / auth files
  /(^|\/)\.netrc$/,
  /(^|\/)\.pypirc$/,
  /(^|\/)\.htpasswd$/,
  /(^|\/)(auth|token)\.json$/,
  /(^|\/)[^/]+\.token$/,
  // Secret config files
  /(^|\/)secrets?\.(json|ya?ml|txt)$/,
  /(^|\/)\.secrets?$/,
  // Database credential files
  /(^|\/)\.pgpass$/,
  /(^|\/)pgpass$/,
  // Terraform variable files (may contain secrets)
  /(^|\/)terraform\.tfvars$/,
  /(^|\/)[^/]+\.tfvars$/,
  /(^|\/)override\.tf$/,
  // Key stores
  /\.(jks|keystore)$/,
  // Shell history (can contain secrets from past commands)
  /(^|\/)\.(bash|zsh|fish|python|node_repl)_history$/,
  // Docker / k8s secret overlays
  /(^|\/)docker-compose\.override\.ya?ml$/,
  /[_-]secrets?\.ya?ml$/,
]

// ---------------------------------------------------------------------------
// Sensitive BASH command patterns (regex applied to the command text)
// ---------------------------------------------------------------------------
// These commands print environment variables or read credential files directly.
// Each tuple is (pattern, reason); the reason is surfaced in the block message.
// Matches use re.IGNORECASE | re.MULTILINE semantics, as in the Python hook.

const SENSITIVE_BASH_PATTERNS: Array<[string, string]> = [
  ["^\\s*env\\s*$", "Running bare `env` prints all environment variables"],
  ["^\\s*printenv\\b", "`printenv` prints environment variables"],
  ["^\\s*set\\s*$", "Running bare `set` dumps shell variables including secrets"],
  ["\\bexport\\s+-p\\b", "`export -p` dumps all exported variables"],
  ["\\becho\\s+['\"]?\\$[A-Z_]{4,}", "`echo $UPPERCASE_VAR` may expose secret values"],
  ["\\bcat\\s+['\"]?[^\\s;|&]*\\.env\\b", "`cat` on a .env file exposes secrets"],
  ["\\bcat\\s+['\"]?~/\\.ssh/", "`cat` on SSH keys exposes private key material"],
  ["\\bcat\\s+['\"]?~/\\.aws/credentials", "`cat` on AWS credentials exposes keys"],
  ["\\bcat\\s+['\"]?~/\\.netrc\\b", "`cat` on .netrc exposes stored passwords"],
  ["\\bcat\\s+['\"]?[^\\s;|&]*credentials\\b", "`cat` on credentials file exposes secrets"],
  ["\\bcat\\s+['\"]?[^\\s;|&]*\\.pem\\b", "`cat` on a .pem file exposes private key material"],
  ["\\bcat\\s+['\"]?[^\\s;|&]*\\.key\\b", "`cat` on a .key file exposes private key material"],
]

const BLOCK_FILE_MESSAGE = (path: string) =>
  `[env-guard] BLOCKED: Access to '${path}' is not permitted.

This file matches a sensitive credential pattern. To protect secrets from
accidental exposure, env-guard prevents Claude from reading, writing, or
editing this file type.

If this is a false positive, the user can grant access by adding a
permissions.allow rule in .claude/settings.json:

  { "permissions": { "allow": ["Read(${path})"] } }`

const BLOCK_BASH_MESSAGE = (reason: string) =>
  `[env-guard] BLOCKED: The command may expose secrets or credentials.

Reason: ${reason}

Commands that print environment variables or read credential files are blocked.
If this operation is genuinely needed, the user should run it directly in a
terminal — outside of Claude Code.`

/** File-path-like args across OpenCode's file tools (read, write, edit). */
const FILE_PATH_ARGS = ["filePath", "file_path", "path", "filepath"]

/** Normalize Windows backslashes to forward slashes before regex matching. */
function normalizePath(p: string): string {
  return p.replace(/\\/g, "/")
}

function isAllowedPath(p: string): boolean {
  const normalized = normalizePath(p)
  return ALLOWED_FILE_PATTERNS.some((pattern) => pattern.test(normalized))
}

function isSensitivePath(p: string): boolean {
  const normalized = normalizePath(p)
  return SENSITIVE_FILE_PATTERNS.some((pattern) => pattern.test(normalized))
}

/** Returns the matching reason string, or null when the command is safe. */
function isSensitiveBash(command: string): string | null {
  for (const [pattern, reason] of SENSITIVE_BASH_PATTERNS) {
    // Mirrors re.search(pattern, command, re.IGNORECASE | re.MULTILINE)
    if (new RegExp(pattern, "im").test(command)) return reason
  }
  return null
}

export const EnvGuardPlugin: Plugin = async ({ client }) => {
  return {
    "tool.execute.before": async (input, output) => {
      const args = (output.args ?? {}) as Record<string, unknown>

      // --- File tools: read | write | edit (OpenCode has no MultiEdit) ---
      if (input.tool === "read" || input.tool === "write" || input.tool === "edit") {
        for (const key of FILE_PATH_ARGS) {
          const value = args[key]
          if (typeof value !== "string" || !value) continue

          // Explicit allow-list takes precedence (e.g. .env.example)
          if (isAllowedPath(value)) {
            await client.app.log({
              body: {
                service: "env-guard",
                level: "info",
                message: `Allowed access to '${value}' (template/example file, no real secrets)`,
              },
            })
            continue
          }

          if (isSensitivePath(value)) {
            throw new Error(BLOCK_FILE_MESSAGE(value))
          }
        }
        return
      }

      // --- Bash: check the command text ---
      if (input.tool === "bash") {
        const command = typeof args.command === "string" ? args.command : ""
        if (!command) return

        const reason = isSensitiveBash(command)
        if (reason) {
          throw new Error(BLOCK_BASH_MESSAGE(reason))
        }
      }
    },
  }
}

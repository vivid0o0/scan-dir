---
name: project-summarizer
description: Use this skill whenever you need to understand a project's structure, get a file inventory, or load full project context at the start of a coding task. Trigger when the user asks for a project overview, before refactors/moves/renames, when working in an unfamiliar repository, or any time you need to reason about project layout while conserving tokens. This skill runs the read-only `prs` CLI to scan a directory and print a compact tree with entry types, file sizes, line counts, modified times, git status markers, and a scan summary — full project awareness in one command without reading every file. Do NOT use it to inspect a single file's contents (read the file directly), search file contents (use grep/ripgrep), or analyze binary/media assets.
---

# Project Summarizer (`prs`)

`prs` is a read-only CLI that scans a project directory and prints a compact, information-rich tree: entry types, file sizes, line counts, modified times, git status markers, and a scan summary. One run gives the agent enough context to reason about structure, identify the largest or most recently changed files, and plan the work — without reading every file. Reuse that output throughout the task instead of re-scanning or reading files one by one.

`prs` is installed as the commands `prs` and `project-summarizer` (alias). Both are equivalent; prefer `prs` for brevity.

## When to use

Run `prs` once at the start of a task when any of these apply:

- The user asks for a project overview, file inventory, or layout summary.
- You are about to refactor, move, rename, or delete files and need to see what exists first.
- You are dropped into an unfamiliar repository and need to orient yourself.
- You need to identify the largest files, the most recently modified files, or the type distribution.
- You need to know which files have uncommitted git changes before editing.
- You want to load full project context in one command instead of N file reads.

## When NOT to use

- A single file's contents are needed — read the file directly; `prs` only shows metadata.
- Searching file contents (strings, symbols, patterns) — use `grep` / `rg` / AST tools.
- The project is a binary or media asset library (images, audio, video, compiled binaries) — `prs` reports sizes but cannot count lines for binaries.
- The working tree is enormous (millions of entries) and `--scan-timeout` would truncate the result before it is useful — narrow the scan with `--only` or `--ignore` first, or scope a subdirectory.
- You need the actual file contents of every file — `prs` is a metadata overview, not a content loader.

## Quick reference

| Task | Command |
| ---- | ------- |
| Scan current directory | `prs` |
| Scan a specific project | `prs ~/code/my-app` |
| Show only TypeScript files | `prs --only -e .ts .tsx` |
| Show only Markdown files (shorthand) | `prs --only .md` |
| Hide generated / vendored noise | `prs --ignore -e .min.js --ignore -n dist node_modules` |
| Ultra-compact tree for AI context | `prs --scan-styling minimal --scan-data "tree, summary"` |
| Tree only (no metadata) | `prs --scan-data "tree"` |
| Totals only (no tree) | `prs --scan-data "summary"` |
| Pipe a clean tree to another tool | `prs --scan-styling minimal --scan-emojis false --auto-copy false` |
| Show install / runtime info | `prs status` |
| Show full help | `prs help` |
| Show version | `prs version` |

## Commands

```bash
prs                       # scan the current directory
prs <path>                # scan a specific directory or file
prs help                  # full help (also -h, --help)
prs version               # version (also --version)
prs status                # runtime status: product, command, alias, version, app dir, python, config
```

`<path>` is optional and defaults to `.`. A path that collides with a command name (`help`, `version`, `status`) can be scanned with the `./` prefix, e.g. `prs ./status` scans a directory named `status`.

## Filter modes

Mutually exclusive — at most one per invocation:

| Mode | Behavior |
| ---- | -------- |
| `--ignore` | Exclude entries matching the selectors. This is the default mode whenever any selector (`-f`, `-t`, `-e`, `-n`) is present. |
| `--only` | Include only entries that match explicit CLI selectors. Config selectors are not applied — they neither add to nor override the include set. Shorthand values are accepted (see Selectors). |
| `--full` | Include every entry, including hidden (dot-prefixed) and empty ones. Equivalent to `--ignore-hidden=false --ignore-empty=false` plus no selector filtering. |

## Selectors

| Flag | Matches |
| ---- | ------- |
| `-f, --paths <paths...>` | Relative paths and everything inside matched directories. |
| `-t, --types <types...>` | Entry types: `file`, `dir`, `link`. |
| `-e, --extensions <ext...>` | Extensions such as `.ts`, `.json`, `.md` (leading dot required). |
| `-n, --names <names...>` | Exact file or directory basenames. |

### `--only` shorthand

Bare values passed to `--only` are auto-routed to the right selector based on their shape:

| Shorthand value | Inferred selector | Example |
| --------------- | ----------------- | ------- |
| `.md`, `.ts` (starts with `.`) | `--extensions` | `prs --only .md` |
| `.` (literal) | `--paths` (keeps the root) | `prs --only .` |
| `file`, `dir`, `link` | `--types` | `prs --only file` |
| `src/main.ts` (contains `/` or `\`) | `--paths` | `prs --only src/main.ts` |
| anything else (e.g. `src`, `README.md`) | `--names` | `prs --only src` |

Selector values that begin with `-` use the attached form, e.g. `--names=-draft`.

## Visibility

| Flag | Behavior |
| ---- | -------- |
| `--ignore-hidden` | Hide dot-prefixed files and directories (`.git`, `.env`, `.idea`, ...). |
| `--ignore-empty` | Hide empty files (0 bytes) and empty directories (no scanned children). |

Both default to off. They compose with `--ignore` / `--only` / `--full`.

## Rendering

| Flag | Values | Notes |
| ---- | ------ | ----- |
| `--scan-styling` | `full` \| `low` \| `minimal` | Controls layout structure only (color always follows terminal support). Affects token cost. |
| `--scan-emojis` | `true` \| `false` | Toggle file-type emojis (`📁`, `📄`, `🔗`) in entry names. |
| `--scan-data` | `"item, item, ..."` | Comma-separated subset of: `tree, lines, size, modified, type, git, summary`. Default is all seven. |

Styling levels:

- `full` — framed summary box with row separators. Best for humans in a terminal.
- `low` — plain summary block, no box. Middle ground.
- `minimal` — ASCII tree connectors (`+--`, `` `-- ``, `|`) and a plain summary. Best for AI agents and pipes; uses the fewest tokens.

For agent consumption, prefer `--scan-styling minimal` and `--scan-emojis false`: ASCII connectors are easier to parse and emojis add tokens without adding information.

## Runtime

| Flag | Values | Notes |
| ---- | ------ | ----- |
| `--scan-timeout` | `<seconds>` (float) | Stop scanning after the given time and print the partial result with a timeout notice. Default `60`. |
| `--auto-copy` | `true` \| `false` | Copy the plain (ANSI-stripped) scan output to the clipboard after rendering. Shipped config defaults to `false`; built-in (no config file) defaults to `true`. |

If the clipboard backend is unavailable (headless server, container, CI), `--auto-copy true` warns on stderr and continues; the scan output on stdout is unaffected. Pass `--auto-copy false` in non-interactive contexts to silence the warning.

## Configuration

| Flag | Notes |
| ---- | ----- |
| `--config <path>` | Use a specific `config.yaml` instead of the one next to the scan root. |

Config discovery order (first found wins): `--config <path>` → `$PWD/config.yaml` → `$XDG_CONFIG_HOME/project-summarizer/config.yaml` (user config, persists across installs) → `$APP_DIR/config.yaml` (installed default) → built-in defaults. The installer stages `config.yaml` alongside the runtime and repairs it on re-run if missing. User config in XDG_CONFIG_HOME is never touched by the installer.

When no CLI flags are passed, `prs` reads `config.yaml` next to the scan root. The file accepts the same keys as the CLI options; both kebab-case (`scan-styling`, `ignore-hidden`) and snake_case (`scan_styling`, `ignore_hidden`) are accepted. CLI flags override config values. The shipped `config.yaml` uses kebab-case and ships a default `names:` ignore list (`.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`, ...).

Selectors use per-category replacement: CLI `-n foo` replaces the config `names:` list rather than appending. Omit `tree` from `--scan-data` for a flat entry list without tree indentation; omit `summary` to hide the summary block.

## Git markers

When the scan root is inside a git repository and `git` is in `--scan-data` (default), entries are annotated with a single status marker. Because porcelain v1 packs index and worktree status into one XY pair, a path can carry several flags at once; `prs` picks one marker per entry using the precedence `?` > `D` > `R` > `A` > `M`:

| Marker | Meaning | Precedence | What to do |
| ------ | ------- | ---------- | ----------- |
| `[?]` | Untracked | highest | New file the user has not committed. Safe to read; ask before deleting. |
| `[D]` | Deleted | | File is tracked but missing from the working tree. Likely intentional removal; do not recreate without asking. |
| `[R]` | Renamed | | Path was moved. Update references to the old path. |
| `[A]` | Added | | Staged or recently added. New code; review before extending. |
| `[M]` | Modified | lowest (catch-all) | Uncommitted changes. Read the current content before editing; the working tree differs from HEAD. |

Entries tracked by git but deleted from the working tree are listed at the bottom of the tree (they have no on-disk location to nest under).

## Reading the output

The tree shows each entry on its own line with metadata columns, depending on which items are in `--scan-data`:

- **`tree`** — the indented tree itself. Connectors are `├──`, `└──`, `│` under `full`/`low` styling and `+--`, `` `-- ``, `|` under `minimal` styling. Indentation encodes the parent/child relationship.
- **`type`** (column header `entries`) — for a leaf, the entry type (`file` or `link`). For a directory, a roll-up like `9 files`, `3 files`, or `2 files, 1 dir` describing its scanned children.
- **`lines`** (column header `lines`) — line count for the entry. Files show `128L`; directories show the summed line count of their scanned children; links show `-` (their target is not followed). The summary's total line count gets a `+?` suffix (e.g. `2,671+? lines`) when some files could not be counted because they were binary or unreadable — the real total is strictly higher than the number shown.
- **`size`** (column header `size`) — human-readable size (`3.4 KB`) for files and dirs; `-` for links.
- **`modified`** (column header `modified`) — relative age of the entry's mtime (`2h ago`, `1d ago`, `now`).
- **`git`** — status marker appended to the entry name (see Git markers above).
- **`summary`** — a summary block at the bottom with totals (files, dirs, links, lines, size), the largest file, the newest entry, the file-type distribution, and the scan timing (`scanned in 14ms`, timestamp).

## Example output

```
example-app/                                       entries     lines       size        modified
├── src/                                           9 files     1,870L      54.8 KB     2h ago
│   ├── main.ts [M]                                file        128L        3.4 KB      2h ago
│   ├── app.ts                                     file        214L        6.8 KB      2h ago
│   ├── config.ts [A]                              file        76L         2.1 KB      1d ago
│   ├── components/                                3 files     563L        18.4 KB     4h ago
│   │   ├── Header.tsx                             file        146L        4.2 KB      4h ago
│   │   ├── ProjectCard.tsx [?]                    file        221L        7.6 KB      4h ago
│   │   └── Sidebar.tsx [M]                        file        196L        6.6 KB      6h ago
│   ├── lib/                                       2 files     577L        15.2 KB     3d ago
│   │   ├── scanner.ts                             file        389L        10.4 KB     3d ago
│   │   └── format.ts                              file        188L        4.8 KB      3d ago
│   └── styles.css                                 file        312L        8.9 KB      5d ago
├── public/                                        2 files     106L        6.4 KB      1w ago
│   ├── favicon.svg                                file        64L         2.8 KB      1w ago
│   └── manifest.json                              file        42L         3.6 KB      1w ago
├── tests/                                         2 files     463L        14.6 KB     2d ago
│   ├── scanner.test.ts                            file        301L        9.7 KB      2d ago
│   └── formatter.test.ts                          file        162L        4.9 KB      2d ago
├── package.json                                   file        48L         1.6 KB      1d ago
├── tsconfig.json                                  file        24L         0.7 KB      6d ago
├── build -> ./dist                                link        -           -           3d ago
├── .AGENTS.md                                     file        48L         1.6 KB      1d ago
└── README.md                                      file        112L        7.8 KB      1h ago

┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│  17 files    5 dirs    1 link    2,671 lines    85.9 KB                                     │
├──────────────────────┬──────────────────────────────────────────────────────────────────────┤
│ largest              │ scanner.ts                              10.4 KB                      │
│ newest               │ README.md                               1h ago                       │
│ types                │ ts: 7  tsx: 3  json: 3  md: 2  css: 1  svg: 1                        │
├──────────────────────┴──────────────────────────────────────────────────────────────────────┤
│ scanned in 14ms                  2026-02-21                    2:32 PM                      │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

The `full`-styling example above is what a human sees in a terminal. For agent context, the same scan with `--scan-styling minimal` uses `+--` / `` `-- ` / `|` connectors and a plain summary block — same data, fewer tokens.

## Edge cases

- **Timeout**: if `--scan-timeout` is reached mid-scan, `prs` stops walking, prints the partial tree, and adds a timeout notice to the summary. Raise `--scan-timeout` or narrow with `--only` / `--ignore` to get a complete scan.
- **Broken symlinks**: shown as `link` entries with size and lines reported as `-`. The broken target is not followed.
- **Binary or unreadable files**: line count is skipped; the summary's total line count gets a `+?` suffix to signal that the real total is strictly higher than shown.
- **Non-git directories**: no git markers are shown; the `git` column is omitted from the tree and the summary.
- **Empty entries**: a file with 0 bytes or a directory with no scanned children. Hidden by `--ignore-empty`; shown by `--full`.
- **Path/command name collision**: a directory literally named `help`, `version`, or `status` can still be scanned with the `./` prefix (e.g. `prs ./help`).
- **Broken pipe**: when stdout is closed early (e.g. `prs | head -1`), `prs` exits cleanly without spurious errors.
- **Permissions**: `prs` is read-only. It never creates, modifies, or deletes anything in the scanned project.

## Tips for AI agents

- **Run once, reuse often.** Capture the `prs` output at the start of the task and refer back to it. Re-scanning is wasteful; re-reading individual files for layout info is more wasteful.
- **Minimal styling for agent context.** When feeding `prs` output into your own context or another agent's, use `--scan-styling minimal --scan-emojis false`. ASCII connectors and no emojis minimize tokens and parsing noise.
- **Trim data to what you need.** `--scan-data "tree"` for structure only. `--scan-data "summary"` for totals only. `--scan-data "tree, lines, size"` for a compact code-overview.
- **Scope before scanning huge trees.** For a large repo, run `prs <subdir>` or `prs --only -e <ext>` first to avoid a timeout and a giant output.
- **Use `--only` to focus a language.** `prs --only -e .py` shows just the Python files; `prs --only .md` (shorthand) shows just the Markdown files.
- **Use `--ignore` to drop noise.** Combine `--ignore -e .min.js .map` with `--ignore -n dist build coverage node_modules` to remove generated artifacts.
- **Read the summary first.** The `largest` and `newest` rows point at the files most likely to matter for the current task. The `types` row tells you the dominant languages at a glance.
- **Trust the git markers.** Before editing a file marked `[M]`, read its current content — the working tree differs from HEAD. A `[?]` file is brand new and probably safe to read but ask before deleting.
- **Disable auto-copy in scripts.** Pass `--auto-copy false` whenever you pipe `prs` output to another tool; otherwise it tries to claim the clipboard.

## Integration with other tools

`prs` output is plain text and pipes cleanly. The `minimal` styling is the most pipe-friendly because it avoids box-drawing characters.

```bash
# Save a snapshot to revisit later
prs ~/code/my-app --scan-styling minimal --auto-copy false > project-snapshot.txt

# Find the largest files prs sees
prs --scan-styling minimal --auto-copy false | grep -E 'file\s+[0-9,]+L' | sort -k3 -h | tail -10

# Count files by extension (uses the type-distribution row in the summary)
prs --scan-styling minimal --auto-copy false | grep '^types'

# Hand the tree to a downstream agent as compact context
prs --scan-styling minimal --scan-emojis false --scan-data "tree, summary" --auto-copy false
```

`prs` makes no filesystem changes, so it composes safely with any other read or write tooling in a pipeline.

## Reference

- `prs help` — the canonical, always-up-to-date list of flags (run this if the SKILL.md and the installed `prs` ever disagree).
- `README.md` — human-facing docs with the full options tables, install instructions, and the same example output.
- `config.yaml` — the default ignore list and rendering/runtime defaults; edit it to change project-wide behavior without touching CLI flags.
- `prs status` — shows the installed version, app directory, Python interpreter, and active config file path. Use it to verify the install before relying on the skill.

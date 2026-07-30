# Scan Dir (`sdir`)

Scan Dir is a dependency-free Python CLI that turns a file or directory into compact, deterministic terminal context. It reports structure, file metadata, Git state, warnings, and a scan summary without reading descendant symlink targets or unsupported special files.

Use `sdir` to inspect a project quickly, prepare bounded context for an AI agent, or generate stable plain-text output for scripts and pipelines.

## Preview

```bash
sdir ~/code/my-project
```

```bash
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

## Features

* Single-file Python 3.10+ runtime with no third-party runtime dependencies
* Tree or flat output with selectable metadata and three styling levels
* Repository-local Git markers with global/system Git configuration disabled
* Explicit filtering, visibility, timeout, clipboard, and configuration controls
* Safe handling for hostile filenames, symlinks, partial scans, and broken pipes
* Transactional Linux/macOS installer with integrity checks and rollback
* Deterministic, reproducible release archives and an included `SKILL.md`

## Install

### Linux / macOS

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/vivid0o0/scan-dir/main/install.sh | bash
```

Requires Python 3.10+; the installer and managed wrappers require Bash 3.2+. The installer stages `sdir.py`, `config.yaml`, and `SKILL.md`, verifies their embedded SHA-256 digests, and installs both command names. Re-running it validates and repairs managed files; owned managed directories are automatically stripped of unsafe group/other write permissions. Run `bash install.sh --help` for installer options.

### Full installation (including SKILL.md)

Give this prompt to your favorite AI agent:

```text
Install Scan Dir from the files I provide. Install sdir.py, config.yaml, and SKILL.md; expose the sdir and scan-dir commands; run syntax, installer, and CLI validation before reporting completion.
```

## Usage

```bash
sdir [path] [options]
```

> **Path is optional. When omitted, `sdir` scans the current directory.**

## Options

### Filter mode

| Option     | Description                                              |
| ---------- | -------------------------------------------------------- |
| `--ignore` | Exclude entries matching the provided filters.           |
| `--only`   | Include only entries matching explicit filters.          |
| `--full`   | Include all entries, including hidden and empty entries. |

### Filter selectors

| Option                             | Description                                                     |
| ---------------------------------- | --------------------------------------------------------------- |
| `-f, --paths <paths...>`           | Match relative paths and everything inside matched directories. |
| `-t, --types <types...>`           | Match entry types: `file`, `dir`, `link`.                       |
| `-e, --extensions <extensions...>` | Match file extensions, such as `.ts`, `.json`, or `.md`.        |
| `-n, --names <names...>`           | Match exact file or directory basenames.                        |

### File visibility

| Option            | Description                                       |
| ----------------- | ------------------------------------------------- |
| `--ignore-hidden`  | Ignore files and directories starting with a dot. |
| `--include-hidden` | Show hidden entries, overriding configuration.       |
| `--ignore-empty`   | Ignore empty files and directories.                |
| `--include-empty`  | Show empty entries, overriding configuration.        |

### Rendering

| Option                                           | Description                             |
| ------------------------------------------------ | --------------------------------------- |
| `--scan-styling <full\|low\|minimal>`            | Set the visual style of the output.     |
| `--scan-data <"item, item, ...">`                | Control how much metadata is shown.     |
| `--scan-emojis <true\|false>`                    | Display emojis for clarity.             |

### Runtime

| Option                      | Description                                                                   |
| --------------------------- | ----------------------------------------------------------------------------- |
| `--scan-timeout <seconds>`  | Use a best-effort scan budget and print the partial result when exceeded. |
| `--auto-copy <true\|false>` | Copy the scan output to the clipboard after scanning. Default: `false`. |

### Configuration

| Option              | Description                           |
| ------------------- | ------------------------------------- |
| `--config <path>`   | Use a specific `config.yaml` file.    |
| `--project-config <auto\|ignore\|require>` | Control project `.sdir.yaml` or `sdir.yaml` discovery. |
| `--help`, `-h`      | Show help text and exit.              |
| `--version`         | Show version number and exit.         |
| `sdir status`        | Show runtime, interpreter, and config status. |

### Notes

* Options can be combined.
* When no command-line options are provided, `sdir` uses `config.yaml`.
* Command-line options override matching `config.yaml` values.
* `--only` uses explicit CLI selectors and does not turn default ignored names into included names.
* Config precedence (highest first): `--config <path>` → project `.sdir.yaml` or `sdir.yaml` → user config → managed `config.yaml` beside `sdir.py` → built-in defaults. A generic project `config.yaml` is ignored. Use `--project-config ignore` for untrusted repositories. Project config cannot enable clipboard copying unless it is also supplied explicitly with `--config`. User config persists across installs and is never overwritten.
* Selectors use per-category replacement: CLI `-n foo` replaces the config `names:` list rather than appending.

## Configuration file

`sdir` can read defaults from `config.yaml`.

```yaml
# Ignore exact relative paths (and everything under them)
paths: []

# Ignore by type: file, dir, link
types: []

# Ignore by extension (include the leading dot)
extensions: []

# Ignore by basename (files or folders)
names:
  - .git
  - node_modules
  - __pycache__
  - .venv
  - venv
  - dist
  - build
  - .next
  - .turbo
  - .cache
  - .idea
  - .pytest_cache
  - .mypy_cache
  - .ruff_cache
  - .tox
  - .eggs
  - .DS_Store
  - Thumbs.db
  - coverage

# File visibility controls
ignore-hidden: false # ignores files starting with a dot (e.g., .git, .env)
ignore-empty: false # ignores empty files and folders

# Rendering controls
# styling: full | low | minimal (best for agents)
scan-styling: full
# show file-type emojis in entry names: true | false
scan-emojis: true
# comma-separated metadata items to show (canonical names).
scan-data: "tree, lines, size, modified, type, git, summary"

# Runtime controls
scan-timeout: 60 # seconds
auto-copy: false # copy to clipboard after scan

### NOTE: The above settings are defaults. You can override them via command-line arguments.
```

## Output

### Styling levels

| Value     | Description                                       |
| --------- | ------------------------------------------------- |
| `full`    | Rich terminal output with a framed summary.       |
| `low`     | Plain summary without the framed box.             |
| `minimal` | Compact ASCII output for AI agents and pipes.     |

> **NOTE**: This only applies to styling, data isn't affected.

### Metadata levels

| Value               | options                                                      |
| ------------------- | ------------------------------------------------------------ |
| `"item, item, ..."` | tree, lines, size, modified, type, git, summary. |

When `tree` is omitted from scan-data, entries are shown as a flat list (no tree indentation). When `summary` is omitted, the summary block is hidden.

## Git status markers

Git markers appear when the scan path is inside a git repository. For deterministic and non-interactive scans, SDIR disables system/global Git configuration; markers therefore reflect repository-local ignore rules rather than personal global excludes.

| Marker | Meaning   |
| ------ | --------- |
| `[M]`  | Modified  |
| `[A]`  | Added     |
| `[D]`  | Deleted   |
| `[R]`  | Renamed   |
| `[C]`  | Copied    |
| `[U]`  | Unmerged  |
| `[?]`  | Untracked |
| `[!]`  | Ignored   |

## Notes

* `--scan-timeout` is a best-effort budget checked around filesystem and Git operations; a blocking system call may return after the budget before the partial result is printed.
* Empty entries are files with `0` bytes or directories with no scanned children.

## Exit codes

### `sdir` command

| Code  | Meaning                                                   |
| ----- | --------------------------------------------------------- |
| `0`   | Success, including a clean downstream broken-pipe exit    |
| `1`   | Runtime, operating-system, or requested clipboard failure |
| `2`   | Argument or configuration error                           |
| `130` | Interrupted (`Ctrl+C`)                                    |

### `install.sh`

| Code  | Meaning                                                                    |
| ----- | -------------------------------------------------------------------------- |
| `0`   | Install, repair, or dry-run validation succeeded                            |
| `1`   | Validation, download, integrity, safety, or installation failure            |
| `70`  | Rollback could not fully restore; the preserved backup path is reported     |
| `130` | Interrupted (`Ctrl+C`); rollback is attempted before the installer exits    |

## License

MIT

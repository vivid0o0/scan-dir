<!-- SKILL.md -- Project Summarizer agent skill -->
<!-- Operating contract for compact, reliable project context with PRS. -->
<!-- Tags: agent-skill, project-context, cli, filesystem, git -->
<!-- 2026-07-28 -->

# Project Summarizer

Use `prs` to obtain a compact structural and metadata overview before reading a project file by file. Treat its output as a navigation aid, not as a substitute for source inspection.

## Availability

```bash
prs version
prs status
```

If the command is unavailable, install the provided package with `install.sh`. Do not invent scan output or assume configuration was installed correctly.

## Recommended scan

```bash
prs <project-path> \
  --scan-styling minimal \
  --scan-emojis false \
  --scan-data "tree, lines, size, modified, type, git, summary" \
  --project-config ignore \
  --auto-copy false
```

Use a narrower scan when less context is needed:

```bash
prs <project-path> --scan-data tree --scan-styling minimal --scan-emojis false
prs <project-path> --scan-data summary --scan-styling minimal --scan-emojis false
prs <project-path> --only -e .py .toml --scan-styling minimal --scan-emojis false
```

## Deterministic argument rules

- Put the scan path before selectors.
- When selectors come first, put the path after `--`.
- Selector values beginning with `-` require attached syntax, such as `--names=-draft`.
- `help`, `version`, and `status` are commands only as the first token.
- Use `./help`, `./version`, `./status`, or `-- <path>` to scan command-like path names.

## Modes and selectors

```text
--ignore                         exclude matches; default mode
--only                           retain explicit matches; selector required
--full                           disable filters and include hidden/empty entries
-f, --paths <paths...>           exact relative paths and descendants
-t, --types <file|dir|link...>   entry types
-e, --extensions <ext...>        extensions including leading dots
-n, --names <names...>           exact basenames
```

`--only` and `--ignore` accept shorthand. `file`, `dir`, and `link` are types; a dot-prefixed token without `/` is an extension; a token containing `/` is a path; every other token is a name.

In ignore mode, each CLI selector category replaces the corresponding configured category. In only mode, configured ignore selectors are never converted into include selectors.

`--full` cannot be combined with selectors, `--ignore-hidden`, or `--ignore-empty`.

## Visibility and rendering

```text
--ignore-hidden / --include-hidden
--ignore-empty  / --include-empty
--scan-styling <full|low|minimal>
--scan-emojis <true|false>
--scan-data <comma-separated exact items>
--scan-timeout <positive finite seconds>
--auto-copy <true|false>
--config <path>
--project-config <auto|ignore|require>
-h, --help / --version
```

Valid data items are `tree`, `lines`, `size`, `modified`, `type`, `git`, and `summary`. At least one item is required. Without `tree`, PRS prints relative-path flat rows.

## Configuration layers

Configuration merges from low to high precedence:

1. Built-in defaults
2. Managed `config.yaml` beside `prs.py`
3. User `config.yaml`
4. Project `.prs.yaml` or `prs.yaml`
5. Explicit `--config`

A higher file overrides only keys it defines. A generic project `config.yaml` is ignored. Use `--project-config ignore` when scanning untrusted code. Project configuration cannot enable clipboard copying unless the same file is explicitly trusted with `--config`. Personal defaults belong in the user configuration directory, not in the installer-managed app directory.

## Reading output

- `[M]`, `[A]`, `[D]`, `[R]`, `[C]`, `[U]`, `[?]`, and `[!]` are Git states.
- `?L` means binary or unreadable content.
- A total such as `2,671+? lines` is a lower bound.
- Warnings preserve usable output while identifying incomplete or skipped work.
- A timeout is a best-effort budget and produces a partial result with an explicit notice when detected.
- Descendant symlinks are displayed but never followed.
- Unsupported special files are warned about and never opened.

## Operating rules

1. Run one appropriately scoped scan at the start of project work.
2. Read relevant files directly before changing them.
3. Respect Git markers and unrelated modifications.
4. Narrow large scans by path or selector instead of hiding timeout state.
5. Keep auto-copy disabled in pipelines and automated workflows.
6. Report warnings and timeout state; never present partial output as complete.
7. Use `prs help` as the canonical installed CLI reference.

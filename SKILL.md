# Scan Dir (sdir)

Use `sdir` to get a compact structural and metadata overview of a project before reading files one by one. Its output is a navigation aid, not a substitute for reading the source.

## Availability

```bash
sdir version
sdir status
```

If the command is missing, install it with `install.sh`. Do not invent scan output or assume config was installed correctly.

## Recommended scan

```bash
sdir <project-path> \
  --scan-styling minimal \
  --scan-emojis false \
  --scan-data "tree, lines, size, modified, type, git, summary" \
  --project-config ignore \
  --auto-copy false
```

Narrower scans when you need less:

```bash
sdir <project-path> --scan-data tree --scan-styling minimal --scan-emojis false
sdir <project-path> --scan-data summary --scan-styling minimal --scan-emojis false
sdir <project-path> --only -e .py .toml --scan-styling minimal --scan-emojis false
```

## Argument rules

- Put the scan path before selectors.
- When selectors come first, put the path after `--`.
- Selector values beginning with `-` need attached syntax, like `--names=-draft`.
- `help`, `version`, `status`, and `--set-config` are commands only as the first token.
- Use `./help`, `./version`, `./status`, or `-- <path>` to scan command-like path names.

## Modes and selectors

```text
--ignore                         exclude matches; default mode
--only                           keep explicit matches; selector required
--full                           disable filters and include hidden/empty entries
-f, --paths <paths...>           exact relative paths and descendants
-t, --types <file|dir|link...>   entry types
-e, --extensions <ext...>        extensions including leading dots
-n, --names <names...>           exact basenames
```

`--only` and `--ignore` accept shorthand. `file`, `dir`, and `link` are types; a dot-prefixed token without `/` is an extension; a token containing `/` is a path; every other token is a name.

In ignore mode each CLI selector category replaces the corresponding configured category. In only mode configured ignore selectors are never turned into include selectors.

`--full` cannot combine with selectors, `--ignore-hidden`, or `--ignore-empty`.

## Visibility and rendering

```text
--ignore-hidden / --include-hidden
--ignore-empty  / --include-empty
--scan-styling <full|low|minimal>
--scan-emojis <true|false>
--scan-data <items or max>
--scan-timeout <seconds>
--auto-copy <true|false>
--config <path>
--set-config <path>
--project-config <auto|ignore|require>
-h, --help / --version
```

`--scan-data` accepts `tree`, `lines`, `size`, `modified`, `type`, `git`, `summary`, or `max` for all of them. At least one item is required. Without `tree`, SDIR prints relative-path flat rows.

## Configuration

Config merges from low to high precedence:

1. Built-in defaults
2. Managed `config.yaml` beside `sdir.py`
3. User `config.yaml`
4. Project `.sdir.yaml` or `sdir.yaml`
5. Explicit `--config`

A higher file overrides only the keys it defines. A generic project `config.yaml` is ignored. Use `--project-config ignore` when scanning untrusted code. Project configuration cannot enable clipboard copying unless the same file is explicitly trusted with `--config`. Use `sdir --set-config <path>` to persist a different default config path.

## Reading output

- `[M]`, `[A]`, `[D]`, `[R]`, `[C]`, `[U]`, `[?]`, and `[!]` are Git states.
- `?L` means binary or unreadable content.
- A total like `2,671+? lines` is a lower bound.
- Warnings keep usable output while flagging incomplete or skipped work.
- A timeout is a best-effort budget and yields a partial result with an explicit notice.
- Descendant symlinks are shown but never followed.
- Unsupported special files are warned about and never opened.

## Operating rules

1. Run one appropriately scoped scan at the start of project work.
2. Read relevant files directly before changing them.
3. Respect Git markers and unrelated modifications.
4. Narrow large scans by path or selector instead of hiding timeout state.
5. Keep auto-copy disabled in pipelines and automated workflows.
6. Report warnings and timeout state; never present partial output as complete.
7. Use `sdir help` as the canonical installed CLI reference.
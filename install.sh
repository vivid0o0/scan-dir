#!/usr/bin/env bash
# install.sh -- Scan Dir installer
# Installs, validates, repairs, and exposes the sdir commands with transactional
# rollback, package integrity verification, and safe PATH integration.
# Tags: installer, linux, macos, rollback, integrity
# 2026-07-28

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly PRODUCT_TITLE='Scan Dir'
readonly PRODUCT_SLUG='scan-dir'
readonly PRIMARY_COMMAND='sdir'
readonly ALIAS_COMMAND='scan-dir'
readonly RUNTIME_FILE='sdir.py'
readonly CONFIG_FILE='config.yaml'
readonly SKILL_FILE='SKILL.md'
readonly MANIFEST_FILE='install-manifest.env'
readonly VERSION_FILE='.installer-version'
readonly MANAGED_FILE='.managed'
readonly MANAGED_MARKER='scan-dir managed command'
readonly BRIDGE_MARKER='scan-dir managed active PATH bridge'
readonly PATH_BLOCK_BEGIN='# >>> scan-dir PATH >>>'
readonly PATH_BLOCK_END='# <<< scan-dir PATH <<<'
readonly INSTALLER_VERSION='1.0.0'
readonly MINIMUM_PYTHON_VERSION='3.10'
readonly DEFAULT_SOURCE_URL='https://raw.githubusercontent.com/vivid0o0/scan-dir/main/sdir.py'
readonly DEFAULT_RUNTIME_SHA256='1e1e1f76dcab5bb2fa7f6224fb3be9f5c708ba8243137279b0d40ae16e751deb'
readonly DEFAULT_CONFIG_SHA256='596dadc0702d50e7b46167478d5c05f24909aa5f380ba854cbff2aca4f6d305f'
readonly DEFAULT_SKILL_SHA256='2d8c071287325026859035fbbb72c687b6ad29ca53a0be5e4af1abe0c2c5104e'
readonly NETWORK_CONNECT_TIMEOUT=15
readonly NETWORK_TOTAL_TIMEOUT=300
readonly NETWORK_RETRIES=3
readonly MAX_DOWNLOAD_BYTES=10485760
readonly PACKAGE_MANAGER_TOTAL_TIMEOUT=180
readonly LOCK_NAME='install.lock'
readonly LOG_NAME='install.log'
readonly DIR_MODE=700
readonly PRIVATE_MODE=600
readonly DATA_MODE=644
readonly EXEC_MODE=755
readonly ROLLBACK_FAILURE_EXIT=70

# Canonical identity of the unpublished pre-1.0 local build. These constants
# are intentionally isolated to the one-way migration routine; they are not
# exposed as supported commands or configuration names.
readonly LEGACY_PRODUCT_SLUG='project-summarizer'
readonly LEGACY_PRIMARY_COMMAND='prs'
readonly LEGACY_ALIAS_COMMAND='project-summarizer'
readonly LEGACY_RUNTIME_FILE='prs.py'
readonly LEGACY_MANAGED_MARKER='project-summarizer managed command'

SOURCE_PATH=${SDIR_SOURCE:-}
SOURCE_URL=${SDIR_SOURCE_URL:-}
RUNTIME_SHA256=${SDIR_SOURCE_SHA256:-}
CONFIG_SHA256=${SDIR_CONFIG_SHA256:-}
SKILL_SHA256=${SDIR_SKILL_SHA256:-}
APP_DIR_OVERRIDE=''
BIN_DIR_OVERRIDE=''
STATE_DIR_OVERRIDE=''
CONFIG_DIR_OVERRIDE=''
TMP_ROOT_OVERRIDE=''
LOGO_MODE='auto'
COLOR_MODE='auto'
QUIET=0
FORCE=0
DRY_RUN=0
NO_PATH_REPAIR=0
NO_ACTIVE_BRIDGE=0

PLATFORM=''
ARCH=''
SCRIPT_DIR=''
SCRIPT_PATH=''
ORIGINAL_PATH=${PATH:-}
APP_DIR=''
BIN_DIR=''
STATE_DIR=''
CONFIG_DIR=''
TMP_ROOT=''
LOG_DIR=''
LOG_FILE=''
LEGACY_APP_DIR=''
LEGACY_STATE_DIR=''
LEGACY_CONFIG_DIR=''
LEGACY_BIN_DIR=''
PYTHON_BIN=''
WORK_DIR=''
STAGED_APP_DIR=''
RESOLVED_RUNTIME=''
RESOLVED_CONFIG=''
RESOLVED_SKILL=''
LOCK_DIRS=()
LOCK_TOKENS=()
CHILD_PID=''
WATCHDOG_PID=''
CHILD_PROCESS_GROUP=0
TEMP_PATH=''
BACKUP_SLOT=''
CURRENT_STEP='startup'
FINALIZED=0
NORMALIZED_SHA=''
MANIFEST_VALID=0
MANIFEST_PRODUCT=''
MANIFEST_VERSION=''
MANIFEST_APP_DIR=''
MANIFEST_BIN_DIR=''
MANIFEST_STATE_DIR=''
MANIFEST_CONFIG_DIR=''
MANIFEST_RUNTIME_SHA=''
MANIFEST_CONFIG_SHA=''
MANIFEST_SKILL_SHA=''
MANIFEST_PRIMARY_SHA=''
MANIFEST_ALIAS_SHA=''
INSTALLED_VERSION=''
INSTALLED_VERSION_UNKNOWN=0
CREATED_DIRS=()
CREATED_FILES=()
BACKUP_TARGETS=()
BACKUP_FILES=()
CLEANUP_PATHS=()
PRESERVED_CLEANUP_PATHS=()
ROLLBACK_FAILURES=()
WARNINGS=()

RESET=''; BOLD=''; GREEN=''; RED=''; CYAN=''; YELLOW=''

has() { command -v "$1" >/dev/null 2>&1; }
is_tty() { [[ -t 1 ]]; }

setup_colors() {
  if [[ "$COLOR_MODE" == 'never' ]]; then return 0; fi
  if [[ "$COLOR_MODE" != 'always' ]]; then
    [[ -z "${NO_COLOR:-}" ]] || return 0
    if ! is_tty && [[ "${FORCE_COLOR:-0}" != 1 ]]; then return 0; fi
  fi
  RESET=$'\033[0m'; BOLD=$'\033[1m'
  GREEN=$'\033[32m'; RED=$'\033[31m'; CYAN=$'\033[36m'; YELLOW=$'\033[33m'
}

sanitize_text() {
  local text="$*" char code escaped
  while [[ "$text" =~ [[:cntrl:]] ]]; do
    char=${BASH_REMATCH[0]}
    printf -v code '%d' "'$char"
    printf -v escaped '\\u%04X' "$code"
    text=${text/"$char"/$escaped}
  done
  printf '%s' "$text"
}


say() { [[ "$QUIET" == 1 ]] || printf '%s\n' "$*"; }
warn() {
  local message; message="$(sanitize_text "$*")"
  WARNINGS+=("$message")
  [[ "$QUIET" == 1 ]] || printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$message" >&2
  log "warning: $message"
}
log() {
  [[ -n "$LOG_FILE" ]] || return 0
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf 'unknown-time')" "$(sanitize_text "$*")" >> "$LOG_FILE" 2>/dev/null || true
}
fail() {
  local message; message="$(sanitize_text "$*")"
  log "error: $message"
  printf '%serror:%s %s\n' "$RED" "$RESET" "$message" >&2
  exit 1
}
step() {
  CURRENT_STEP="$1"
  [[ "$QUIET" == 1 ]] || printf '%s•%s %s\n' "$CYAN" "$RESET" "$CURRENT_STEP"
  shift
  "$@"
}

usage() {
  cat <<EOF
$PRODUCT_TITLE setup

Usage:
  bash install.sh [options]

Package source:
  --source <path>          Install a local package beside config.yaml and SKILL.md
  --source-url <https-url> Download sdir.py and its two sibling package files
  --sha256 <digest>        Expected SHA-256 for sdir.py
  --config-sha256 <digest> Expected SHA-256 for config.yaml
  --skill-sha256 <digest>  Expected SHA-256 for SKILL.md

Managed paths:
  --app-dir <path>         Runtime directory
  --bin-dir <path>         Command directory
  --state-dir <path>       State and log directory
  --config-dir <path>      Persistent user-configuration directory
  --tmp-dir <path>         Temporary workspace parent

Behavior:
  --no-path-repair         Do not edit shell startup files
  --no-active-bridge       Do not bridge into a safe active PATH directory
  --force                  Reinstall, permit downgrade, and replace foreign commands
  --dry-run                Resolve and validate without persistent changes
  --quiet                  Suppress non-error output
  --logo <mode>            auto, text, small, medium, or large
  --color <mode>           auto, always, or never
  -h, --help               Show this help

The no-argument curl installer downloads the canonical three-file package and
verifies embedded SHA-256 digests. A custom --source-url requires all three
explicit digest options. Foreign commands are never replaced without --force.
EOF
}

require_arg() { [[ -n "${2:-}" ]] || fail "$1 requires a value"; }
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source) shift; require_arg --source "${1:-}"; SOURCE_PATH=$1 ;;
      --source-url) shift; require_arg --source-url "${1:-}"; SOURCE_URL=$1 ;;
      --sha256) shift; require_arg --sha256 "${1:-}"; RUNTIME_SHA256=$1 ;;
      --config-sha256) shift; require_arg --config-sha256 "${1:-}"; CONFIG_SHA256=$1 ;;
      --skill-sha256) shift; require_arg --skill-sha256 "${1:-}"; SKILL_SHA256=$1 ;;
      --app-dir) shift; require_arg --app-dir "${1:-}"; APP_DIR_OVERRIDE=$1 ;;
      --bin-dir) shift; require_arg --bin-dir "${1:-}"; BIN_DIR_OVERRIDE=$1 ;;
      --state-dir) shift; require_arg --state-dir "${1:-}"; STATE_DIR_OVERRIDE=$1 ;;
      --config-dir) shift; require_arg --config-dir "${1:-}"; CONFIG_DIR_OVERRIDE=$1 ;;
      --tmp-dir) shift; require_arg --tmp-dir "${1:-}"; TMP_ROOT_OVERRIDE=$1 ;;
      --no-path-repair) NO_PATH_REPAIR=1 ;;
      --no-active-bridge) NO_ACTIVE_BRIDGE=1 ;;
      --force) FORCE=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --quiet) QUIET=1 ;;
      --logo) shift; require_arg --logo "${1:-}"; LOGO_MODE=$1 ;;
      --color) shift; require_arg --color "${1:-}"; COLOR_MODE=$1 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown option: $1" ;;
    esac
    shift
  done
  [[ -z "$SOURCE_PATH" || -z "$SOURCE_URL" ]] || fail '--source and --source-url are mutually exclusive'
  case "$LOGO_MODE" in auto|text|small|medium|large) : ;; *) fail "invalid logo mode: $LOGO_MODE" ;; esac
  case "$COLOR_MODE" in auto|always|never) : ;; *) fail "invalid color mode: $COLOR_MODE" ;; esac
}

render_banner() {
  [[ "$QUIET" == 1 ]] && return 0
  local mode=$LOGO_MODE
  if [[ "$mode" == auto ]]; then
    if is_tty; then mode=medium; else mode=text; fi
  fi
  case "$mode" in
    text)
      printf '%s%s installer%s
' "$BOLD" "$PRODUCT_TITLE" "$RESET"
      ;;
    small)
      printf '%s[%s]%s install • repair • validate
' "$CYAN" "$PRIMARY_COMMAND" "$RESET"
      ;;
    medium)
      printf '%s┌──────────────────────────────────────────────┐%s
' "$CYAN" "$RESET"
      printf '%s│%s  %-42s  %s│%s
' "$CYAN" "$RESET" "$PRODUCT_TITLE installer" "$CYAN" "$RESET"
      printf '%s│%s  %-42s  %s│%s
' "$CYAN" "$RESET" 'install • repair • validate' "$CYAN" "$RESET"
      printf '%s└──────────────────────────────────────────────┘%s
' "$CYAN" "$RESET"
      ;;
    large)
      printf '%s' "$CYAN"
      cat <<'EOF'
  ____                   ____  _
 / ___|  ___ __ _ _ __  |  _ \(_)_ __
 \___ \ / __/ _` | '_ \ | | | | | '__|
  ___) | (_| (_| | | | || |_| | | |
 |____/ \___\__,_|_| |_||____/|_|_|
EOF
      printf '%sinstall • repair • validate%s
' "$BOLD" "$RESET"
      ;;
  esac
}


contains_control() {
  [[ "$1" =~ [[:cntrl:]] ]]
}

lexical_absolute_path() {
  local input=$1 component result='' old_ifs=$IFS
  [[ -n "$input" ]] || return 1
  case "$input" in
    \~) input=$HOME ;;
    \~/*) input="$HOME/${input:2}" ;;
    /*) : ;;
    *) input="$(pwd -P)/$input" ;;
  esac
  local parts=()
  IFS='/' read -r -a parts <<< "$input"
  IFS=$old_ifs
  for component in "${parts[@]}"; do
    case "$component" in
      ''|.) : ;;
      ..) result=${result%/*}; [[ -n "$result" ]] || result='' ;;
      *) result="$result/$component" ;;
    esac
  done
  [[ -n "$result" ]] || result='/'
  printf '%s' "$result"
}

physical_path() {
  local input parent suffix='' base
  input="$(lexical_absolute_path "$1")" || return 1
  parent=$input
  while [[ ! -e "$parent" && ! -L "$parent" ]]; do
    base=$(basename "$parent")
    suffix="/$base$suffix"
    parent=$(dirname "$parent")
  done
  if [[ -d "$parent" ]]; then
    parent=$(cd -P "$parent" 2>/dev/null && pwd -P) || return 1
  else
    base=$(basename "$parent")
    parent=$(cd -P "$(dirname "$parent")" 2>/dev/null && pwd -P) || return 1
    parent="$parent/$base"
  fi
  printf '%s%s' "${parent%/}" "$suffix"
}

path_is_within() {
  local child=${1%/} parent=${2%/}
  [[ "$child" == "$parent" || "$child" == "$parent/"* ]]
}

validate_home() {
  [[ -n "${HOME:-}" ]] || fail 'HOME is not set'
  contains_control "$HOME" && fail 'HOME contains control characters'
  HOME="$(physical_path "$HOME")" || fail 'unable to resolve HOME'
  [[ "$HOME" == /* && "$HOME" != '/' ]] || fail "unsafe HOME: $HOME"
}

validate_managed_path() {
  local label=$1 path=$2
  [[ "$path" == /* ]] || fail "$label must be absolute: $path"
  contains_control "$path" && fail "$label contains control characters"
  [[ "$path" != "$HOME" ]] || fail "refusing broad $label: $path"
  case "$path" in /|/bin|/sbin|/dev|/etc|/proc|/root|/run|/sys|/usr|/usr/bin|/usr/sbin|/usr/local|/var|/tmp|/opt|/home|/Users|/Library|/Applications) fail "refusing broad $label: $path" ;; esac
}

select_xdg() {
  local candidate=$1 fallback=$2
  if [[ -n "$candidate" && "$candidate" == /* ]]; then printf '%s' "$candidate"; else printf '%s' "$fallback"; fi
}

setup_paths() {
  validate_home
  local data_base state_base config_base bin_base tmp_base
  case "$PLATFORM" in
    darwin)
      data_base="$HOME/Library/Application Support"
      state_base="$HOME/Library/Application Support"
      config_base="$HOME/Library/Application Support"
      ;;
    *)
      data_base="$(select_xdg "${XDG_DATA_HOME:-}" "$HOME/.local/share")"
      state_base="$(select_xdg "${XDG_STATE_HOME:-}" "$HOME/.local/state")"
      config_base="$(select_xdg "${XDG_CONFIG_HOME:-}" "$HOME/.config")"
      ;;
  esac
  bin_base="$(select_xdg "${XDG_BIN_HOME:-}" "$HOME/.local/bin")"
  tmp_base="${TMPDIR:-/tmp}"; [[ "$tmp_base" == /* ]] || tmp_base=/tmp

  local explicit
  for explicit in "${APP_DIR_OVERRIDE:-${SDIR_APP_DIR:-}}" "${BIN_DIR_OVERRIDE:-${SDIR_BIN_DIR:-}}" "${STATE_DIR_OVERRIDE:-${SDIR_STATE_DIR:-}}" "${CONFIG_DIR_OVERRIDE:-${SDIR_CONFIG_DIR:-}}" "${TMP_ROOT_OVERRIDE:-${SDIR_TMP_ROOT:-}}"; do
    [[ -z "$explicit" || "$explicit" == /* ]] || fail "explicit managed path must be absolute: $explicit"
  done
  if [[ "$PLATFORM" == darwin ]]; then
    APP_DIR=${APP_DIR_OVERRIDE:-${SDIR_APP_DIR:-$HOME/Library/Application Support/$PRODUCT_SLUG/app}}
    STATE_DIR=${STATE_DIR_OVERRIDE:-${SDIR_STATE_DIR:-$HOME/Library/Application Support/$PRODUCT_SLUG/state}}
    CONFIG_DIR=${CONFIG_DIR_OVERRIDE:-${SDIR_CONFIG_DIR:-$HOME/Library/Application Support/$PRODUCT_SLUG/config}}
    LEGACY_APP_DIR="$HOME/Library/Application Support/$LEGACY_PRODUCT_SLUG/app"
    LEGACY_STATE_DIR="$HOME/Library/Application Support/$LEGACY_PRODUCT_SLUG/state"
    LEGACY_CONFIG_DIR="$HOME/Library/Application Support/$LEGACY_PRODUCT_SLUG/config"
  else
    APP_DIR=${APP_DIR_OVERRIDE:-${SDIR_APP_DIR:-$data_base/$PRODUCT_SLUG}}
    STATE_DIR=${STATE_DIR_OVERRIDE:-${SDIR_STATE_DIR:-$state_base/$PRODUCT_SLUG}}
    CONFIG_DIR=${CONFIG_DIR_OVERRIDE:-${SDIR_CONFIG_DIR:-$config_base/$PRODUCT_SLUG}}
    LEGACY_APP_DIR="$data_base/$LEGACY_PRODUCT_SLUG"
    LEGACY_STATE_DIR="$state_base/$LEGACY_PRODUCT_SLUG"
    LEGACY_CONFIG_DIR="$config_base/$LEGACY_PRODUCT_SLUG"
  fi
  BIN_DIR=${BIN_DIR_OVERRIDE:-${SDIR_BIN_DIR:-$bin_base}}
  LEGACY_BIN_DIR=$BIN_DIR
  TMP_ROOT=${TMP_ROOT_OVERRIDE:-${SDIR_TMP_ROOT:-$tmp_base}}

  APP_DIR="$(physical_path "$APP_DIR")" || fail 'unable to resolve application directory'
  STATE_DIR="$(physical_path "$STATE_DIR")" || fail 'unable to resolve state directory'
  CONFIG_DIR="$(physical_path "$CONFIG_DIR")" || fail 'unable to resolve config directory'
  BIN_DIR="$(physical_path "$BIN_DIR")" || fail 'unable to resolve binary directory'
  TMP_ROOT="$(physical_path "$TMP_ROOT")" || fail 'unable to resolve temporary directory'
  LOG_DIR="$(physical_path "$STATE_DIR/logs")" || fail 'unable to resolve log directory'
  LEGACY_APP_DIR="$(physical_path "$LEGACY_APP_DIR")" || fail 'unable to resolve legacy application directory'
  LEGACY_STATE_DIR="$(physical_path "$LEGACY_STATE_DIR")" || fail 'unable to resolve legacy state directory'
  LEGACY_CONFIG_DIR="$(physical_path "$LEGACY_CONFIG_DIR")" || fail 'unable to resolve legacy config directory'
  LEGACY_BIN_DIR="$(physical_path "$LEGACY_BIN_DIR")" || fail 'unable to resolve legacy binary directory'
  path_is_within "$LOG_DIR" "$STATE_DIR" || fail "log directory escapes state directory: $LOG_DIR"

  validate_managed_path 'application directory' "$APP_DIR"
  validate_managed_path 'state directory' "$STATE_DIR"
  validate_managed_path 'config directory' "$CONFIG_DIR"
  validate_managed_path 'binary directory' "$BIN_DIR"
  [[ "$TMP_ROOT" == /* ]] || fail "temporary directory must be absolute: $TMP_ROOT"
  contains_control "$TMP_ROOT" && fail 'temporary directory contains control characters'
  [[ "$TMP_ROOT" != "$HOME" ]] || fail "refusing broad temporary directory: $TMP_ROOT"
  case "$TMP_ROOT" in /|/bin|/sbin|/dev|/etc|/proc|/root|/run|/sys|/usr|/usr/bin|/usr/sbin|/usr/local|/var|/opt|/home|/Users|/Library|/Applications) fail "refusing broad temporary directory: $TMP_ROOT" ;; esac

  local labels=('application directory' 'state directory' 'config directory' 'binary directory')
  local paths=("$APP_DIR" "$STATE_DIR" "$CONFIG_DIR" "$BIN_DIR")
  local i j a b
  for ((i=0; i<${#paths[@]}; i++)); do
    for ((j=i+1; j<${#paths[@]}; j++)); do
      a=${paths[$i]}; b=${paths[$j]}
      if path_is_within "$a" "$b" || path_is_within "$b" "$a"; then
        fail "managed directories must not overlap: ${labels[$i]} $a and ${labels[$j]} $b"
      fi
    done
    # A temporary root inside a persistent managed directory could let
    # cleanup traverse installer-created workspaces through that managed
    # tree. A broad temporary parent such as /tmp may safely contain the
    # persistent paths because cleanup always targets unique child paths.
    if path_is_within "$TMP_ROOT" "${paths[$i]}"; then
      fail "temporary directory must not be inside ${labels[$i]}: $TMP_ROOT"
    fi
  done
}


record_created_dir() { CREATED_DIRS+=("$1"); }
record_created_file() { CREATED_FILES+=("$1"); }
append_cleanup() { CLEANUP_PATHS+=("$1"); }
preserve_cleanup_path() { PRESERVED_CLEANUP_PATHS+=("$1"); }
cleanup_path_is_preserved() {
  local candidate=$1 preserved
  for preserved in "${PRESERVED_CLEANUP_PATHS[@]:-}"; do
    [[ -n "$preserved" && "$candidate" == "$preserved" ]] && return 0
  done
  return 1
}

make_sibling_temp() {
  local target=$1 parent base
  parent=$(dirname "$target")
  base=$(basename "$target")
  TEMP_PATH=$(mktemp "$parent/.$base.tmp.XXXXXX") || fail "unable to create temporary file beside $target"
  append_cleanup "$TEMP_PATH"
}

reserve_backup_slot() {
  local target=$1 parent base slot
  parent=$(dirname "$target")
  base=$(basename "$target")
  slot=$(mktemp -d "$parent/.$base.rollback.XXXXXX") || fail "unable to reserve rollback space for $target"
  append_cleanup "$slot"
  BACKUP_SLOT="$slot/original"
}

directory_mode() {
  local target=$1 mode=''
  if mode=$(stat -c '%a' "$target" 2>/dev/null); then
    printf '%s' "$mode"
    return 0
  fi
  if mode=$(stat -f '%Lp' "$target" 2>/dev/null); then
    printf '%s' "$mode"
    return 0
  fi
  return 1
}
directory_is_secure() {
  local target=$1 mode=''
  [[ -d "$target" && ! -L "$target" && -O "$target" ]] || return 1
  mode=$(directory_mode "$target") || return 1
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
  (( (8#$mode & 8#022) == 0 ))
}
secure_owned_directory() {
  local label=$1 target=$2
  if ! "$PYTHON_BIN" -S - "$target" <<'PYCODE' >/dev/null 2>&1
import os
import stat
import sys

path = sys.argv[1]
descriptor = None
try:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    before = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid():
        raise OSError("directory type or ownership is unsafe")
    current_mode = stat.S_IMODE(before.st_mode)
    secured_mode = current_mode & ~0o022
    if secured_mode != current_mode:
        os.fchmod(descriptor, secured_mode)
    after = os.fstat(descriptor)
    current = os.stat(path, follow_symlinks=False)
    before_identity = (before.st_dev, before.st_ino)
    after_identity = (after.st_dev, after.st_ino)
    current_identity = (current.st_dev, current.st_ino)
    if before_identity != after_identity or after_identity != current_identity:
        raise OSError("directory changed while being secured")
    if not stat.S_ISDIR(current.st_mode) or after.st_uid != os.getuid() or current.st_uid != os.getuid():
        raise OSError("directory type or ownership changed")
    if stat.S_IMODE(after.st_mode) & 0o022 or stat.S_IMODE(current.st_mode) & 0o022:
        raise OSError("directory remains group- or other-writable")
finally:
    if descriptor is not None:
        os.close(descriptor)
PYCODE
  then
    fail "$label must be a real current-user directory that can be secured against group/other writes: $target"
  fi
}
validate_existing_managed_directories() {
  local label path
  local labels=('state directory' 'config directory' 'binary directory')
  local paths=("$STATE_DIR" "$CONFIG_DIR" "$BIN_DIR")
  local i
  for ((i=0; i<${#paths[@]}; i++)); do
    label=${labels[$i]}; path=${paths[$i]}
    if [[ -e "$path" || -L "$path" ]]; then secure_owned_directory "$label" "$path"; fi
  done
}

ensure_dir() {
  local target=$1 mode=${2:-$DIR_MODE} current='' part old_ifs=$IFS
  target="$(lexical_absolute_path "$target")"
  local parts=()
  IFS='/' read -r -a parts <<< "$target"
  IFS=$old_ifs
  for part in "${parts[@]}"; do
    [[ -n "$part" ]] || continue
    current="$current/$part"
    if [[ ! -e "$current" && ! -L "$current" ]]; then
      mkdir "$current"
      chmod "$mode" "$current" 2>/dev/null || true
      record_created_dir "$current"
    elif [[ ! -d "$current" ]]; then
      fail "path component is not a directory: $current"
    fi
  done
}

setup_logging() {
  if [[ "$DRY_RUN" == 1 ]]; then
    ensure_dir "$TMP_ROOT" "$DIR_MODE"
    LOG_DIR=$(mktemp -d "$TMP_ROOT/$PRODUCT_SLUG-dry-run.XXXXXX")
    append_cleanup "$LOG_DIR"
  else
    ensure_dir "$STATE_DIR" "$DIR_MODE"
    secure_owned_directory 'state directory' "$STATE_DIR"
    ensure_dir "$LOG_DIR" "$DIR_MODE"
    secure_owned_directory 'log directory' "$LOG_DIR"
  fi
  [[ -O "$LOG_DIR" && -w "$LOG_DIR" ]] || fail "log directory must be owned and writable by the current user: $LOG_DIR"

  local candidate="$LOG_DIR/$LOG_NAME" temp
  if [[ -e "$candidate" || -L "$candidate" ]]; then
    [[ -f "$candidate" && ! -L "$candidate" && -w "$candidate" ]] || fail "log target must be a writable regular file: $candidate"
    candidate=$(mktemp "$LOG_DIR/install.log.XXXXXX") || fail "unable to create installer log in $LOG_DIR"
    chmod "$PRIVATE_MODE" "$candidate" 2>/dev/null || true
    record_created_file "$candidate"
  else
    temp=$(mktemp "$LOG_DIR/.$LOG_NAME.create.XXXXXX") || fail "unable to create installer log in $LOG_DIR"
    chmod "$PRIVATE_MODE" "$temp" 2>/dev/null || true
    if ! ln "$temp" "$candidate" 2>/dev/null; then
      rm -f "$temp"
      fail "log target changed during creation: $candidate"
    fi
    rm -f "$temp"
    record_created_file "$candidate"
  fi
  LOG_FILE=$candidate
  log "installer=$INSTALLER_VERSION platform=$PLATFORM arch=$ARCH dry_run=$DRY_RUN"
}

backup_target() {
  local target=$1 backup=''
  if [[ -e "$target" || -L "$target" ]]; then
    reserve_backup_slot "$target"
    backup=$BACKUP_SLOT
    # Record rollback intent before the destructive rename. The reserved slot
    # prevents same-user collisions without relying on predictable filenames.
    BACKUP_TARGETS+=("$target")
    BACKUP_FILES+=("$backup")
    mv "$target" "$backup"
    return 0
  fi
  BACKUP_TARGETS+=("$target")
  BACKUP_FILES+=("")
}

backup_profile_target() {
  local target=$1 backup=''
  if [[ -e "$target" ]]; then
    reserve_backup_slot "$target"
    backup=$BACKUP_SLOT
    BACKUP_TARGETS+=("$target")
    BACKUP_FILES+=("$backup")
    cp -p "$target" "$backup"
    return 0
  fi
  BACKUP_TARGETS+=("$target")
  BACKUP_FILES+=("")
}


restore_backups() {
  local i target backup slot failed=0
  i=$((${#BACKUP_TARGETS[@]} - 1))
  while (( i >= 0 )); do
    target=${BACKUP_TARGETS[$i]}; backup=${BACKUP_FILES[$i]}
    if [[ -n "$backup" ]]; then
      slot=$(dirname "$backup")
      if [[ ! -e "$backup" && ! -L "$backup" ]]; then
        ROLLBACK_FAILURES+=("missing rollback backup for $target: $backup")
        preserve_cleanup_path "$slot"
        failed=1
      elif [[ ( -e "$target" || -L "$target" ) ]] && ! rm -rf "$target"; then
        ROLLBACK_FAILURES+=("unable to remove failed target $target; previous installation preserved at $backup")
        preserve_cleanup_path "$slot"
        failed=1
      elif ! mv "$backup" "$target"; then
        ROLLBACK_FAILURES+=("unable to restore $target; previous installation preserved at $backup")
        preserve_cleanup_path "$slot"
        failed=1
      fi
    elif [[ ( -e "$target" || -L "$target" ) ]] && ! rm -rf "$target"; then
      ROLLBACK_FAILURES+=("unable to remove newly created target during rollback: $target")
      failed=1
    fi
    i=$((i - 1))
  done
  (( failed == 0 ))
}

cleanup() {
  local status=$? path i rollback_failed=0 failure
  set +e
  if (( status != 0 )) && [[ "$FINALIZED" == 0 ]]; then
    restore_backups || rollback_failed=1
  fi
  for path in "${CLEANUP_PATHS[@]:-}"; do
    [[ -n "$path" ]] || continue
    cleanup_path_is_preserved "$path" && continue
    rm -rf "$path" 2>/dev/null || true
  done
  i=$((${#LOCK_DIRS[@]} - 1))
  while (( i >= 0 )); do
    if [[ -f "${LOCK_DIRS[$i]}/token" ]] && [[ "$(cat "${LOCK_DIRS[$i]}/token" 2>/dev/null)" == "${LOCK_TOKENS[$i]}" ]]; then
      rm -rf "${LOCK_DIRS[$i]}" 2>/dev/null || true
    fi
    i=$((i - 1))
  done
  if [[ "$FINALIZED" == 0 ]]; then
    for path in "${CREATED_FILES[@]:-}"; do [[ -n "$path" ]] && rm -f "$path" 2>/dev/null || true; done
    i=$((${#CREATED_DIRS[@]} - 1)); while (( i >= 0 )); do rmdir "${CREATED_DIRS[$i]}" 2>/dev/null || true; i=$((i - 1)); done
  fi
  if (( rollback_failed != 0 )); then
    for failure in "${ROLLBACK_FAILURES[@]:-}"; do [[ -n "$failure" ]] && printf 'error: %s\n' "$failure" >&2; done
    printf 'error: rollback was incomplete; preserved backup paths must be recovered manually\n' >&2
    status=$ROLLBACK_FAILURE_EXIT
  fi
  exit "$status"
}
on_signal() {
  local code=$1
  if [[ -n "${WATCHDOG_PID:-}" ]]; then
    kill -TERM "$WATCHDOG_PID" 2>/dev/null || true
    wait "$WATCHDOG_PID" 2>/dev/null || true
    WATCHDOG_PID=''
  fi
  if [[ -n "${CHILD_PID:-}" ]]; then
    terminate_interruptible_child TERM "$CHILD_PID"
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=''
    CHILD_PROCESS_GROUP=0
  fi
  warn "installation interrupted during $CURRENT_STEP"
  exit "$code"
}

trap cleanup EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
trap 'on_signal 129' HUP

acquire_single_lock() {
  local lock_dir=$1 lock_parent token pid quarantine_slot quarantine
  lock_parent=$(dirname "$lock_dir")
  token="$$.$RANDOM.$(date +%s 2>/dev/null || printf 0)"
  if mkdir "$lock_dir" 2>/dev/null; then
    printf '%s\n' "$$" > "$lock_dir/pid"
    printf '%s\n' "$token" > "$lock_dir/token"
    LOCK_DIRS+=("$lock_dir")
    LOCK_TOKENS+=("$token")
    return 0
  fi
  pid=$(cat "$lock_dir/pid" 2>/dev/null || true)
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then fail "another install is running: pid $pid"; fi
  quarantine_slot=$(mktemp -d "$lock_parent/.$LOCK_NAME.stale.XXXXXX") || fail "could not reserve stale-lock quarantine in $lock_parent"
  append_cleanup "$quarantine_slot"
  quarantine="$quarantine_slot/lock"
  mv "$lock_dir" "$quarantine" 2>/dev/null || fail "could not take over stale install lock: $lock_dir"
  mkdir "$lock_dir" || fail "could not acquire install lock: $lock_dir"
  printf '%s\n' "$$" > "$lock_dir/pid"
  printf '%s\n' "$token" > "$lock_dir/token"
  LOCK_DIRS+=("$lock_dir")
  LOCK_TOKENS+=("$token")
}

acquire_lock() {
  local app_parent app_lock bin_lock first second
  app_parent=$(dirname "$APP_DIR")
  ensure_dir "$app_parent" "$DIR_MODE"
  ensure_dir "$BIN_DIR" "$DIR_MODE"
  secure_owned_directory 'binary directory' "$BIN_DIR"
  app_lock="$app_parent/.$(basename "$APP_DIR").$LOCK_NAME"
  bin_lock="$BIN_DIR/.$PRODUCT_SLUG.$LOCK_NAME"
  first=$app_lock; second=$bin_lock
  if [[ "$second" < "$first" ]]; then first=$bin_lock; second=$app_lock; fi
  acquire_single_lock "$first"
  [[ "$second" == "$first" ]] || acquire_single_lock "$second"
}

python_version_ok() {
  "$1" -S - "$MINIMUM_PYTHON_VERSION" <<'PY' >/dev/null 2>&1
import sys
major, minor = map(int, sys.argv[1].split('.'))
raise SystemExit(0 if sys.version_info >= (major, minor) else 1)
PY
}
find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
    [[ -n "$candidate" ]] || continue
    if has "$candidate"; then candidate=$(command -v "$candidate"); fi
    if [[ ! -x "$candidate" ]] || ! python_version_ok "$candidate"; then continue; fi
    PYTHON_BIN=$candidate; return 0
  done
  return 1
}
install_python() {
  if [[ "$DRY_RUN" == 1 ]]; then return 1; fi
  if [[ "$PLATFORM" == darwin ]] && has brew; then
    run_interruptible_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" brew install python
    return
  fi
  if has apt-get; then
    local apt_network_options=(
      -o "Acquire::Retries=$NETWORK_RETRIES"
      -o "Acquire::http::Timeout=$NETWORK_CONNECT_TIMEOUT"
      -o "Acquire::https::Timeout=$NETWORK_CONNECT_TIMEOUT"
      -o "Acquire::ftp::Timeout=$NETWORK_CONNECT_TIMEOUT"
      -o "Dpkg::Lock::Timeout=$NETWORK_CONNECT_TIMEOUT"
    )
    run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" apt-get "${apt_network_options[@]}" update -qq \
      && run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" apt-get "${apt_network_options[@]}" install -y -qq python3
    return
  fi
  if has dnf; then run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" dnf install -y python3; return; fi
  if has yum; then run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" yum install -y python3; return; fi
  if has pacman; then run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" pacman -S --noconfirm --needed python; return; fi
  if has zypper; then run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" zypper --non-interactive install python3; return; fi
  if has apk; then run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" apk add python3; return; fi
  if has pkg; then run_privileged_timeout "$PACKAGE_MANAGER_TOTAL_TIMEOUT" pkg install -y python3; return; fi
  return 1
}

ensure_python() {
  if find_python; then return 0; fi
  [[ "$DRY_RUN" == 0 ]] || fail "Python $MINIMUM_PYTHON_VERSION+ is required for dry-run validation"
  warn "Python $MINIMUM_PYTHON_VERSION+ is missing; attempting system package installation"
  install_python || fail "Python $MINIMUM_PYTHON_VERSION+ could not be installed automatically"
  find_python || fail "Python $MINIMUM_PYTHON_VERSION+ remains unavailable"
}

sha256_file() {
  "$PYTHON_BIN" -S - "$1" <<'PY'
import hashlib, sys
h=hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
print(h.hexdigest())
PY
}
normalize_sha() {
  local value=$1 label=$2
  NORMALIZED_SHA=$(printf '%s' "$value" | tr 'A-F' 'a-f')
  [[ "$NORMALIZED_SHA" =~ ^[0-9a-f]{64}$ ]] || fail "invalid $label SHA-256 digest"
}
verify_sha() {
  local file=$1 expected=$2 label=$3 actual
  normalize_sha "$expected" "$label"; expected=$NORMALIZED_SHA
  actual=$(sha256_file "$file")
  [[ "$actual" == "$expected" ]] || fail "$label checksum mismatch"
}

validate_url() {
  local url=$1
  contains_control "$url" && fail 'source URL contains control characters'
  if ! "$PYTHON_BIN" -S - "$url" <<'PY' >/dev/null 2>&1
import sys
from urllib.parse import urlsplit
url=sys.argv[1]
if any(ch.isspace() or ord(ch)==0x7f for ch in url) or '\\' in url:
    raise SystemExit(1)
try:
    parsed=urlsplit(url)
    _=parsed.port
except ValueError:
    raise SystemExit(1)
valid=(
    parsed.scheme=='https' and bool(parsed.hostname)
    and parsed.username is None and parsed.password is None
    and not parsed.query and not parsed.fragment
    and parsed.path.startswith('/') and len(parsed.path)>1
    and not parsed.path.endswith('/')
)
raise SystemExit(0 if valid else 1)
PY
  then
    fail 'source URL must be an absolute HTTPS file URL without credentials, whitespace, query, fragment, or trailing slash'
  fi
}


start_interruptible_child() {
  local restore_monitor=0
  if [[ $- != *m* ]]; then
    set -m
    restore_monitor=1
  fi
  # Explicitly duplicate stdin so backgrounded commands still receive caller
  # heredocs instead of Bash's default /dev/null for asynchronous processes.
  "$@" <&0 &
  CHILD_PID=$!
  CHILD_PROCESS_GROUP=1
  if [[ "$restore_monitor" == 1 ]]; then
    set +m
  fi
}

terminate_interruptible_child() {
  local signal_name=$1 child=$2
  [[ "$child" =~ ^[0-9]+$ ]] || return 0
  if [[ "$CHILD_PROCESS_GROUP" == 1 ]]; then
    # Job control gives the child its own process group. Signal the entire
    # group so package-manager helpers and other descendants cannot survive a
    # timeout or interrupted installation. Fall back to the direct PID if the
    # group has already disappeared or the platform rejects group signalling.
    kill -"$signal_name" -- "-$child" 2>/dev/null || kill -"$signal_name" "$child" 2>/dev/null || true
  else
    kill -"$signal_name" "$child" 2>/dev/null || true
  fi
}

run_interruptible() {
  local status=0
  start_interruptible_child "$@"
  wait "$CHILD_PID" || status=$?
  CHILD_PID=''
  CHILD_PROCESS_GROUP=0
  return "$status"
}

run_interruptible_timeout() {
  local duration=$1 status=0 child watchdog
  shift
  start_interruptible_child "$@"
  child=$CHILD_PID
  (
    sleep "$duration"
    if kill -0 "$child" 2>/dev/null; then
      terminate_interruptible_child TERM "$child"
      sleep 5
      terminate_interruptible_child KILL "$child"
    fi
  ) &
  WATCHDOG_PID=$!
  watchdog=$WATCHDOG_PID
  wait "$child" || status=$?
  kill -TERM "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  WATCHDOG_PID=''
  CHILD_PID=''
  CHILD_PROCESS_GROUP=0
  return "$status"
}

run_privileged_timeout() {
  local duration=$1
  shift
  if [[ $(id -u) == 0 ]]; then
    run_interruptible_timeout "$duration" "$@"
  elif has sudo; then
    run_interruptible_timeout "$duration" sudo "$@"
  elif has doas; then
    run_interruptible_timeout "$duration" doas "$@"
  else
    return 127
  fi
}

download_file() {
  local url=$1 output=$2 partial status=0 size
  make_sibling_temp "$output"
  partial=$TEMP_PATH
  if has curl && curl --help all 2>/dev/null | grep -Fq -- '--max-filesize'; then
    run_interruptible curl --fail --location --silent --show-error \
      --connect-timeout "$NETWORK_CONNECT_TIMEOUT" \
      --max-time "$NETWORK_TOTAL_TIMEOUT" \
      --retry "$NETWORK_RETRIES" \
      --proto '=https' --proto-redir '=https' \
      --max-filesize "$MAX_DOWNLOAD_BYTES" \
      --output "$partial" "$url" || status=$?
  else
    run_interruptible "$PYTHON_BIN" -S - \
      "$url" "$partial" "$NETWORK_CONNECT_TIMEOUT" "$NETWORK_TOTAL_TIMEOUT" \
      "$MAX_DOWNLOAD_BYTES" "$NETWORK_RETRIES" <<'PY' || status=$?
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

url, output = sys.argv[1:3]
connect_timeout = float(sys.argv[3])
total_timeout = float(sys.argv[4])
limit = int(sys.argv[5])
retries = int(sys.argv[6])
deadline = time.monotonic() + total_timeout

class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if urllib.parse.urlsplit(target).scheme.lower() != "https":
            raise urllib.error.HTTPError(target, code, "non-HTTPS redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)

def alarm_handler(_signum, _frame):
    raise TimeoutError("download total timeout exceeded")

def remove_partial():
    try:
        os.unlink(output)
    except FileNotFoundError:
        pass

signal.signal(signal.SIGALRM, alarm_handler)
opener = urllib.request.build_opener(HTTPSOnly())
request = urllib.request.Request(url, headers={"User-Agent": "scan-dir-installer"})
last_error = None
try:
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = TimeoutError("download total timeout exceeded")
            break
        remove_partial()
        try:
            signal.setitimer(signal.ITIMER_REAL, remaining)
            with opener.open(request, timeout=min(connect_timeout, remaining)) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        announced = int(length)
                    except ValueError as exc:
                        raise RuntimeError("invalid Content-Length") from exc
                    if announced < 0 or announced > limit:
                        raise RuntimeError("download size limit exceeded")
                total = 0
                descriptor = os.open(
                    output,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("download total timeout exceeded")
                        chunk = response.read(min(1024 * 1024, limit - total + 1))
                        if time.monotonic() >= deadline:
                            raise TimeoutError("download total timeout exceeded")
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise RuntimeError("download size limit exceeded")
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            if total == 0:
                raise RuntimeError("download was empty")
            raise SystemExit(0)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code not in {408, 429}:
                break
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
        except RuntimeError as exc:
            last_error = exc
            break
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        remove_partial()
        if attempt < retries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(float(2**attempt), remaining))
finally:
    signal.setitimer(signal.ITIMER_REAL, 0)
remove_partial()
raise SystemExit(str(last_error or "download failed"))
PY
  fi
  if [[ "$status" != 0 || ! -s "$partial" ]]; then rm -f "$partial"; return 1; fi
  size=$(wc -c < "$partial" | tr -d '[:space:]')
  if [[ ! "$size" =~ ^[0-9]+$ ]] || (( size > MAX_DOWNLOAD_BYTES )); then
    rm -f "$partial"
    return 1
  fi
  mv "$partial" "$output"
}


canonical_file() {
  local input=$1 parent base
  input="$(lexical_absolute_path "$input")"
  parent=$(physical_path "$(dirname "$input")") || return 1
  base=$(basename "$input")
  printf '%s/%s' "${parent%/}" "$base"
}

snapshot_local_file() {
  local source=$1 destination=$2 label=$3
  if ! "$PYTHON_BIN" -S - "$source" "$destination" "$MAX_DOWNLOAD_BYTES" <<'PYCODE' >/dev/null 2>&1
import os, stat, sys
source, destination, maximum = sys.argv[1], sys.argv[2], int(sys.argv[3])
source_fd = output_fd = None
try:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    before = os.fstat(source_fd)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("source is not regular")
    output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    total = 0
    while True:
        chunk = os.read(source_fd, min(1024 * 1024, maximum - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise OSError("source exceeds size limit")
        view = memoryview(chunk)
        while view:
            written = os.write(output_fd, view)
            view = view[written:]
    os.fsync(output_fd)
    after = os.fstat(source_fd)
    current = os.stat(source, follow_symlinks=False)
    before_sig = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_sig = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    current_sig = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
    if before_sig != after_sig or after_sig != current_sig:
        raise OSError("source changed while being copied")
except (OSError, ValueError):
    try:
        os.unlink(destination)
    except OSError:
        pass
    raise SystemExit(1)
finally:
    if output_fd is not None:
        os.close(output_fd)
    if source_fd is not None:
        os.close(source_fd)
PYCODE
  then
    fail "unable to snapshot local $label package file"
  fi
  [[ -f "$destination" && ! -L "$destination" ]] || fail "local $label snapshot is missing"
}

resolve_local_package() {
  local runtime=$1 dir source_runtime source_config source_skill
  contains_control "$runtime" && fail 'local source path contains control characters'
  source_runtime=$(canonical_file "$runtime") || fail "unable to resolve local source: $runtime"
  [[ -f "$source_runtime" && -r "$source_runtime" && ! -L "$source_runtime" ]] || fail "local runtime must be a readable regular file: $source_runtime"
  dir=$(dirname "$source_runtime")
  source_config="$dir/$CONFIG_FILE"
  source_skill="$dir/$SKILL_FILE"
  [[ -f "$source_config" && -r "$source_config" && ! -L "$source_config" ]] || fail "local package is missing $CONFIG_FILE"
  [[ -f "$source_skill" && -r "$source_skill" && ! -L "$source_skill" ]] || fail "local package is missing $SKILL_FILE"

  RESOLVED_RUNTIME="$WORK_DIR/$RUNTIME_FILE"
  RESOLVED_CONFIG="$WORK_DIR/$CONFIG_FILE"
  RESOLVED_SKILL="$WORK_DIR/$SKILL_FILE"
  snapshot_local_file "$source_runtime" "$RESOLVED_RUNTIME" runtime
  snapshot_local_file "$source_config" "$RESOLVED_CONFIG" config
  snapshot_local_file "$source_skill" "$RESOLVED_SKILL" skill
  [[ -z "$RUNTIME_SHA256" ]] || verify_sha "$RESOLVED_RUNTIME" "$RUNTIME_SHA256" runtime
  [[ -z "$CONFIG_SHA256" ]] || verify_sha "$RESOLVED_CONFIG" "$CONFIG_SHA256" config
  [[ -z "$SKILL_SHA256" ]] || verify_sha "$RESOLVED_SKILL" "$SKILL_SHA256" skill
}

resolve_remote_package() {
  local url=$1 base
  validate_url "$url"
  if [[ "$url" == "$DEFAULT_SOURCE_URL" ]]; then
    [[ "$DEFAULT_RUNTIME_SHA256" != __*__ && "$DEFAULT_CONFIG_SHA256" != __*__ && "$DEFAULT_SKILL_SHA256" != __*__ ]] || fail 'canonical package digests are not embedded'
    RUNTIME_SHA256=$DEFAULT_RUNTIME_SHA256; CONFIG_SHA256=$DEFAULT_CONFIG_SHA256; SKILL_SHA256=$DEFAULT_SKILL_SHA256
  else
    [[ -n "$RUNTIME_SHA256" && -n "$CONFIG_SHA256" && -n "$SKILL_SHA256" ]] || fail 'custom remote packages require --sha256, --config-sha256, and --skill-sha256'
  fi
  normalize_sha "$RUNTIME_SHA256" runtime; RUNTIME_SHA256=$NORMALIZED_SHA
  normalize_sha "$CONFIG_SHA256" config; CONFIG_SHA256=$NORMALIZED_SHA
  normalize_sha "$SKILL_SHA256" skill; SKILL_SHA256=$NORMALIZED_SHA
  base=${url%/*}
  RESOLVED_RUNTIME="$WORK_DIR/$RUNTIME_FILE"
  RESOLVED_CONFIG="$WORK_DIR/$CONFIG_FILE"
  RESOLVED_SKILL="$WORK_DIR/$SKILL_FILE"
  download_file "$url" "$RESOLVED_RUNTIME" || fail "unable to download $url"
  download_file "$base/$CONFIG_FILE" "$RESOLVED_CONFIG" || fail "unable to download $base/$CONFIG_FILE"
  download_file "$base/$SKILL_FILE" "$RESOLVED_SKILL" || fail "unable to download $base/$SKILL_FILE"
  verify_sha "$RESOLVED_RUNTIME" "$RUNTIME_SHA256" runtime
  verify_sha "$RESOLVED_CONFIG" "$CONFIG_SHA256" config
  verify_sha "$RESOLVED_SKILL" "$SKILL_SHA256" skill
}

create_workspace() {
  ensure_dir "$TMP_ROOT" "$DIR_MODE"
  WORK_DIR=$(mktemp -d "$TMP_ROOT/$PRODUCT_SLUG.XXXXXX")
  append_cleanup "$WORK_DIR"
}

resolve_package() {
  if [[ -n "$SOURCE_PATH" ]]; then resolve_local_package "$SOURCE_PATH"; return; fi
  if [[ -n "$SOURCE_URL" ]]; then resolve_remote_package "$SOURCE_URL"; return; fi
  if [[ -n "$SCRIPT_DIR" ]]; then
    local present=0
    [[ -e "$SCRIPT_DIR/$RUNTIME_FILE" || -L "$SCRIPT_DIR/$RUNTIME_FILE" ]] && present=$((present + 1))
    [[ -e "$SCRIPT_DIR/$CONFIG_FILE" || -L "$SCRIPT_DIR/$CONFIG_FILE" ]] && present=$((present + 1))
    [[ -e "$SCRIPT_DIR/$SKILL_FILE" || -L "$SCRIPT_DIR/$SKILL_FILE" ]] && present=$((present + 1))
    if [[ "$present" == 3 ]]; then
      resolve_local_package "$SCRIPT_DIR/$RUNTIME_FILE"
      return
    fi
    [[ "$present" == 0 ]] || fail "local installer package is incomplete beside $SCRIPT_PATH"
  fi
  resolve_remote_package "$DEFAULT_SOURCE_URL"
}


require_version() {
  local actual=$1 context=$2 expected="$PRIMARY_COMMAND $INSTALLER_VERSION"
  [[ "$actual" == "$expected" ]] || fail "$context version mismatch: expected '$expected', got '$actual'"
}

validate_package() {
  "$PYTHON_BIN" -S - "$RESOLVED_RUNTIME" <<'PY'
import pathlib, sys
p=pathlib.Path(sys.argv[1]); compile(p.read_text(encoding='utf-8'),str(p),'exec')
PY
  require_version "$(PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -S "$RESOLVED_RUNTIME" version)" source
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -S "$RESOLVED_RUNTIME" help >/dev/null
  local fixture output
  fixture=$(mktemp -d "$WORK_DIR/fixture.XXXXXX"); printf 'demo\n' > "$fixture/README.md"
  output=$(SDIR_CONFIG_DIR="$CONFIG_DIR" PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -S "$RESOLVED_RUNTIME" "$fixture" --config "$RESOLVED_CONFIG" --only -e .md --include-hidden --include-empty --scan-data 'tree,summary' --scan-styling minimal --scan-emojis false --auto-copy false)
  [[ "$output" == *README.md* && "$output" == *largest* ]] || fail 'source runtime validation failed'
}

version_compare() {
  "$PYTHON_BIN" -S - "$1" "$2" <<'PY'
import re,sys
def parse(s):
    if not re.fullmatch(r'\d+(?:\.\d+)*',s): raise SystemExit(2)
    return tuple(map(int,s.split('.')))
a,b=parse(sys.argv[1]),parse(sys.argv[2]); n=max(len(a),len(b)); a+= (0,)*(n-len(a)); b+=(0,)*(n-len(b))
print(-1 if a<b else 1 if a>b else 0)
PY
}

read_manifest() {
  MANIFEST_VALID=0; MANIFEST_PRODUCT=''; MANIFEST_VERSION=''; MANIFEST_APP_DIR=''; MANIFEST_BIN_DIR=''
  MANIFEST_STATE_DIR=''; MANIFEST_CONFIG_DIR=''
  MANIFEST_RUNTIME_SHA=''; MANIFEST_CONFIG_SHA=''; MANIFEST_SKILL_SHA=''
  MANIFEST_PRIMARY_SHA=''; MANIFEST_ALIAS_SHA=''
  local file="$APP_DIR/$MANIFEST_FILE" key value seen='|'
  [[ -f "$file" && ! -L "$file" ]] || return 0
  while IFS='=' read -r key value || [[ -n "$key$value" ]]; do
    [[ "$key" =~ ^[a-z_][a-z0-9_]*$ ]] || return 0
    contains_control "$value" && return 0
    case "$seen" in *"|$key|"*) return 0 ;; esac; seen="$seen$key|"
    case "$key" in
      product) MANIFEST_PRODUCT=$value ;;
      installer_version) MANIFEST_VERSION=$value ;;
      app_dir) MANIFEST_APP_DIR=$value ;;
      bin_dir) MANIFEST_BIN_DIR=$value ;;
      state_dir) MANIFEST_STATE_DIR=$value ;;
      config_dir) MANIFEST_CONFIG_DIR=$value ;;
      runtime_sha256) MANIFEST_RUNTIME_SHA=$value ;;
      config_sha256) MANIFEST_CONFIG_SHA=$value ;;
      skill_sha256) MANIFEST_SKILL_SHA=$value ;;
      primary_wrapper_sha256) MANIFEST_PRIMARY_SHA=$value ;;
      alias_wrapper_sha256) MANIFEST_ALIAS_SHA=$value ;;
      installed_at|platform|arch|cache_dir) : ;;
      *) : ;;
    esac
  done < "$file"
  [[ "$MANIFEST_PRODUCT" == "$PRODUCT_SLUG" ]] || return 0
  [[ -n "$MANIFEST_VERSION" ]] || return 0
  [[ "$MANIFEST_APP_DIR" == "$APP_DIR" ]] || return 0
  [[ "$MANIFEST_BIN_DIR" == "$BIN_DIR" ]] || return 0
  [[ -n "$MANIFEST_STATE_DIR" && -n "$MANIFEST_CONFIG_DIR" ]] || return 0
  MANIFEST_VALID=1
}

read_install_metadata() {
  local version_file="$APP_DIR/$VERSION_FILE" value=''
  INSTALLED_VERSION=''
  INSTALLED_VERSION_UNKNOWN=0
  read_manifest
  if [[ "$MANIFEST_VALID" == 1 ]]; then
    INSTALLED_VERSION=$MANIFEST_VERSION
    return 0
  fi
  if file_equals_line "$APP_DIR/$MANAGED_FILE" "$MANAGED_MARKER"; then
    if [[ -f "$version_file" && ! -L "$version_file" ]] && value=$(cat "$version_file" 2>/dev/null); then
      if [[ -n "$value" ]] && ! contains_control "$value"; then
        INSTALLED_VERSION=$value
        return 0
      fi
    fi
    INSTALLED_VERSION_UNKNOWN=1
  fi
}

file_equals_line() {
  local path=$1 expected=$2 actual=''
  [[ -f "$path" && ! -L "$path" ]] || return 1
  actual=$(cat "$path" 2>/dev/null) || return 1
  [[ "$actual" == "$expected" ]]
}

integrity_matches() {
  local path=$1 expected=$2
  [[ -n "$expected" && -f "$path" && ! -L "$path" ]] || return 1
  [[ "$(sha256_file "$path")" == "$expected" ]]
}

managed_layout_matches() {
  "$PYTHON_BIN" -S - \
    "$APP_DIR" "$STATE_DIR" "$CONFIG_DIR" "$BIN_DIR" \
    "$BIN_DIR/$PRIMARY_COMMAND" "$BIN_DIR/$ALIAS_COMMAND" \
    "$RUNTIME_FILE" "$CONFIG_FILE" "$SKILL_FILE" "$VERSION_FILE" \
    "$MANAGED_FILE" "$MANIFEST_FILE" <<'PY'
import os
import stat
import sys

app, state_dir, config_dir, bin_dir, primary, alias, runtime, config, skill, version, managed, manifest = sys.argv[1:]
uid = os.getuid()
expected = {
    runtime: 0o755,
    config: 0o644,
    skill: 0o644,
    version: 0o644,
    managed: 0o644,
    manifest: 0o600,
}

def exact(path, kind, mode):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode:
        return False
    return kind(info.st_mode)

def secure_dir(path, exact_mode=None):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != uid or not stat.S_ISDIR(info.st_mode) or mode & 0o022:
        return False
    return exact_mode is None or mode == exact_mode

if not secure_dir(app, 0o700):
    raise SystemExit(1)
for directory in (state_dir, config_dir, bin_dir):
    if not secure_dir(directory):
        raise SystemExit(1)
try:
    if set(os.listdir(app)) != set(expected):
        raise SystemExit(1)
except OSError:
    raise SystemExit(1)
for name, mode in expected.items():
    if not exact(os.path.join(app, name), stat.S_ISREG, mode):
        raise SystemExit(1)
for wrapper in (primary, alias):
    if not exact(wrapper, stat.S_ISREG, 0o755):
        raise SystemExit(1)
PY
}

installed_is_intact() {
  [[ "$MANIFEST_VALID" == 1 && "$MANIFEST_VERSION" == "$INSTALLER_VERSION" ]] || return 1
  [[ "$MANIFEST_STATE_DIR" == "$STATE_DIR" && "$MANIFEST_CONFIG_DIR" == "$CONFIG_DIR" ]] || return 1
  file_equals_line "$APP_DIR/$VERSION_FILE" "$INSTALLER_VERSION" || return 1
  file_equals_line "$APP_DIR/$MANAGED_FILE" "$MANAGED_MARKER" || return 1
  managed_layout_matches || return 1
  integrity_matches "$APP_DIR/$RUNTIME_FILE" "$MANIFEST_RUNTIME_SHA" || return 1
  integrity_matches "$APP_DIR/$CONFIG_FILE" "$MANIFEST_CONFIG_SHA" || return 1
  integrity_matches "$APP_DIR/$SKILL_FILE" "$MANIFEST_SKILL_SHA" || return 1
  integrity_matches "$BIN_DIR/$PRIMARY_COMMAND" "$MANIFEST_PRIMARY_SHA" || return 1
  integrity_matches "$BIN_DIR/$ALIAS_COMMAND" "$MANIFEST_ALIAS_SHA" || return 1
  [[ "$("$BIN_DIR/$PRIMARY_COMMAND" version 2>/dev/null || true)" == "$PRIMARY_COMMAND $INSTALLER_VERSION" ]]
}

is_managed_bridge() {
  local target=$1 launcher=$2 actual='' expected=''
  [[ -f "$target" && ! -L "$target" ]] || return 1
  actual=$(cat "$target" 2>/dev/null) || return 1
  expected=$(printf '#!/usr/bin/env bash\n# %s\nexec %s "$@"' "$BRIDGE_MARKER" "$(shell_quote "$launcher")")
  [[ "$actual" == "$expected" ]]
}
is_managed_wrapper() {
  local target=$1 command_name=$2 quoted_app plain_app source_line exec_line dollar='$'
  [[ -f "$target" && ! -L "$target" ]] || return 1
  quoted_app="APP_DIR=$(shell_quote "$APP_DIR")"
  plain_app="APP_DIR=$APP_DIR"
  grep -Fqx '#!/usr/bin/env bash' "$target" 2>/dev/null || return 1
  grep -Fqx "# $command_name -- Scan Dir command" "$target" 2>/dev/null || return 1
  grep -Fqx "# $MANAGED_MARKER" "$target" 2>/dev/null || return 1
  if ! grep -Fqx "$quoted_app" "$target" 2>/dev/null && ! grep -Fqx "$plain_app" "$target" 2>/dev/null; then
    return 1
  fi
  source_line="SOURCE_FILE=\"${dollar}APP_DIR/sdir.py\""
  exec_line="exec \"${dollar}PYTHON_BIN\" -S \"${dollar}SOURCE_FILE\" \"${dollar}@\""
  grep -Fqx "$source_line" "$target" 2>/dev/null || return 1
  grep -Fqx "$exec_line" "$target" 2>/dev/null
}
manifest_owns_command() {
  local target=$1
  [[ "$MANIFEST_VALID" == 1 && "$MANIFEST_APP_DIR" == "$APP_DIR" && "$MANIFEST_BIN_DIR" == "$BIN_DIR" ]] || return 1
  file_equals_line "$APP_DIR/$MANAGED_FILE" "$MANAGED_MARKER" || return 1
  [[ "$target" == "$BIN_DIR/$PRIMARY_COMMAND" || "$target" == "$BIN_DIR/$ALIAS_COMMAND" ]]
}
ensure_app_replaceable() {
  [[ ! -e "$APP_DIR" && ! -L "$APP_DIR" ]] && return 0
  file_equals_line "$APP_DIR/$MANAGED_FILE" "$MANAGED_MARKER" && return 0
  [[ "$FORCE" == 1 ]] && return 0
  fail "refusing to replace foreign runtime directory without --force: $APP_DIR"
}
ensure_replaceable() {
  local target=$1 command_name
  [[ ! -e "$target" && ! -L "$target" ]] && return 0
  manifest_owns_command "$target" && return 0
  command_name=$(basename "$target")
  is_managed_wrapper "$target" "$command_name" && return 0
  [[ "$FORCE" == 1 ]] && return 0
  fail "refusing to replace foreign command without --force: $target"
}

shell_quote() { printf '%q' "$1"; }
write_wrapper() {
  local target=$1 command_name=$2 app_dir=$3 python_bin=$4 config_dir=$5 temp
  make_sibling_temp "$target"
  temp=$TEMP_PATH
  cat > "$temp" <<EOF
#!/usr/bin/env bash
# $command_name -- Scan Dir command
# $MANAGED_MARKER
set -Eeuo pipefail
APP_DIR=$(shell_quote "$app_dir")
PYTHON_BIN=$(shell_quote "$python_bin")
export SDIR_CONFIG_DIR=$(shell_quote "$config_dir")
SOURCE_FILE="\$APP_DIR/$RUNTIME_FILE"
if [[ ! -r "\$SOURCE_FILE" ]]; then printf 'sdir: managed runtime missing: %s\\n' "\$SOURCE_FILE" >&2; exit 127; fi
if [[ ! -x "\$PYTHON_BIN" ]] || ! "\$PYTHON_BIN" -S - '$MINIMUM_PYTHON_VERSION' <<'PY' >/dev/null 2>&1
import sys
v=tuple(map(int,sys.argv[1].split('.')))
raise SystemExit(0 if sys.version_info>=v else 1)
PY
then
  for candidate in python3 python; do
    if command -v "\$candidate" >/dev/null 2>&1 && "\$candidate" -S - '$MINIMUM_PYTHON_VERSION' <<'PY' >/dev/null 2>&1
import sys
v=tuple(map(int,sys.argv[1].split('.')))
raise SystemExit(0 if sys.version_info>=v else 1)
PY
    then PYTHON_BIN="\$(command -v "\$candidate")"; break; fi
  done
fi
if [[ ! -x "\$PYTHON_BIN" ]] || ! "\$PYTHON_BIN" -S - '$MINIMUM_PYTHON_VERSION' <<'PY' >/dev/null 2>&1
import sys
v=tuple(map(int,sys.argv[1].split('.')))
raise SystemExit(0 if sys.version_info>=v else 1)
PY
then printf 'sdir: Python $MINIMUM_PYTHON_VERSION+ is unavailable. Re-run install.sh.\\n' >&2; exit 127; fi
export PYTHONDONTWRITEBYTECODE=1
exec "\$PYTHON_BIN" -S "\$SOURCE_FILE" "\$@"
EOF
  chmod "$EXEC_MODE" "$temp"
  mv "$temp" "$target"
}

stage_package() {
  local stage_parent
  if [[ "$DRY_RUN" == 1 ]]; then stage_parent=$WORK_DIR; else stage_parent=$(dirname "$APP_DIR"); ensure_dir "$stage_parent" "$DIR_MODE"; fi
  STAGED_APP_DIR=$(mktemp -d "$stage_parent/.$PRODUCT_SLUG.stage.XXXXXX")
  append_cleanup "$STAGED_APP_DIR"
  cp "$RESOLVED_RUNTIME" "$STAGED_APP_DIR/$RUNTIME_FILE"
  cp "$RESOLVED_CONFIG" "$STAGED_APP_DIR/$CONFIG_FILE"
  cp "$RESOLVED_SKILL" "$STAGED_APP_DIR/$SKILL_FILE"
  chmod "$EXEC_MODE" "$STAGED_APP_DIR/$RUNTIME_FILE"
  chmod "$DATA_MODE" "$STAGED_APP_DIR/$CONFIG_FILE" "$STAGED_APP_DIR/$SKILL_FILE"
  printf '%s\n' "$INSTALLER_VERSION" > "$STAGED_APP_DIR/$VERSION_FILE"
  printf '%s\n' "$MANAGED_MARKER" > "$STAGED_APP_DIR/$MANAGED_FILE"
  chmod "$DATA_MODE" "$STAGED_APP_DIR/$VERSION_FILE" "$STAGED_APP_DIR/$MANAGED_FILE"
  require_version "$(SDIR_CONFIG_DIR="$CONFIG_DIR" "$PYTHON_BIN" -S "$STAGED_APP_DIR/$RUNTIME_FILE" version)" staged
}

commit_package() {
  [[ "$DRY_RUN" == 0 ]] || return 0
  ensure_app_replaceable
  ensure_dir "$BIN_DIR" "$DIR_MODE"
  secure_owned_directory 'binary directory' "$BIN_DIR"
  ensure_dir "$CONFIG_DIR" "$DIR_MODE"
  secure_owned_directory 'config directory' "$CONFIG_DIR"
  ensure_replaceable "$BIN_DIR/$PRIMARY_COMMAND"
  ensure_replaceable "$BIN_DIR/$ALIAS_COMMAND"
  backup_target "$APP_DIR"
  mv "$STAGED_APP_DIR" "$APP_DIR"; STAGED_APP_DIR=''
  backup_target "$BIN_DIR/$PRIMARY_COMMAND"
  backup_target "$BIN_DIR/$ALIAS_COMMAND"
  write_wrapper "$BIN_DIR/$PRIMARY_COMMAND" "$PRIMARY_COMMAND" "$APP_DIR" "$PYTHON_BIN" "$CONFIG_DIR"
  write_wrapper "$BIN_DIR/$ALIAS_COMMAND" "$ALIAS_COMMAND" "$APP_DIR" "$PYTHON_BIN" "$CONFIG_DIR"
}

path_has_dir() {
  local needle=$1 path_value=${2:-$ORIGINAL_PATH} entry physical old_ifs=$IFS
  local entries=()
  needle=$(physical_path "$needle" 2>/dev/null || true)
  [[ -n "$needle" ]] || return 1
  IFS=':' read -r -a entries <<< "$path_value"
  IFS=$old_ifs
  for entry in "${entries[@]}"; do
    [[ -n "$entry" && "$entry" == /* ]] || continue
    contains_control "$entry" && continue
    physical=$(physical_path "$entry" 2>/dev/null || true)
    [[ -n "$physical" && "$physical" == "$needle" ]] && return 0
  done
  return 1
}
safe_active_path_dir() {
  local entry physical conventional allowed old_ifs=$IFS
  local entries=() conventional_dirs=("$HOME/.local/bin" "$HOME/bin")
  if [[ -n "${XDG_BIN_HOME:-}" && "$XDG_BIN_HOME" == /* ]]; then
    conventional_dirs+=("$XDG_BIN_HOME")
  fi
  IFS=':' read -r -a entries <<< "$ORIGINAL_PATH"
  IFS=$old_ifs
  for entry in "${entries[@]}"; do
    [[ -n "$entry" && "$entry" == /* && -d "$entry" && -w "$entry" ]] || continue
    contains_control "$entry" && continue
    physical=$(physical_path "$entry" 2>/dev/null || true)
    [[ -n "$physical" ]] || continue
    directory_is_secure "$physical" || continue
    path_is_within "$physical" "$HOME" || continue
    [[ "$physical" != "$BIN_DIR" ]] || continue
    allowed=0
    for conventional in "${conventional_dirs[@]}"; do
      conventional=$(physical_path "$conventional" 2>/dev/null || true)
      [[ -n "$conventional" && "$physical" == "$conventional" ]] && allowed=1
    done
    if is_managed_bridge "$physical/$PRIMARY_COMMAND" "$BIN_DIR/$PRIMARY_COMMAND" || is_managed_bridge "$physical/$ALIAS_COMMAND" "$BIN_DIR/$ALIAS_COMMAND"; then
      allowed=1
    fi
    [[ "$allowed" == 1 ]] || continue
    printf '%s' "$physical"
    return 0
  done
  return 1
}
write_bridge_if_safe() {
  local command_name=$1 directory=$2 target launcher temp
  target="$directory/$command_name"
  launcher="$BIN_DIR/$command_name"
  if [[ -e "$target" || -L "$target" ]]; then
    if [[ "$target" -ef "$launcher" ]] 2>/dev/null || is_managed_bridge "$target" "$launcher"; then :; else warn "active PATH command left unchanged: $target"; return 0; fi
  fi
  backup_target "$target"
  make_sibling_temp "$target"
  temp=$TEMP_PATH
  cat > "$temp" <<EOF
#!/usr/bin/env bash
# $BRIDGE_MARKER
exec $(shell_quote "$launcher") "\$@"
EOF
  chmod "$EXEC_MODE" "$temp"; mv "$temp" "$target"
}
create_active_bridge() {
  [[ "$DRY_RUN" == 0 && "$NO_ACTIVE_BRIDGE" == 0 ]] || return 0
  path_has_dir "$BIN_DIR" && return 0
  local directory; directory=$(safe_active_path_dir || true)
  [[ -n "$directory" ]] || return 0
  write_bridge_if_safe "$PRIMARY_COMMAND" "$directory"
  write_bridge_if_safe "$ALIAS_COMMAND" "$directory"
}

profile_candidates() {
  case "$(basename "${SHELL:-sh}")" in
    zsh)
      printf '%s
' "${ZDOTDIR:-$HOME}/.zshrc" "${ZDOTDIR:-$HOME}/.zprofile"
      ;;
    bash)
      printf '%s
' "$HOME/.bashrc"
      if [[ -e "$HOME/.bash_profile" || -L "$HOME/.bash_profile" ]]; then
        printf '%s
' "$HOME/.bash_profile"
      else
        printf '%s
' "$HOME/.profile"
      fi
      ;;
    fish)
      printf '%s
' "$HOME/.config/fish/conf.d/$PRODUCT_SLUG.fish"
      ;;
    *)
      printf '%s
' "$HOME/.profile"
      ;;
  esac
}
resolve_profile_target() {
  local profile=$1 resolved parent
  contains_control "$profile" && return 1
  if [[ -L "$profile" ]]; then
    resolved=$("$PYTHON_BIN" -S - "$profile" <<'PY' 2>/dev/null || true
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)
    [[ -n "$resolved" && -f "$resolved" ]] || return 1
  else
    resolved=$profile
  fi
  parent=$(physical_path "$(dirname "$resolved")" 2>/dev/null || true)
  [[ -n "$parent" ]] || return 1
  resolved="$parent/$(basename "$resolved")"
  path_is_within "$resolved" "$HOME" || return 1
  printf '%s' "$resolved"
}

edit_profile() {
  local profile=$1 target
  target=$(resolve_profile_target "$profile" 2>/dev/null || true)
  if [[ -z "$target" ]]; then warn "unsafe shell profile skipped: $profile"; return 0; fi
  ensure_dir "$(dirname "$target")" "$DIR_MODE"
  backup_profile_target "$target"
  "$PYTHON_BIN" -S - "$target" "$BIN_DIR" "$PATH_BLOCK_BEGIN" "$PATH_BLOCK_END" "$profile" <<'PY'
import os, pathlib, stat, sys, tempfile
path=pathlib.Path(sys.argv[1]); bindir=sys.argv[2]; begin=sys.argv[3].encode(); end=sys.argv[4].encode(); requested=sys.argv[5]
old=path.read_bytes() if path.exists() else b''
mode=stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
start=old.find(begin); finish=old.find(end)
if (start<0) != (finish<0) or (start>=0 and finish<start): raise SystemExit('malformed scan-dir PATH block')
raw=os.fsencode(bindir)
posix_quoted=b"'"+raw.replace(b"'", b"'\\''")+b"'"
fish_quoted=b"'"+raw.replace(b"\\", b"\\\\").replace(b"'", b"\\'")+b"'"
if requested.endswith('.fish'):
    block=begin+b'\nif not contains -- '+fish_quoted+b' $PATH\n    fish_add_path -- '+fish_quoted+b'\nend\n'+end
else:
    block=begin+b'\nexport PATH='+posix_quoted+b':"$PATH"\n'+end
if start>=0:
    finish += len(end); data=old[:start]+block+old[finish:]
else:
    data=old + (b'\n' if old and not old.endswith(b'\n') else b'') + b'\n' + block + b'\n'
fd,tmp=tempfile.mkstemp(prefix=path.name+'.tmp.',dir=str(path.parent))
try:
    with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,mode); os.replace(tmp,path)
finally:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
PY
}
repair_path() {
  [[ "$DRY_RUN" == 0 && "$NO_PATH_REPAIR" == 0 ]] || return 0
  path_has_dir "$BIN_DIR" && return 0
  local profile profiles=()
  while IFS= read -r profile; do
    [[ -n "$profile" ]] && profiles+=("$profile")
  done < <(profile_candidates)
  for profile in "${profiles[@]}"; do
    edit_profile "$profile"
  done
}

write_manifest() {
  local target="$APP_DIR/$MANIFEST_FILE" temp installed_at runtime_digest config_digest skill_digest primary_digest alias_digest
  make_sibling_temp "$target"
  temp=$TEMP_PATH
  installed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf unknown)
  runtime_digest=$(sha256_file "$APP_DIR/$RUNTIME_FILE") || fail 'unable to hash installed runtime'
  config_digest=$(sha256_file "$APP_DIR/$CONFIG_FILE") || fail 'unable to hash installed configuration'
  skill_digest=$(sha256_file "$APP_DIR/$SKILL_FILE") || fail 'unable to hash installed skill file'
  primary_digest=$(sha256_file "$BIN_DIR/$PRIMARY_COMMAND") || fail 'unable to hash primary command wrapper'
  alias_digest=$(sha256_file "$BIN_DIR/$ALIAS_COMMAND") || fail 'unable to hash alias command wrapper'
  cat > "$temp" <<EOF
product=$PRODUCT_SLUG
installer_version=$INSTALLER_VERSION
installed_at=$installed_at
platform=$PLATFORM
arch=$ARCH
app_dir=$APP_DIR
bin_dir=$BIN_DIR
state_dir=$STATE_DIR
config_dir=$CONFIG_DIR
runtime_sha256=$runtime_digest
config_sha256=$config_digest
skill_sha256=$skill_digest
primary_wrapper_sha256=$primary_digest
alias_wrapper_sha256=$alias_digest
EOF
  chmod "$PRIVATE_MODE" "$temp"
  mv "$temp" "$target"
}


validate_installed() {
  [[ -x "$BIN_DIR/$PRIMARY_COMMAND" && -x "$BIN_DIR/$ALIAS_COMMAND" ]] || fail 'installed command wrappers are missing'
  require_version "$("$BIN_DIR/$PRIMARY_COMMAND" version)" installed
  "$BIN_DIR/$PRIMARY_COMMAND" help >/dev/null
  "$BIN_DIR/$PRIMARY_COMMAND" status >/dev/null
  "$BIN_DIR/$ALIAS_COMMAND" version >/dev/null
  local fixture output
  fixture=$(mktemp -d "$WORK_DIR/installed-fixture.XXXXXX"); printf 'demo\n' > "$fixture/README.md"
  output=$("$BIN_DIR/$PRIMARY_COMMAND" "$fixture" --config "$APP_DIR/$CONFIG_FILE" --only -e .md --include-hidden --include-empty --scan-data 'tree,summary' --scan-styling minimal --scan-emojis false --auto-copy false)
  [[ "$output" == *README.md* && "$output" == *largest* ]] || fail 'installed runtime validation failed'
}

legacy_app_layout_is_owned() {
  [[ "$LEGACY_APP_DIR" != "$APP_DIR" ]] || return 1
  "$PYTHON_BIN" -S - "$LEGACY_APP_DIR" "$LEGACY_MANAGED_MARKER" <<'PY' >/dev/null 2>&1
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
marker = sys.argv[2]
allowed = {
    ".installer-version",
    ".managed",
    "SKILL.md",
    "config.yaml",
    "install-manifest.env",
    "prs.py",
}
info = root.lstat()
mode = stat.S_IMODE(info.st_mode)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or mode & 0o022:
    raise SystemExit(1)
entries = {entry.name for entry in root.iterdir()}
if not entries <= allowed or ".managed" not in entries:
    raise SystemExit(1)
for entry in root.iterdir():
    item = entry.lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid():
        raise SystemExit(1)
if (root / ".managed").read_text(encoding="utf-8").rstrip("\n") != marker:
    raise SystemExit(1)
PY
}

legacy_state_layout_is_owned() {
  [[ "$LEGACY_STATE_DIR" != "$STATE_DIR" ]] || return 1
  [[ ! -e "$LEGACY_STATE_DIR" && ! -L "$LEGACY_STATE_DIR" ]] && return 0
  "$PYTHON_BIN" -S - "$LEGACY_STATE_DIR" <<'PY' >/dev/null 2>&1
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
info = root.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
    raise SystemExit(1)
for entry in root.rglob("*"):
    item = entry.lstat()
    if item.st_uid != os.getuid() or stat.S_ISLNK(item.st_mode):
        raise SystemExit(1)
    relative = entry.relative_to(root)
    if stat.S_ISDIR(item.st_mode):
        if relative.parts != ("logs",):
            raise SystemExit(1)
    elif stat.S_ISREG(item.st_mode):
        if len(relative.parts) != 2 or relative.parts[0] != "logs" or not relative.name.startswith("install") or ".log" not in relative.name:
            raise SystemExit(1)
    else:
        raise SystemExit(1)
PY
}

legacy_wrapper_is_owned() {
  local target=$1 command_name=$2 dollar='$' source_line exec_line quoted_app plain_app
  [[ -f "$target" && ! -L "$target" && -O "$target" ]] || return 1
  grep -Fqx '#!/usr/bin/env bash' "$target" 2>/dev/null || return 1
  grep -Fqx "# $command_name -- Project Summarizer command" "$target" 2>/dev/null || return 1
  grep -Fqx "# $LEGACY_MANAGED_MARKER" "$target" 2>/dev/null || return 1
  quoted_app="APP_DIR=$(shell_quote "$LEGACY_APP_DIR")"
  plain_app="APP_DIR=$LEGACY_APP_DIR"
  if ! grep -Fqx "$quoted_app" "$target" 2>/dev/null && ! grep -Fqx "$plain_app" "$target" 2>/dev/null; then
    return 1
  fi
  source_line="SOURCE_FILE=\"${dollar}APP_DIR/$LEGACY_RUNTIME_FILE\""
  exec_line="exec \"${dollar}PYTHON_BIN\" -S \"${dollar}SOURCE_FILE\" \"${dollar}@\""
  grep -Fqx "$source_line" "$target" 2>/dev/null || return 1
  grep -Fqx "$exec_line" "$target" 2>/dev/null
}

migrate_legacy_user_config() {
  local old="$LEGACY_CONFIG_DIR/$CONFIG_FILE" new="$CONFIG_DIR/$CONFIG_FILE" temp
  [[ "$LEGACY_CONFIG_DIR" != "$CONFIG_DIR" ]] || return 0
  [[ -e "$old" || -L "$old" ]] || {
    if [[ -d "$LEGACY_CONFIG_DIR" && ! -L "$LEGACY_CONFIG_DIR" ]]; then rmdir "$LEGACY_CONFIG_DIR" 2>/dev/null || true; fi
    return 0
  }
  if [[ ! -f "$old" || -L "$old" || ! -O "$old" ]]; then
    warn "legacy user configuration was not migrated because it is not a safe current-user regular file: $old"
    return 0
  fi
  ensure_dir "$CONFIG_DIR" "$DIR_MODE"
  secure_owned_directory 'config directory' "$CONFIG_DIR"
  if [[ -e "$new" || -L "$new" ]]; then
    if [[ -f "$new" && ! -L "$new" ]] && cmp -s "$old" "$new"; then
      rm -f "$old" || warn "unable to remove duplicate legacy user configuration: $old"
    else
      warn "legacy user configuration differs from the existing Scan Dir configuration and was preserved: $old"
      return 0
    fi
  else
    make_sibling_temp "$new"
    temp=$TEMP_PATH
    cp "$old" "$temp"
    chmod "$PRIVATE_MODE" "$temp"
    mv "$temp" "$new"
    record_created_file "$new"
    rm -f "$old" || warn "legacy user configuration was copied but could not be removed: $old"
  fi
  rmdir "$LEGACY_CONFIG_DIR" 2>/dev/null || true
}

cleanup_legacy_install() {
  [[ "$DRY_RUN" == 0 ]] || return 0
  migrate_legacy_user_config

  local primary="$LEGACY_BIN_DIR/$LEGACY_PRIMARY_COMMAND"
  local alias="$LEGACY_BIN_DIR/$LEGACY_ALIAS_COMMAND"
  local app_present=0 unsafe=0
  [[ -e "$LEGACY_APP_DIR" || -L "$LEGACY_APP_DIR" ]] && app_present=1

  if [[ "$app_present" == 1 ]] && ! legacy_app_layout_is_owned; then
    warn "legacy runtime was preserved because ownership or layout could not be proven: $LEGACY_APP_DIR"
    return 0
  fi
  if [[ -e "$primary" || -L "$primary" ]]; then
    legacy_wrapper_is_owned "$primary" "$LEGACY_PRIMARY_COMMAND" || unsafe=1
  fi
  if [[ -e "$alias" || -L "$alias" ]]; then
    legacy_wrapper_is_owned "$alias" "$LEGACY_ALIAS_COMMAND" || unsafe=1
  fi
  if [[ "$unsafe" == 1 ]]; then
    warn "legacy commands were preserved because ownership could not be proven in $LEGACY_BIN_DIR"
    return 0
  fi

  [[ ! -e "$primary" && ! -L "$primary" ]] || rm -f "$primary" || warn "unable to remove legacy command: $primary"
  [[ ! -e "$alias" && ! -L "$alias" ]] || rm -f "$alias" || warn "unable to remove legacy command: $alias"
  if [[ "$app_present" == 1 ]]; then
    rm -rf "$LEGACY_APP_DIR" || warn "unable to remove legacy runtime: $LEGACY_APP_DIR"
  fi
  if legacy_state_layout_is_owned; then
    [[ ! -e "$LEGACY_STATE_DIR" && ! -L "$LEGACY_STATE_DIR" ]] || rm -rf "$LEGACY_STATE_DIR" || warn "unable to remove legacy state: $LEGACY_STATE_DIR"
  elif [[ -e "$LEGACY_STATE_DIR" || -L "$LEGACY_STATE_DIR" ]]; then
    warn "legacy state was preserved because ownership or layout could not be proven: $LEGACY_STATE_DIR"
  fi
}

finalize() {
  local backup
  # The installation has already passed direct command and manifest integrity
  # validation. Commit the transaction before deleting rollback copies so an
  # interrupt during housekeeping cannot partially restore an older release.
  FINALIZED=1
  for backup in "${BACKUP_FILES[@]:-}"; do [[ -n "$backup" ]] && rm -rf "$backup" 2>/dev/null || true; done
  BACKUP_TARGETS=(); BACKUP_FILES=(); CREATED_DIRS=(); CREATED_FILES=()
}

print_summary() {
  [[ "$QUIET" == 1 ]] && return 0
  printf '\n%s✓%s installed %s\n' "$GREEN" "$RESET" "$PRIMARY_COMMAND $INSTALLER_VERSION"
  printf '  command: %s\n  alias:   %s\n  runtime: %s\n' "$BIN_DIR/$PRIMARY_COMMAND" "$BIN_DIR/$ALIAS_COMMAND" "$APP_DIR"
  if ((${#WARNINGS[@]})); then printf '  warnings: %s\n' "${#WARNINGS[@]}"; fi
}

main() {
  parse_args "$@"
  setup_colors
  case "$(uname -s 2>/dev/null || printf unknown)" in Darwin*) PLATFORM=darwin ;; Linux*) PLATFORM=linux ;; *) PLATFORM=unix ;; esac
  ARCH=$(uname -m 2>/dev/null || printf unknown)
  local invoked_source=${BASH_SOURCE[0]:-}
  if [[ -n "$invoked_source" && -f "$invoked_source" && ! -L "$invoked_source" ]]; then
    SCRIPT_PATH=$(physical_path "$invoked_source") || fail 'unable to resolve installer location'
    SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
  else
    SCRIPT_PATH=''
    SCRIPT_DIR=''
  fi
  setup_paths
  render_banner
  step 'resolve Python runtime' ensure_python
  setup_logging
  if [[ "$DRY_RUN" == 0 ]]; then
    validate_existing_managed_directories
    acquire_lock
  fi
  read_install_metadata
  ensure_app_replaceable
  if [[ "$INSTALLED_VERSION_UNKNOWN" == 1 && "$FORCE" == 0 ]]; then
    fail 'managed installation version cannot be verified; rerun with --force to repair it'
  fi
  if [[ -n "$INSTALLED_VERSION" ]]; then
    local comparison=''
    if ! comparison=$(version_compare "$INSTALLED_VERSION" "$INSTALLER_VERSION"); then
      if [[ "$FORCE" == 0 ]]; then fail 'installed version metadata is invalid; rerun with --force to repair it'; fi
      warn 'installed version metadata is invalid and will be replaced because --force was supplied'
    elif [[ "$comparison" == 1 && "$FORCE" == 0 ]]; then
      fail "refusing downgrade from $INSTALLED_VERSION to $INSTALLER_VERSION without --force"
    fi
  fi
  create_workspace

  if [[ "$FORCE" == 0 && "$DRY_RUN" == 0 ]] && installed_is_intact; then
    step 'repair active PATH integration' create_active_bridge
    step 'repair shell PATH' repair_path
    step 'validate installed commands' validate_installed
    step 'migrate legacy installation' cleanup_legacy_install
    finalize
    say "Already installed ($INSTALLER_VERSION); integrity verified."
    return 0
  fi

  step 'resolve package' resolve_package
  step 'validate package' validate_package
  step 'stage managed runtime' stage_package
  if [[ "$DRY_RUN" == 1 ]]; then
    say "Dry-run succeeded: package and runtime contract validated."
    return 0
  fi
  step 'commit managed runtime' commit_package
  step 'create safe active PATH bridge' create_active_bridge
  step 'repair shell PATH' repair_path
  step 'validate installed commands' validate_installed
  step 'write integrity manifest' write_manifest
  read_install_metadata
  installed_is_intact || fail 'post-install integrity verification failed'
  step 'migrate legacy installation' cleanup_legacy_install
  finalize
  print_summary
}

main "$@"

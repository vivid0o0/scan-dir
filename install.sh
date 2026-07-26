#!/usr/bin/env bash
# install.sh -- Project Summarizer setup
# Installs, repairs, validates, and exposes the `prs` and `project-summarizer`
# commands with managed runtime state, atomic rollback, a single in-place
# progress bar, and shell-aware PATH integration. Designed to run unattended
# via `curl | bash` on any Linux or macOS distribution with Bash 3.2+.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

# Save original stdout (fd 3) and stderr (fd 4) so the EXIT trap can restore
# them. This is critical: when fail() calls exit 1 inside run_step's redirected
# command (>>"$LOG_FILE" 2>&1), the redirections persist during the EXIT trap.
# Without restoring the original fds, cleanup()'s output goes to the log file
# instead of the terminal.
exec 3>&1 4>&2

# ─── PRODUCT CONTRACT ───────────────────────────────────────────────────────
# Immutable identity constants. Everything else derives from these.

readonly PRODUCT_TITLE='Project Summarizer'
readonly PRODUCT_SLUG='project-summarizer'
readonly PRIMARY_COMMAND='prs'
readonly ALIAS_COMMAND='project-summarizer'
readonly RUNTIME_SOURCE_NAME='prs.py'
readonly SKILL_FILE_NAME='SKILL.md'
readonly CONFIG_FILE_NAME='config.yaml'
readonly DEFAULT_SOURCE_URL='https://raw.githubusercontent.com/vivid0o0/project-summarizer/main/prs.py'
readonly INSTALLER_VERSION='2026.06.20.31'
readonly MINIMUM_PYTHON_VERSION='3.10'
readonly MANAGED_MARKER='project-summarizer managed command'
readonly BRIDGE_MARKER='project-summarizer managed active PATH bridge'
readonly PATH_BLOCK_BEGIN='# >>> project-summarizer PATH >>>'
readonly PATH_BLOCK_END='# <<< project-summarizer PATH <<<'
readonly DEFAULT_SCAN_TIMEOUT='60'
readonly INSTALL_LOCK_NAME='install.lock'
readonly MANIFEST_NAME='install-manifest.env'
readonly LOG_NAME='install.log'
readonly DATE_FORMAT='+%Y-%m-%dT%H:%M:%SZ'

# ─── OPERATIONAL CONSTANTS ──────────────────────────────────────────────────
# Policy values for network I/O, filesystem permissions, and rendering layout.
# Centralized here so behavior is tunable from one location and inline magic
# numbers do not drift across functions. Identity constants live in PRODUCT
# CONTRACT above; this section holds the tunable operational policy.

# ─── Network policy
# Timeouts (seconds) and retry count for every download path. curl, wget, and
# the Python urllib fallback all read these so the three paths behave
# identically. CONNECT caps the TCP handshake so a silent server fails fast;
# MAXIMUM caps the entire transfer so a stalled server cannot hang the
# install. RETRY_COUNT and RETRY_DELAY_SECONDS control curl's retry-on-failure
# behavior (wget honors RETRY_COUNT via --tries; the Python fallback does not
# retry because it is only reached when both curl and wget are absent).
readonly NETWORK_CONNECT_TIMEOUT_SECONDS=15
readonly NETWORK_MAXIMUM_TIMEOUT_SECONDS=300
readonly NETWORK_RETRY_COUNT=3
readonly NETWORK_RETRY_DELAY_SECONDS=1
# User-Agent for the Python urllib fallback. curl and wget send their own
# default UA; the Python fallback sends this string so the CDN can identify
# installer traffic. Derived from PRODUCT_SLUG so it tracks the product name.
readonly NETWORK_USER_AGENT="$PRODUCT_SLUG-installer"

# ─── File permissions
# Permission modes for every file and directory the installer creates.
# DIRECTORY_PRIVATE protects state/config/cache/log directories (may contain
# user paths and install metadata). FILE_PRIVATE protects the install log
# and install manifest. EXECUTABLE is for command wrappers and active PATH
# bridges. DATA_FILE is for the read-only SKILL.md documentation. All modes
# are given as decimal integers; chmod and install both accept this form.
readonly PERMISSIONS_DIRECTORY_PRIVATE=700
readonly PERMISSIONS_FILE_PRIVATE=600
readonly PERMISSIONS_EXECUTABLE=755
readonly PERMISSIONS_DATA_FILE=644

# ─── Terminal and rendering layout
# Width policies shared across terminal_columns() and every renderer.
# MINIMUM is the floor below which we refuse to render narrower. FALLBACK is
# used when terminal detection fails entirely. RENDER_MAXIMUM caps how many
# columns any single renderer may consume, even on very wide terminals, so
# banners and progress bars stay readable instead of stretching edge-to-edge.
readonly TERMINAL_WIDTH_MINIMUM=60
readonly TERMINAL_WIDTH_FALLBACK=100
readonly RENDER_WIDTH_MAXIMUM=100
# Progress animation frame interval (seconds). About 12 fps — fast enough for
# visible motion, slow enough to avoid burning CPU on a long step. Stored as
# a string because bash arithmetic cannot hold fractional values.
# Progress bar fill bounds, in columns. Below MINIMUM the bar is unreadable;
# above MAXIMUM it dominates the line and crowds out the step label.
readonly PROGRESS_BAR_WIDTH_MINIMUM=12
readonly PROGRESS_BAR_WIDTH_MAXIMUM=64
# Cap (percent) while a step is running. The bar never reaches 100% until the
# whole install is done, so a slow step never looks complete.
readonly PROGRESS_PERCENT_RUNNING_CAP=95
# Box layout shared by the banner, the success summary, and the error box.
# BOX_PADDING is the columns reserved between the art/label and the box
# border. BOX_BORDER is the border thickness on each side (one column of
# the vertical rule character). SLOT_OFFSET is the columns reserved for
# borders plus inner padding inside a value cell. SLOT_MINIMUM is the
# smallest cell that still fits a label + value pair legibly.
readonly BANNER_BOX_PADDING=6
readonly BANNER_BOX_WIDTH_MINIMUM=34
readonly RENDER_BOX_BORDER=2
readonly RENDER_SLOT_OFFSET=6
readonly RENDER_SLOT_MINIMUM=20
# The error box uses a smaller slot minimum than the success summary because
# its cells only need to fit "step: <name>" and "error: <message>". The
# prefix width reserves space for the "error: " label when truncating.
readonly ERROR_SLOT_MINIMUM=10
readonly ERROR_PREFIX_WIDTH=7

# ─── USER OPTIONS ───────────────────────────────────────────────────────────
# Populated by parse_args(). All defaults are safe for an unattended run.

SOURCE_PATH=''
SOURCE_URL=''
SOURCE_SHA256=''
APP_DIR_OVERRIDE=''
BIN_DIR_OVERRIDE=''
STATE_DIR_OVERRIDE=''
CONFIG_DIR_OVERRIDE=''
CACHE_DIR_OVERRIDE=''
TMP_ROOT_OVERRIDE=''
LOGO_MODE='auto'
COLOR_MODE='auto'
QUIET=0
NO_PATH_REPAIR=0
NO_ACTIVE_BRIDGE=0
NO_REPLACE=0
DRY_RUN=0
FORCE=0

# ─── RUNTIME STATE ──────────────────────────────────────────────────────────
# Mutable globals, populated as the install progresses. Grouped by lifecycle.

OS_NAME=''
PLATFORM=''
ARCH=''
SCRIPT_DIR=''
ORIGINAL_PATH="${PATH:-}"
APP_DIR=''
BIN_DIR=''
STATE_DIR=''
CONFIG_DIR=''
CACHE_DIR=''
TMP_ROOT=''
TMP_DIR=''
LOG_DIR=''
LOG_FILE=''
LOCK_DIR=''
LOCK_TOKEN=''
PYTHON_BIN=''
RESOLVED_SOURCE=''
RESOLVED_SKILL=''
RESOLVED_CONFIG=''
STAGED_APP_DIR=''
STAGED_BIN_DIR=''
CURRENT_STEP='startup'
TOTAL_STEPS=0
STEP_INDEX=0
STEP_START_TIME=0
INSTALL_COMMITTED=0
ACTIVE_PATH_BRIDGE_DIR=''
ALIAS_PATH_BRIDGE_DIR=''
BACKUP_APP_DIR=''
BACKUP_PRIMARY_COMMAND=''
BACKUP_ALIAS_COMMAND=''
BACKUP_ACTIVE_PRIMARY=''
BACKUP_ACTIVE_ALIAS=''
PROFILE_BACKUP_TARGETS=()
PROFILE_BACKUP_FILES=()
CLEANUP_PATHS=()

# ─── COLOR PALETTE ──────────────────────────────────────────────────────────
# Clean, high-contrast palette. Standard ANSI colors (not bright) avoid the
# neon look. Each semantic role maps to exactly one color, applied uniformly
# across every surface (banner, progress, summary, errors).

RESET=''
BOLD=''
DIM=''
COLOR_SUCCESS=''   # green  — completed steps, success banner, summary box
COLOR_ERROR=''     # red    — failed steps, error box
COLOR_INFO=''      # cyan   — banner, section titles, neutral info
COLOR_ACCENT=''    # blue   — running step, progress bar fill
COLOR_WARN=''      # yellow — warnings

setup_colors() {
  # --color always wins over everything (including NO_COLOR) so the user can
  # force color for piping or logging. --color never disables unconditionally.
  # Otherwise, respect NO_COLOR and require a TTY (or FORCE_COLOR=1).
  if [[ "$COLOR_MODE" == 'always' ]]; then
    :
  elif [[ "$COLOR_MODE" == 'never' ]]; then
    return 0
  elif [[ -n "${NO_COLOR:-}" ]]; then
    return 0
  elif ! is_tty && [[ "${FORCE_COLOR:-0}" != '1' ]]; then
    return 0
  fi
  RESET=$'\033[0m'
  BOLD=$'\033[1m'
  DIM=$'\033[2m'
  COLOR_SUCCESS=$'\033[32m'
  COLOR_ERROR=$'\033[31m'
  COLOR_INFO=$'\033[36m'
  COLOR_ACCENT=$'\033[34m'
  COLOR_WARN=$'\033[33m'
}

# ─── TERMINAL CAPABILITIES ──────────────────────────────────────────────────

has() {
  command -v "$1" >/dev/null 2>&1
}

is_tty() {
  [[ -t 1 ]]
}

stderr_is_tty() {
  [[ -t 2 ]]
}

unicode_ok() {
  local locale_value="${LC_ALL:-${LC_CTYPE:-${LANG:-}}}"
  case "$locale_value" in
    *UTF-8*|*utf8*|*UTF8*) return 0 ;;
    *) return 1 ;;
  esac
}

terminal_columns() {
  local detected=''
  if is_tty && has tput; then
    detected="$(tput cols 2>/dev/null || true)"
  fi
  if ! [[ "$detected" =~ ^[0-9]+$ && "$detected" -gt 0 ]]; then
    detected="${COLUMNS:-}"
  fi
  [[ "$detected" =~ ^[0-9]+$ && "$detected" -gt 0 ]] || detected="$TERMINAL_WIDTH_FALLBACK"
  (( detected < TERMINAL_WIDTH_MINIMUM )) && detected="$TERMINAL_WIDTH_MINIMUM"
  printf '%s' "$detected"
}

# ─── TEXT UTILITIES ─────────────────────────────────────────────────────────

repeat_char() {
  local char="$1"
  local count="$2"
  local pad=''
  (( count > 0 )) || return 0
  printf -v pad '%*s' "$count" ''
  printf '%s' "${pad// /$char}"
}

truncate_text() {
  local text="$1"
  local width="$2"
  local suffix='…'
  unicode_ok || suffix='...'
  (( width > 0 )) || return 0
  if (( ${#text} <= width )); then
    printf '%s' "$text"
    return 0
  fi
  if (( width <= ${#suffix} )); then
    printf '%s' "${text:0:width}"
  else
    printf '%s%s' "${text:0:$(( width - ${#suffix} ))}" "$suffix"
  fi
}

center_text() {
  local width="$1"
  local text="$2"
  local length left right
  text="$(truncate_text "$text" "$width")"
  length=${#text}
  left=$(( (width - length) / 2 ))
  right=$(( width - length - left ))
  printf '%*s%s%*s' "$left" '' "$text" "$right" ''
}

shell_quote() {
  printf '%q' "$1"
}

single_quote() {
  local value="$1"
  printf "'"
  local i ch
  for (( i = 0; i < ${#value}; i++ )); do
    ch="${value:i:1}"
    if [[ "$ch" == "'" ]]; then
      printf %s "'\\''"
    else
      printf '%s' "$ch"
    fi
  done
  printf "'"
}

fish_quote() {
  local value="$1"
  local i ch
  printf "'"
  for (( i = 0; i < ${#value}; i++ )); do
    ch="${value:i:1}"
    case "$ch" in
      "'") printf %s "\\'" ;;
      "\\") printf %s "\\\\" ;;
      *) printf '%s' "$ch" ;;
    esac
  done
  printf "'"
}

utc_now() {
  date -u "$DATE_FORMAT"
}

log_raw() {
  [[ -n "${LOG_FILE:-}" ]] || return 0
  printf '%s %s\n' "$(utc_now)" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

append_cleanup_path() {
  CLEANUP_PATHS+=("$1")
}

# ─── PROGRESS RENDERER ──────────────────────────────────────────────────────
# A SINGLE progress bar for the entire install. The bar lives on one line and
# goes from 0% to 100%. As each step runs, the bar fills proportionally and the
# label on the right updates to show the current step. The bar is never
# duplicated — there is exactly one bar line in the final output.
#
# Layout (TTY, single line, updated in place):
#   <percent>% <bar>  <current step label>
# The spinner glyph sits in front of the percent while a step is running; it
# becomes a checkmark when the whole install succeeds, or a cross on failure.
#
# Layout (non-TTY): the bar is printed once at its final state (100% or the
# failure percent) with the last step label, so non-TTY output has exactly one
# bar line.

PROGRESS_SPINNER_FRAMES=( '⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏' )
# shellcheck disable=SC1003  # the trailing '\\' is a literal backslash frame, not an escape
PROGRESS_SPINNER_FRAMES_ASCII=( '|' '/' '-' '\\' )
PROGRESS_ANIMATION_PID=''
PROGRESS_FINAL_PERCENT=0
PROGRESS_FINAL_LABEL=''
PROGRESS_RENDERED=0
PROGRESS_ERROR_PRINTED=0
EXIT_MESSAGE=''

progress_target_percent() {
  # The target percent for the current step (the percent the bar should show
  # when this step completes). Capped at PROGRESS_PERCENT_RUNNING_CAP while
  # running so the bar never reaches 100% until the install is fully done.
  if (( TOTAL_STEPS <= 0 )); then
    printf '0'
    return 0
  fi
  local target=$(( STEP_INDEX * 100 / TOTAL_STEPS ))
  (( target > 100 )) && target=100
  printf '%s' "$target"
}

progress_running_percent() {
  # Interpolate the bar from the previous step's percent toward the current
  # step's target percent based on elapsed time, capped at
  # PROGRESS_PERCENT_RUNNING_CAP so a slow step never looks complete before
  # it finishes.
  local prev_percent next_percent elapsed
  if (( TOTAL_STEPS <= 0 )); then
    printf '0'
    return 0
  fi
  prev_percent=$(( (STEP_INDEX - 1) * 100 / TOTAL_STEPS ))
  (( prev_percent < 0 )) && prev_percent=0
  next_percent=$(( STEP_INDEX * 100 / TOTAL_STEPS ))
  (( next_percent > 100 )) && next_percent=100
  (( next_percent > PROGRESS_PERCENT_RUNNING_CAP )) && next_percent="$PROGRESS_PERCENT_RUNNING_CAP"
  elapsed=$(( $(date +%s) - STEP_START_TIME ))
  local advance=$(( elapsed * 50 / 2 ))
  (( advance > 100 )) && advance=100
  local target=$(( prev_percent + (next_percent - prev_percent) * advance / 100 ))
  (( target > next_percent )) && target=next_percent
  (( target > PROGRESS_PERCENT_RUNNING_CAP )) && target="$PROGRESS_PERCENT_RUNNING_CAP"
  (( target < prev_percent )) && target=prev_percent
  printf '%s' "$target"
}

progress_bar() {
  # Render the bar fill+empty string for the given percent and width.
  local percent="$1"
  local width="$2"
  local fill empty fill_char empty_char
  (( width < PROGRESS_BAR_WIDTH_MINIMUM )) && width="$PROGRESS_BAR_WIDTH_MINIMUM"
  (( width > PROGRESS_BAR_WIDTH_MAXIMUM )) && width="$PROGRESS_BAR_WIDTH_MAXIMUM"
  if unicode_ok; then
    fill_char='━'
    empty_char='·'
  else
    fill_char='='
    empty_char='.'
  fi
  fill=$(( (width * percent + 50) / 100 ))
  (( fill > width )) && fill=$width
  (( fill < 0 )) && fill=0
  empty=$(( width - fill ))
  printf '%s' "$(repeat_char "$fill_char" "$fill")"
  printf '%s' "$(repeat_char "$empty_char" "$empty")"
}

progress_spinner() {
  local frame="$1"
  local frames
  if unicode_ok; then
    frames=("${PROGRESS_SPINNER_FRAMES[@]}")
  else
    frames=("${PROGRESS_SPINNER_FRAMES_ASCII[@]}")
  fi
  printf '%s' "${frames[$(( frame % ${#frames[@]} ))]}"
}

render_progress_line() {
  local state="$1"
  local label="$2"
  local frame="${3:-0}"
  local terminal_width width left_pad pad label_width bar_width
  terminal_width="$(terminal_columns)"
  width="$terminal_width"
  (( width > RENDER_WIDTH_MAXIMUM )) && width="$RENDER_WIDTH_MAXIMUM"
  (( width < 54 )) && width="$terminal_width"
  left_pad=$(( (terminal_width - width) / 2 ))
  (( left_pad < 0 )) && left_pad=0
  printf -v pad '%*s' "$left_pad" ''
  label_width=$(( width * 2 / 5 ))
  (( label_width < 20 )) && label_width=20
  (( label_width > 46 )) && label_width=46
  bar_width=$(( width - label_width - 14 ))
  (( bar_width < PROGRESS_BAR_WIDTH_MINIMUM )) && bar_width="$PROGRESS_BAR_WIDTH_MINIMUM"
  local glyph color percent_value bar
  case "$state" in
    done)
      glyph='✓'
      color="$COLOR_SUCCESS"
      percent_value=100
      bar="$(progress_bar 100 "$bar_width")"
      ;;
    fail)
      glyph='✗'
      color="$COLOR_ERROR"
      percent_value="$PROGRESS_FINAL_PERCENT"
      bar="$(progress_bar "$percent_value" "$bar_width")"
      ;;
    *)
      glyph="$(progress_spinner "$frame")"
      color="$COLOR_ACCENT"
      percent_value="$(progress_running_percent)"
      bar="$(progress_bar "$percent_value" "$bar_width")"
      ;;
  esac
  unicode_ok || glyph='*'
  local label_text
  label_text="$(truncate_text "$label" "$label_width")"
  printf '%s%s%s%s %3d%% %s%s%s  %s%-*s%s' \
    "$pad" \
    "$color" "$glyph" "$RESET" \
    "$percent_value" \
    "$color" "$bar" "$RESET" \
    "$DIM" "$label_width" "$label_text" "$RESET"
}

progress_clear_line() {
  printf '\r\033[2K'
}

progress_hide_cursor() {
  if is_tty && has tput; then
    tput civis 2>/dev/null || true
  fi
}

progress_show_cursor() {
  if is_tty && has tput; then
    tput cnorm 2>/dev/null || true
  fi
}

progress_start_step() {
  # Begin a step: increment the index, record the start time, and render the
  # current progress line once. The installer does not use fixed-delay
  # animation loops; each state change advances immediately.
  STEP_INDEX=$(( STEP_INDEX + 1 ))
  STEP_START_TIME=$(date +%s)
  PROGRESS_FINAL_LABEL="$1"
  if [[ "$QUIET" == 1 ]]; then
    return 0
  fi
  if ! is_tty; then
    return 0
  fi
  progress_hide_cursor
  progress_clear_line
  render_progress_line 'run' "$1" 0
}

progress_finish_step() {
  # End a step. On TTY the animation continues (the bar keeps filling toward
  # the next step's target). On non-TTY we record the final percent/label so
  # progress_finalize can print the single bar line at the end.
  PROGRESS_FINAL_PERCENT="$(progress_target_percent)"
  if [[ "$QUIET" == 1 ]]; then
    return 0
  fi
  if ! is_tty; then
    return 0
  fi
  # Redraw once with the step's final percent (capped at 95 unless this is the
  # last step), then let the next progress_start_step restart the animation.
  if [[ -n "${PROGRESS_ANIMATION_PID:-}" ]]; then
    kill "$PROGRESS_ANIMATION_PID" >/dev/null 2>&1 || true
    wait "$PROGRESS_ANIMATION_PID" 2>/dev/null || true
    PROGRESS_ANIMATION_PID=''
  fi
  progress_clear_line
  # While running (not the last step), show the spinner at the step's target.
  if (( STEP_INDEX < TOTAL_STEPS )); then
    render_progress_line 'run' "$PROGRESS_FINAL_LABEL" 0
  else
    # Last step: we'll print the final done line in progress_finalize.
    :
  fi
}

progress_finalize() {
  # Print the final state of the single bar. Called once at the very end of
  # the install (success or failure). On TTY this clears the animation and
  # prints the final line; on non-TTY this is the only bar line printed.
  local state="$1"
  if [[ "$QUIET" == 1 ]]; then
    return 0
  fi
  if [[ -n "${PROGRESS_ANIMATION_PID:-}" ]]; then
    kill "$PROGRESS_ANIMATION_PID" >/dev/null 2>&1 || true
    wait "$PROGRESS_ANIMATION_PID" 2>/dev/null || true
    PROGRESS_ANIMATION_PID=''
  fi
  if is_tty; then
    progress_clear_line
    progress_show_cursor
  fi
  if [[ "$PROGRESS_RENDERED" == 0 ]] || is_tty; then
    render_progress_line "$state" "$PROGRESS_FINAL_LABEL" 0
    printf '\n'
    PROGRESS_RENDERED=1
  fi
}

run_step() {
  # Run a single install step under the progress renderer.
  # Key: step output goes to the log file ONLY (both TTY and non-TTY) so the
  # progress animation owns stdout exclusively (prevents bar stacking and
  # duplicate output). On failure, fail() calls exit 1, which triggers the
  # EXIT trap (cleanup), which calls progress_finalize 'fail' to print the
  # final bar and the log tail.
  local label="$1"
  shift
  CURRENT_STEP="$label"
  PROGRESS_FINAL_LABEL="$label"
  progress_start_step "$label"
  log_raw "run: $label"
  "$@" >>"$LOG_FILE" 2>&1
  progress_finish_step
  log_raw "done: $label"
}

# ─── BANNER AND SUMMARY ─────────────────────────────────────────────────────

logo_text_art() {
  printf 'Project Summarizer
PRS
'
}

logo_small_art() {
  cat <<'LOGO_SMALL_ART'
█▀█ █▀█ █▀
█▀▀ █▀▄ ▄█
LOGO_SMALL_ART
}

logo_medium_art() {
  cat <<'LOGO_MEDIUM_ART'
 ██████╗ ██████╗ ███████╗
 ██╔══██╗██╔══██╗██╔════╝
 ██████╔╝██████╔╝███████╗
 ██╔═══╝ ██╔══██╗╚════██║
 ██║     ██║  ██║███████║
 ╚═╝     ╚═╝  ╚═╝╚══════╝
LOGO_MEDIUM_ART
}

logo_large_art() {
  cat <<'LOGO_LARGE_ART'
 ██████╗ ██████╗  ██████╗      ██╗███████╗ ██████╗████████╗                       
 ██╔══██╗██╔══██╗██╔═══██╗     ██║██╔════╝██╔════╝╚══██╔══╝                       
 ██████╔╝██████╔╝██║   ██║     ██║█████╗  ██║        ██║                           
 ██╔═══╝ ██╔══██╗██║   ██║██   ██║██╔══╝  ██║        ██║                           
 ██║     ██║  ██║╚██████╔╝╚█████╔╝███████╗╚██████╗   ██║                           
 ╚═╝     ╚═╝  ╚═╝ ╚═════╝  ╚════╝ ╚══════╝ ╚═════╝   ╚═╝                           
 
 ███████╗██╗   ██╗███╗   ███╗███╗   ███╗ █████╗ ██████╗ ██╗███████╗███████╗██████╗ 
 ██╔════╝██║   ██║████╗ ████║████╗ ████║██╔══██╗██╔══██╗██║╚══███╔╝██╔════╝██╔══██╗
 ███████╗██║   ██║██╔████╔██║██╔████╔██║███████║██████╔╝██║  ███╔╝ █████╗  ██████╔╝
 ╚════██║██║   ██║██║╚██╔╝██║██║╚██╔╝██║██╔══██║██╔══██╗██║ ███╔╝  ██╔══╝  ██╔══██╗
 ███████║╚██████╔╝██║ ╚═╝ ██║██║ ╚═╝ ██║██║  ██║██║  ██║██║███████╗███████╗██║  ██║
 ╚══════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚══════╝╚══════╝╚═╝  ╚═╝
LOGO_LARGE_ART
}

line_display_width() {
  local text="$1"
  text="${text%"${text##*[![:space:]]}"}"
  printf '%s' "${#text}"
}

logo_max_width() {
  local art="$1" line width max_width=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    width="$(line_display_width "$line")"
    (( width > max_width )) && max_width="$width"
  done <<< "$art"
  printf '%s' "$max_width"
}

selected_logo() {
  local width="$1" usable
  usable=$(( width - BANNER_BOX_PADDING ))
  case "$LOGO_MODE" in
    text) logo_text_art; return 0 ;;
    small) logo_small_art; return 0 ;;
    medium) logo_medium_art; return 0 ;;
    large) logo_large_art; return 0 ;;
  esac
  if unicode_ok && (( usable >= 86 )); then
    logo_large_art
  elif unicode_ok && (( usable >= 30 )); then
    logo_medium_art
  elif unicode_ok && (( usable >= 11 )); then
    logo_small_art
  else
    logo_text_art
  fi
}

banner_subtitle() {
  printf 'install  •  repair  •  validate'
}

colorize_logo_line() {
  local line="$1"
  if [[ -z "$COLOR_INFO" ]]; then
    printf '%s' "$line"
    return 0
  fi
  printf '%s%s%s' "${BOLD}${COLOR_INFO}" "$line" "$RESET"
}

render_centered_box_line() {
  local pad="$1" inner="$2" text="$3" color="$4" border_color="$5" trimmed shown left right
  trimmed="${text%"${text##*[![:space:]]}"}"
  local text_width=${#trimmed}
  (( text_width > inner )) && trimmed="$(truncate_text "$trimmed" "$inner")" && text_width=${#trimmed}
  left=$(( (inner - text_width) / 2 ))
  right=$(( inner - text_width - left ))
  printf '%s%s║%s%*s%s%s%s%*s%s║%s
' "$pad" "$border_color" "$RESET" "$left" '' "$color" "$trimmed" "$RESET" "$right" '' "$border_color" "$RESET"
}

render_banner() {
  [[ "$QUIET" == 1 ]] && return 0
  local terminal_width art art_width box_width inner left_pad pad rule line subtitle
  terminal_width="$(terminal_columns)"
  art="$(selected_logo "$terminal_width")"
  art_width="$(logo_max_width "$art")"
  box_width=$(( art_width + BANNER_BOX_PADDING ))
  (( box_width < BANNER_BOX_WIDTH_MINIMUM )) && box_width="$BANNER_BOX_WIDTH_MINIMUM"
  (( box_width > terminal_width )) && box_width="$terminal_width"
  inner=$(( box_width - RENDER_BOX_BORDER ))
  left_pad=$(( (terminal_width - box_width) / 2 ))
  (( left_pad < 0 )) && left_pad=0
  printf -v pad '%*s' "$left_pad" ''
  printf '
'
  if unicode_ok; then
    rule="$(repeat_char '═' "$inner")"
    printf '%s%s╔%s╗%s
' "$pad" "${BOLD}${COLOR_INFO}" "$rule" "$RESET"
    while IFS= read -r line || [[ -n "$line" ]]; do
      render_centered_box_line "$pad" "$inner" "$line" "${BOLD}${COLOR_INFO}" "$COLOR_INFO"
    done <<< "$art"
    printf '%s%s╟%s╢%s
' "$pad" "$COLOR_INFO" "$(repeat_char '─' "$inner")" "$RESET"
    subtitle="$(banner_subtitle)"
    render_centered_box_line "$pad" "$inner" 'PRS' "${BOLD}${COLOR_SUCCESS}" "$COLOR_INFO"
    render_centered_box_line "$pad" "$inner" "$subtitle" "$COLOR_WARN" "$COLOR_INFO"
    printf '%s%s╚%s╝%s

' "$pad" "${BOLD}${COLOR_INFO}" "$rule" "$RESET"
  else
    rule="$(repeat_char '-' "$inner")"
    printf '%s%s+%s+%s
' "$pad" "${BOLD}${COLOR_INFO}" "$rule" "$RESET"
    while IFS= read -r line || [[ -n "$line" ]]; do
      local trimmed="${line%"${line##*[![:space:]]}"}"
      printf '%s%s|%s|%s
' "$pad" "$COLOR_INFO" "$(center_text "$inner" "$trimmed")" "$RESET"
    done <<< "$art"
    printf '%s%s+%s+%s
' "$pad" "$COLOR_INFO" "$rule" "$RESET"
    printf '%s%s

' "$pad" "$(center_text "$box_width" 'PRS  install - repair - validate')"
  fi
}


render_summary() {
  # Final success box. Suppressed under --quiet.
  [[ "$QUIET" == 1 ]] && return 0
  local width inner rule slot skill_path
  width="$(terminal_columns)"
  inner=$(( width - RENDER_BOX_BORDER ))
  slot=$(( width - RENDER_SLOT_OFFSET ))
  (( slot < RENDER_SLOT_MINIMUM )) && slot="$RENDER_SLOT_MINIMUM"
  if [[ -f "$APP_DIR/$SKILL_FILE_NAME" ]]; then
    skill_path="$APP_DIR/$SKILL_FILE_NAME"
  else
    skill_path='(unavailable)'
  fi
  printf '\n'
  if unicode_ok; then
    rule="$(repeat_char '═' "$inner")"
    printf '%s╔%s╗%s\n' "$COLOR_SUCCESS" "$rule" "$RESET"
    printf '%s║%s%s%s║%s\n' "$COLOR_SUCCESS" "$RESET" "$(center_text "$inner" 'installed')" "$COLOR_SUCCESS" "$RESET"
    printf '%s╟%s╢%s\n' "$COLOR_SUCCESS" "$(repeat_char '─' "$inner")" "$RESET"
    summary_value_line "$width" 'command' "$BIN_DIR/$PRIMARY_COMMAND"
    summary_value_line "$width" 'alias  ' "$BIN_DIR/$ALIAS_COMMAND"
    summary_value_line "$width" 'runtime' "$APP_DIR"
    summary_value_line "$width" 'skill  ' "$skill_path"
    summary_value_line "$width" 'log    ' "$LOG_FILE"
    summary_value_line "$width" 'usage  ' 'prs [path] [options]'
    printf '%s╚%s╝%s\n\n' "$COLOR_SUCCESS" "$rule" "$RESET"
  else
    printf 'installed\n'
    printf 'command  %s\n' "$BIN_DIR/$PRIMARY_COMMAND"
    printf 'alias    %s\n' "$BIN_DIR/$ALIAS_COMMAND"
    printf 'runtime  %s\n' "$APP_DIR"
    printf 'skill    %s\n' "$skill_path"
    printf 'log      %s\n' "$LOG_FILE"
    printf 'usage    prs [path] [options]\n'
  fi
}

summary_value_line() {
  local width="$1"
  local label="$2"
  local value="$3"
  local slot=$(( width - RENDER_SLOT_OFFSET ))
  (( slot < RENDER_SLOT_MINIMUM )) && slot="$RENDER_SLOT_MINIMUM"
  local content
  content="$(truncate_text "$label  $value" "$slot")"
  if unicode_ok; then
    printf '%s║%s  %-*s  %s║%s\n' "$COLOR_SUCCESS" "$RESET" "$slot" "$content" "$COLOR_SUCCESS" "$RESET"
  else
    printf '|  %-*s  |\n' "$slot" "$content"
  fi
}

# ─── ERROR HANDLING AND ROLLBACK ────────────────────────────────────────────

print_error_box() {
  local message="$1"
  local width inner slot rule
  width="$(terminal_columns)"
  inner=$(( width - RENDER_BOX_BORDER ))
  slot=$(( width - RENDER_SLOT_OFFSET ))
  (( slot < ERROR_SLOT_MINIMUM )) && slot="$ERROR_SLOT_MINIMUM"
  if unicode_ok; then
    rule="$(repeat_char '═' "$inner")"
    printf '\n%s╔%s╗%s\n' "$COLOR_ERROR" "$rule" "$RESET" >&2
    printf '%s║%s%s%s║%s\n' "$COLOR_ERROR" "$RESET" "$(center_text "$inner" 'INSTALL FAILED')" "$COLOR_ERROR" "$RESET" >&2
    printf '%s╟%s╢%s\n' "$COLOR_ERROR" "$(repeat_char '─' "$inner")" "$RESET" >&2
    printf '%s║%s  %-*s  %s║%s\n' "$COLOR_ERROR" "$RESET" "$slot" "step: $CURRENT_STEP" "$COLOR_ERROR" "$RESET" >&2
    printf '%s║%s  %-*s  %s║%s\n' "$COLOR_ERROR" "$RESET" "$slot" "error: $(truncate_text "$message" "$(( slot - ERROR_PREFIX_WIDTH ))")" "$COLOR_ERROR" "$RESET" >&2
    [[ -n "${LOG_FILE:-}" ]] && printf '%s║%s  %-*s  %s║%s\n' "$COLOR_ERROR" "$RESET" "$slot" "log: $LOG_FILE" "$COLOR_ERROR" "$RESET" >&2
    printf '%s╚%s╝%s\n\n' "$COLOR_ERROR" "$rule" "$RESET" >&2
  else
    printf '\nINSTALL FAILED\n' >&2
    printf 'step: %s\n' "$CURRENT_STEP" >&2
    printf 'error: %s\n' "$message" >&2
    [[ -n "${LOG_FILE:-}" ]] && printf 'log: %s\n' "$LOG_FILE" >&2
    printf '\n' >&2
  fi
}

fail() {
  local message="$*"
  log_raw "error: $message"
  # Don't print the error box here — the EXIT trap (cleanup) will call
  # print_error_box and progress_finalize. This avoids duplicate output.
  EXIT_MESSAGE="$message"
  exit 1
}

restore_file_backup() {
  local backup="$1"
  local target="$2"
  [[ -n "$backup" && -e "$backup" ]] || return 0
  rm -f "$target" 2>/dev/null || true
  mv "$backup" "$target" 2>/dev/null || true
}

restore_directory_backup() {
  local backup="$1"
  local target="$2"
  [[ -n "$backup" && -e "$backup" ]] || return 0
  rm -rf "$target" 2>/dev/null || true
  mv "$backup" "$target" 2>/dev/null || true
}

restore_profile_backups() {
  local index target backup
  for index in "${!PROFILE_BACKUP_TARGETS[@]}"; do
    target="${PROFILE_BACKUP_TARGETS[$index]}"
    backup="${PROFILE_BACKUP_FILES[$index]}"
    if [[ -n "$backup" && -e "$backup" ]]; then
      mkdir -p "$(dirname -- "$target")" 2>/dev/null || true
      mv "$backup" "$target" 2>/dev/null || true
    else
      rm -f "$target" 2>/dev/null || true
    fi
  done
}

rollback_if_needed() {
  [[ "$INSTALL_COMMITTED" == 0 ]] || return 0
  restore_profile_backups
  restore_file_backup "$BACKUP_ACTIVE_PRIMARY" "$ACTIVE_PATH_BRIDGE_DIR/$PRIMARY_COMMAND"
  restore_file_backup "$BACKUP_ACTIVE_ALIAS" "$ALIAS_PATH_BRIDGE_DIR/$ALIAS_COMMAND"
  restore_file_backup "$BACKUP_PRIMARY_COMMAND" "$BIN_DIR/$PRIMARY_COMMAND"
  restore_file_backup "$BACKUP_ALIAS_COMMAND" "$BIN_DIR/$ALIAS_COMMAND"
  restore_directory_backup "$BACKUP_APP_DIR" "$APP_DIR"
}

cleanup() {
  local status=$?
  set +e  # Disable errexit inside cleanup so command failures don't abort it.
  # Restore original stdout/stderr. When fail() calls exit 1 inside run_step's
  # >>"$LOG_FILE" 2>&1 redirection, the redirections persist during the EXIT
  # trap. This restores the original fds so cleanup output goes to the terminal.
  exec 1>&3 2>&4
  # If exiting with a failure, finalize the progress bar and print the error
  # box. This catches both run_step failures and fail() calls.
  if (( status != 0 )); then
    progress_finalize 'fail'
    if [[ "${PROGRESS_ERROR_PRINTED:-0}" == 0 ]]; then
      PROGRESS_ERROR_PRINTED=1
      if [[ -n "${EXIT_MESSAGE:-}" ]]; then
        print_error_box "$EXIT_MESSAGE"
      else
        print_error_box "command failed"
      fi
      if [[ -n "${LOG_FILE:-}" && -s "$LOG_FILE" ]]; then
        printf '\n%srecent log%s\n' "$DIM" "$RESET" >&2
        tail -n 20 "$LOG_FILE" | sed 's/^/  /' >&2 || true
      fi
    fi
    rollback_if_needed
  fi
  progress_show_cursor
  local path
  for path in "${CLEANUP_PATHS[@]:-}"; do
    [[ -n "$path" && -e "$path" ]] && rm -rf "$path" 2>/dev/null || true
  done
  if [[ -n "${LOCK_DIR:-}" && -n "${LOCK_TOKEN:-}" && -f "$LOCK_DIR/token" ]] && [[ "$(cat "$LOCK_DIR/token" 2>/dev/null || true)" == "$LOCK_TOKEN" ]]; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
  fi
}

on_error() {
  local status=$?
  local line_number="${1:-?}"
  local command_text="${2:-unknown}"
  log_raw "unhandled failure line=$line_number command=$command_text status=$status"
  # Don't print the error box here — cleanup() will do it. Just record the
  # message so cleanup can use it. This avoids duplicate error boxes.
  if [[ -z "${EXIT_MESSAGE:-}" ]]; then
    EXIT_MESSAGE="command failed at line $line_number"
  fi
  exit "$status"
}

trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
trap cleanup EXIT

# ─── PLATFORM AND PATH RESOLUTION ───────────────────────────────────────────

absolute_path() {
  local input="$1"
  case "$input" in
    ~) printf '%s' "$HOME" ;;
    ~/*) printf '%s/%s' "$HOME" "${input#~/}" ;;
    /*) printf '%s' "${input%/}" ;;
    *) printf '%s/%s' "$(pwd -P)" "${input%/}" ;;
  esac
}

canonical_file_path() {
  local input="$1"
  local parent base
  parent="$(dirname -- "$input")"
  base="$(basename -- "$input")"
  parent="$(cd -- "$parent" >/dev/null 2>&1 && pwd -P)" || return 1
  printf '%s/%s' "$parent" "$base"
}

script_directory() {
  local source="${BASH_SOURCE[0]:-$0}"
  cd -- "$(dirname -- "$source")" >/dev/null 2>&1 && pwd -P
}

detect_platform() {
  OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"
  case "$OS_NAME" in
    Darwin*) PLATFORM='darwin' ;;
    Linux*) PLATFORM='linux' ;;
    FreeBSD*) PLATFORM='freebsd' ;;
    OpenBSD*) PLATFORM='openbsd' ;;
    NetBSD*) PLATFORM='netbsd' ;;
    *) PLATFORM='unix' ;;
  esac
  ARCH="$(uname -m 2>/dev/null || printf 'unknown')"
}

reject_unsafe_path() {
  # Refuse paths that would be catastrophic to write into. The invariant:
  # every managed path must be inside a per-product subdirectory of a user
  # data directory, never a system root or a shared home.
  local label="$1"
  local path="$2"
  [[ -n "$path" ]] || fail "empty $label path"
  [[ "$path" == /* ]] || fail "$label path must be absolute: $path"
  [[ "$path" != *$'\n'* && "$path" != *$'\r'* ]] || fail "$label path contains newline characters"
  case "$path" in
    /|/home|/Users|/tmp|/var|/usr|/usr/local|/opt|"$HOME")
      fail "refusing broad $label path: $path"
      ;;
  esac
}

setup_paths() {
  # Resolve every managed directory from XDG/env overrides, falling back to
  # the per-platform conventions. All paths are absolute and validated.
  case "$PLATFORM" in
    darwin)
      APP_DIR="${APP_DIR_OVERRIDE:-${PRS_APP_DIR:-$HOME/Library/Application Support/$PRODUCT_SLUG}}"
      STATE_DIR="${STATE_DIR_OVERRIDE:-${PRS_STATE_DIR:-$HOME/Library/Application Support/$PRODUCT_SLUG/state}}"
      CONFIG_DIR="${CONFIG_DIR_OVERRIDE:-${PRS_CONFIG_DIR:-$HOME/Library/Application Support/$PRODUCT_SLUG/config}}"
      CACHE_DIR="${CACHE_DIR_OVERRIDE:-${PRS_CACHE_DIR:-$HOME/Library/Caches/$PRODUCT_SLUG}}"
      ;;
    *)
      APP_DIR="${APP_DIR_OVERRIDE:-${PRS_APP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/$PRODUCT_SLUG}}"
      STATE_DIR="${STATE_DIR_OVERRIDE:-${PRS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/$PRODUCT_SLUG}}"
      CONFIG_DIR="${CONFIG_DIR_OVERRIDE:-${PRS_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/$PRODUCT_SLUG}}"
      CACHE_DIR="${CACHE_DIR_OVERRIDE:-${PRS_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/$PRODUCT_SLUG}}"
      ;;
  esac
  BIN_DIR="${BIN_DIR_OVERRIDE:-${PRS_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}}"
  TMP_ROOT="${TMP_ROOT_OVERRIDE:-${PRS_TMP_ROOT:-${TMPDIR:-/tmp}}}"
  LOG_DIR="${PRS_LOG_DIR:-$STATE_DIR/logs}"

  APP_DIR="$(absolute_path "$APP_DIR")"
  STATE_DIR="$(absolute_path "$STATE_DIR")"
  CONFIG_DIR="$(absolute_path "$CONFIG_DIR")"
  CACHE_DIR="$(absolute_path "$CACHE_DIR")"
  BIN_DIR="$(absolute_path "$BIN_DIR")"
  TMP_ROOT="$(absolute_path "$TMP_ROOT")"
  LOG_DIR="$(absolute_path "$LOG_DIR")"

  reject_unsafe_path 'application directory' "$APP_DIR"
  reject_unsafe_path 'state directory' "$STATE_DIR"
  reject_unsafe_path 'config directory' "$CONFIG_DIR"
  reject_unsafe_path 'cache directory' "$CACHE_DIR"
  reject_unsafe_path 'binary directory' "$BIN_DIR"
  reject_unsafe_path 'log directory' "$LOG_DIR"

  if [[ "$DRY_RUN" == 1 ]]; then
    mkdir -p "$TMP_ROOT"
    LOG_DIR="${TMP_ROOT%/}/$PRODUCT_SLUG-dry-run-logs.$$"
    mkdir -p "$LOG_DIR"
    chmod "$PERMISSIONS_DIRECTORY_PRIVATE" "$LOG_DIR" 2>/dev/null || true
  else
    mkdir -p "$STATE_DIR" "$CONFIG_DIR" "$CACHE_DIR" "$BIN_DIR" "$LOG_DIR" "$TMP_ROOT"
    chmod "$PERMISSIONS_DIRECTORY_PRIVATE" "$STATE_DIR" "$CONFIG_DIR" "$CACHE_DIR" "$LOG_DIR" 2>/dev/null || true
  fi
  LOG_FILE="$LOG_DIR/$LOG_NAME"
  : > "$LOG_FILE"
  chmod "$PERMISSIONS_FILE_PRIVATE" "$LOG_FILE" 2>/dev/null || true
  if [[ "$DRY_RUN" == 1 ]]; then
    append_cleanup_path "$LOG_DIR"
  fi
}

acquire_install_lock() {
  # Atomic mkdir-based mutex. Stale locks are detected via kill -0 and removed.
  LOCK_DIR="$STATE_DIR/$INSTALL_LOCK_NAME"
  LOCK_TOKEN="$$.$(date +%s).$RANDOM"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    printf '%s\n' "$LOCK_TOKEN" > "$LOCK_DIR/token"
    return 0
  fi
  local existing_pid=''
  existing_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
    fail "another install is running: pid $existing_pid"
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR" || fail "could not acquire install lock: $LOCK_DIR"
  printf '%s\n' "$$" > "$LOCK_DIR/pid"
  printf '%s\n' "$LOCK_TOKEN" > "$LOCK_DIR/token"
}

create_temporary_workspace() {
  TMP_DIR="$(mktemp -d "${TMP_ROOT%/}/$PRODUCT_SLUG.XXXXXX")"
  append_cleanup_path "$TMP_DIR"
  STAGED_APP_DIR="$TMP_DIR/app"
  STAGED_BIN_DIR="$TMP_DIR/bin"
  mkdir -p "$STAGED_APP_DIR" "$STAGED_BIN_DIR"
}

# ─── ARGUMENT PARSING ───────────────────────────────────────────────────────

usage() {
  cat <<EOF
$PRODUCT_TITLE setup

Usage:
  bash install.sh [options]

Options:
  --source <path>          Install from a local prs source file
  --source-url <url>       Download a prs source file (default: $DEFAULT_SOURCE_URL)
  --sha256 <hash>          Verify source SHA-256
  --app-dir <path>         Managed runtime directory
  --bin-dir <path>         Command directory
  --state-dir <path>       State directory
  --config-dir <path>      Config directory
  --cache-dir <path>       Cache directory
  --tmp-dir <path>         Temporary workspace parent
  --logo <mode>            auto, text, small, medium, large
  --color <mode>           auto, always, never
  --no-path-repair         Do not edit shell startup files
  --no-active-bridge       Do not bridge into an already-active PATH directory
  --no-replace             Refuse replacement unless the existing command is managed
  --quiet                  Reduce output
  --force                  Force replacement of any existing command (overrides --no-replace)
  --dry-run                Validate only, do not install
  -h, --help               Show help

Environment:
  PRS_SOURCE               Local source file
  PRS_SOURCE_URL           Remote source file
  PRS_SOURCE_SHA256        Source SHA-256
  PRS_APP_DIR              Managed runtime directory
  PRS_BIN_DIR              Command directory
  PRS_STATE_DIR            State directory
  PRS_CONFIG_DIR           Config directory
  PRS_CACHE_DIR            Cache directory
  PRS_TMP_ROOT             Temporary workspace parent
  PRS_LOG_DIR              Log directory

When run via curl-pipe-bash with no --source and no --source-url, the installer
automatically downloads the runtime from $DEFAULT_SOURCE_URL, so the one-liner
"curl -fsSL https://raw.githubusercontent.com/vivid0o0/project-summarizer/main/install.sh | bash" works with no extra arguments.
EOF
}

require_arg() {
  local option="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "$option requires a value"
}

parse_args() {
  SOURCE_PATH="${PRS_SOURCE:-}"
  SOURCE_URL="${PRS_SOURCE_URL:-}"
  SOURCE_SHA256="${PRS_SOURCE_SHA256:-}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source) shift; require_arg --source "${1:-}"; SOURCE_PATH="$1" ;;
      --source-url) shift; require_arg --source-url "${1:-}"; SOURCE_URL="$1" ;;
      --sha256) shift; require_arg --sha256 "${1:-}"; SOURCE_SHA256="$1" ;;
      --app-dir) shift; require_arg --app-dir "${1:-}"; APP_DIR_OVERRIDE="$1" ;;
      --bin-dir) shift; require_arg --bin-dir "${1:-}"; BIN_DIR_OVERRIDE="$1" ;;
      --state-dir) shift; require_arg --state-dir "${1:-}"; STATE_DIR_OVERRIDE="$1" ;;
      --config-dir) shift; require_arg --config-dir "${1:-}"; CONFIG_DIR_OVERRIDE="$1" ;;
      --cache-dir) shift; require_arg --cache-dir "${1:-}"; CACHE_DIR_OVERRIDE="$1" ;;
      --tmp-dir) shift; require_arg --tmp-dir "${1:-}"; TMP_ROOT_OVERRIDE="$1" ;;
      --logo) shift; require_arg --logo "${1:-}"; LOGO_MODE="$1" ;;
      --color) shift; require_arg --color "${1:-}"; COLOR_MODE="$1" ;;
      --no-path-repair) NO_PATH_REPAIR=1 ;;
      --no-active-bridge) NO_ACTIVE_BRIDGE=1 ;;
      --no-replace) NO_REPLACE=1 ;;
      --quiet) QUIET=1 ;;
      --force) FORCE=1 ;;
      --dry-run) DRY_RUN=1 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown option: $1" ;;
    esac
    shift
  done
  case "$LOGO_MODE" in auto|text|small|medium|large) : ;; *) fail "invalid logo mode: $LOGO_MODE" ;; esac
  case "$COLOR_MODE" in auto|always|never) : ;; *) fail "invalid color mode: $COLOR_MODE" ;; esac
}

# ─── PYTHON RUNTIME ─────────────────────────────────────────────────────────
# Detect a suitable Python interpreter. If none is found, attempt to
# install one via the system package manager. The invariant: after this
# section, PYTHON_BIN points to a working interpreter or the install aborts.

python_version_ok() {
  # Check whether a candidate interpreter meets MINIMUM_PYTHON_VERSION. The
  # version string is passed as argv[1] and parsed in Python so the threshold
  # is derived from the PRODUCT CONTRACT constant, not hardcoded in the
  # heredoc. Only the first two components (major.minor) are compared,
  # matching sys.version_info tuple semantics; a trailing patch component
  # is ignored.
  local candidate="$1"
  "$candidate" - "$MINIMUM_PYTHON_VERSION" <<'PY' >/dev/null 2>&1
import sys
parts = sys.argv[1].split('.')
raise SystemExit(0 if sys.version_info >= (int(parts[0]), int(parts[1])) else 1)
PY
}

find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3 python python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    [[ -n "$candidate" ]] || continue
    if has "$candidate"; then
      candidate="$(command -v "$candidate")"
    fi
    [[ -x "$candidate" ]] || continue
    if python_version_ok "$candidate"; then
      PYTHON_BIN="$candidate"
      return 0
    fi
  done
  return 1
}

privilege_command() {
  # Return the privilege-escalation command (sudo/doas) if available and the
  # user is not already root. Returns non-zero if neither is available.
  if [[ "$(id -u)" == 0 ]]; then
    printf ''
    return 0
  elif has sudo; then
    printf 'sudo'
    return 0
  elif has doas; then
    printf 'doas'
    return 0
  fi
  return 1
}

package_manager() {
  # Detect the system package manager. Order matters: most-specific first.
  if [[ "$PLATFORM" == darwin ]] && has brew; then printf 'brew'; return 0; fi
  if has apt-get; then printf 'apt-get'; return 0; fi
  if has dnf; then printf 'dnf'; return 0; fi
  if has yum; then printf 'yum'; return 0; fi
  if has pacman; then printf 'pacman'; return 0; fi
  if has zypper; then printf 'zypper'; return 0; fi
  if has apk; then printf 'apk'; return 0; fi
  if has xbps-install; then printf 'xbps-install'; return 0; fi
  if has pkg; then printf 'pkg'; return 0; fi
  if has emerge; then printf 'emerge'; return 0; fi
  if has slackpkg; then printf 'slackpkg'; return 0; fi
  if has pkg_add; then printf 'pkg_add'; return 0; fi
  if has eopkg; then printf 'eopkg'; return 0; fi
  if has conda; then printf 'conda'; return 0; fi
  return 1
}

run_privileged() {
  local priv
  priv="$(privilege_command || true)"
  if [[ -n "$priv" ]]; then
    "$priv" "$@"
  else
    "$@"
  fi
}

install_python_with_package_manager() {
  # Best-effort automatic Python install. Each branch is a single
  # distribution family. If a branch fails, the caller fails the install.
  local manager
  manager="$(package_manager || true)"
  [[ -n "$manager" ]] || return 1
  case "$manager" in
    brew) brew install python ;;
    apt-get) run_privileged apt-get update -qq && run_privileged apt-get install -y -qq python3 ;;
    dnf) run_privileged dnf install -y -q python3 ;;
    yum) run_privileged yum install -y -q python3 ;;
    pacman) run_privileged pacman -S --noconfirm --needed python ;;
    zypper) run_privileged zypper install -y python3 ;;
    apk) run_privileged apk add --no-cache python3 ;;
    xbps-install) run_privileged xbps-install -Sy python3 ;;
    pkg) run_privileged pkg install -y python3 ;;
    emerge) run_privileged emerge -q dev-lang/python ;;
    slackpkg) run_privileged slackpkg -batch=on -default_answer=y install python3 ;;
    pkg_add) run_privileged pkg_add python-3 ;;
    eopkg) run_privileged eopkg install -y python3 ;;
    conda) conda install -y python ;;
    *) return 1 ;;
  esac
}

ensure_python() {
  if find_python; then
    log_raw "python=$PYTHON_BIN"
    return 0
  fi
  log_raw 'python missing; attempting package manager repair'
  install_python_with_package_manager || fail "Python $MINIMUM_PYTHON_VERSION+ is required and could not be installed automatically; install it manually and re-run"
  find_python || fail "Python $MINIMUM_PYTHON_VERSION+ remains unavailable after package manager repair"
}

# ─── CLIPBOARD INTEGRATION ──────────────────────────────────────────────────
# auto-copy is configurable in config.yaml. The installer checks whether a
# clipboard backend is available and logs the result, so users can verify
# support. The runtime itself distinguishes "environment unreachable" (warn
# and continue, exit 0) from "backend broke" (exit 1), so a missing backend
# never breaks a scan.

clipboard_backend_available() {
  if [[ "$PLATFORM" == darwin ]]; then
    has pbcopy && return 0
    return 1
  fi
  has wl-copy || has xclip || has xsel || has clip.exe
}

ensure_clipboard_integration() {
  if clipboard_backend_available; then
    log_raw 'clipboard backend available'
    return 0
  fi
  log_raw 'clipboard backend not detected; auto-copy can be enabled after installing pbcopy on macOS, wl-copy on Wayland, xclip or xsel on X11, or clip.exe on WSL'
}

# ─── DOWNLOAD AND CHECKSUM ──────────────────────────────────────────────────

sha256_file() {
  local file="$1"
  if has sha256sum; then
    sha256sum "$file" | awk '{print $1}'
  elif has shasum; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    "$PYTHON_BIN" - "$file" <<'PY'
import hashlib
import sys
path = sys.argv[1]
digest = hashlib.sha256()
with open(path, 'rb') as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
        digest.update(chunk)
print(digest.hexdigest())
PY
  fi
}

verify_sha256_if_requested() {
  local file="$1"
  [[ -n "$SOURCE_SHA256" ]] || return 0
  local expected actual
  expected="$(printf '%s' "$SOURCE_SHA256" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail "invalid SHA-256: $SOURCE_SHA256"
  actual="$(sha256_file "$file" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [[ "$actual" == "$expected" ]] || fail "source checksum mismatch"
}

download_file() {
  # Download a URL to a path using the first available tool: curl, wget, or
  # Python's urllib. All three honor NETWORK_MAXIMUM_TIMEOUT_SECONDS and follow
  # redirects. curl and wget retry NETWORK_RETRY_COUNT times; the Python
  # fallback does not retry (it is only reached when both curl and wget are
  # absent, so retry logic would add complexity for a rarely-hit path).
  local url="$1"
  local output="$2"
  if has curl; then
    curl --fail --location --show-error --silent \
      --connect-timeout "$NETWORK_CONNECT_TIMEOUT_SECONDS" \
      --max-time "$NETWORK_MAXIMUM_TIMEOUT_SECONDS" \
      --retry "$NETWORK_RETRY_COUNT" \
      --retry-delay "$NETWORK_RETRY_DELAY_SECONDS" \
      "$url" -o "$output"
  elif has wget; then
    wget --quiet --timeout="$NETWORK_MAXIMUM_TIMEOUT_SECONDS" --tries="$NETWORK_RETRY_COUNT" "$url" -O "$output"
  else
    "$PYTHON_BIN" - "$url" "$output" "$NETWORK_MAXIMUM_TIMEOUT_SECONDS" "$NETWORK_USER_AGENT" <<'PY'
import sys
import urllib.request
url, output, timeout, user_agent = sys.argv[1:5]
request = urllib.request.Request(url, headers={'User-Agent': user_agent})
with urllib.request.urlopen(request, timeout=int(timeout)) as response, open(output, 'wb') as handle:
    handle.write(response.read())
PY
  fi
}

validate_source_url() {
  local url="$1"
  case "$url" in
    https://*) ;;
    *) fail "source URL must use HTTPS: $url" ;;
  esac
  [[ "$url" != *$'\n'* && "$url" != *$'\r'* ]] || fail 'source URL contains newline characters'
}

# ─── SOURCE RESOLUTION AND VALIDATION ───────────────────────────────────────
# Resolve the prs source file from --source, --source-url, or the script's
# own directory. When run via curl-pipe-bash (no --source, no --source-url,
# no source in $SCRIPT_DIR), default to DEFAULT_SOURCE_URL so the install
# proceeds without any user argument.

source_candidate_score() {
  # Heuristic: score how likely a file is the prs runtime. Rejects files
  # named install* (the installer itself). Scores by signature constants.
  local file="$1"
  [[ -f "$file" && -r "$file" ]] || return 1
  [[ "$(basename -- "$file")" != install* ]] || return 1
  "$PYTHON_BIN" - "$file" <<'PY'
import ast
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
try:
    text = path.read_text(encoding='utf-8')
    tree = ast.parse(text)
except Exception:
    raise SystemExit(1)
score = 0
score += 20 if 'PROGRAM_NAME = "prs"' in text or "PROGRAM_NAME = 'prs'" in text else 0
score += 10 if 'Project Summarizer' in text else 0
score += 8 if 'CONFIG_FILE_NAME = "config.yaml"' in text or "CONFIG_FILE_NAME = 'config.yaml'" in text else 0
score += 8 if 'argparse' in text else 0
score += 8 if any(isinstance(node, ast.FunctionDef) and node.name == 'main' for node in ast.walk(tree)) else 0
score += 4 if 'SCAN_DATA_ITEMS' in text else 0
score += 4 if 'STYLING_LEVELS' in text else 0
score += 4 if 'VERSION' in text else 0
if score < 40:
    raise SystemExit(1)
print(score)
PY
}

search_source_in_directory() {
  # Look for the prs runtime in a directory by trying known filenames and
  # scoring each candidate. The highest-scoring candidate wins.
  local directory="$1"
  local best=''
  local best_score=0
  local candidate score
  for candidate in \
    "$directory/prs.py" \
    "$directory/prs" \
    "$directory/extract.py" \
    "$directory/extract"; do
    [[ -f "$candidate" ]] || continue
    score="$(source_candidate_score "$candidate" 2>/dev/null || true)"
    [[ "$score" =~ ^[0-9]+$ ]] || continue
    if (( score > best_score )); then
      best_score="$score"
      best="$candidate"
    fi
  done
  [[ -n "$best" ]] && printf '%s' "$best"
}

resolve_local_source() {
  if [[ -n "$SOURCE_PATH" ]]; then
    SOURCE_PATH="$(absolute_path "$SOURCE_PATH")"
    canonical_file_path "$SOURCE_PATH"
    return 0
  fi
  local found=''
  found="$(search_source_in_directory "$SCRIPT_DIR" || true)"
  [[ -n "$found" ]] && { canonical_file_path "$found"; return 0; }
  return 1
}

resolve_skill_file() {
  # Locate SKILL.md next to the resolved source. If the source was
  # downloaded, also try to download SKILL.md from the same URL prefix.
  local source_dir=''
  source_dir="$(canonical_file_path "$(dirname -- "$RESOLVED_SOURCE")" 2>/dev/null || absolute_path "$(dirname -- "$RESOLVED_SOURCE")")"
  local candidate="$source_dir/$SKILL_FILE_NAME"
  if [[ -f "$candidate" && -r "$candidate" ]]; then
    RESOLVED_SKILL="$(canonical_file_path "$candidate" 2>/dev/null || printf '%s' "$candidate")"
    log_raw "skill=$RESOLVED_SKILL"
    return 0
  fi
  if [[ -n "$SOURCE_URL" ]]; then
    validate_source_url "$SOURCE_URL"
    local skill_url="${SOURCE_URL%/*}/$SKILL_FILE_NAME"
    local downloaded="$TMP_DIR/skill-download.md"
    if download_file "$skill_url" "$downloaded" 2>/dev/null; then
      RESOLVED_SKILL="$downloaded"
      log_raw "skill=$RESOLVED_SKILL (downloaded from $skill_url)"
      return 0
    fi
  fi
  RESOLVED_SKILL=''
  log_raw "skill=(not available)"
}

resolve_config_file() {
  local source_dir=''
  source_dir="$(canonical_file_path "$(dirname -- "$RESOLVED_SOURCE")" 2>/dev/null || absolute_path "$(dirname -- "$RESOLVED_SOURCE")")"
  local candidate="$source_dir/$CONFIG_FILE_NAME"
  if [[ -f "$candidate" && -r "$candidate" ]]; then
    RESOLVED_CONFIG="$(canonical_file_path "$candidate" 2>/dev/null || printf '%s' "$candidate")"
    log_raw "config=$RESOLVED_CONFIG"
    return 0
  fi
  if [[ -n "$SOURCE_URL" ]]; then
    validate_source_url "$SOURCE_URL"
    local config_url="${SOURCE_URL%/*}/$CONFIG_FILE_NAME"
    local downloaded="$TMP_DIR/config-download.yaml"
    if download_file "$config_url" "$downloaded" 2>/dev/null; then
      RESOLVED_CONFIG="$downloaded"
      log_raw "config=$RESOLVED_CONFIG (downloaded from $config_url)"
      return 0
    fi
  fi
  RESOLVED_CONFIG=''
  log_raw "config=(not available)"
}

resolve_source() {
  local downloaded=''
  if [[ -n "$SOURCE_URL" ]]; then
    validate_source_url "$SOURCE_URL"
    downloaded="$TMP_DIR/source-download.py"
    download_file "$SOURCE_URL" "$downloaded" || fail "unable to download source from $SOURCE_URL"
    RESOLVED_SOURCE="$downloaded"
  else
    RESOLVED_SOURCE="$(resolve_local_source || true)"
    if [[ -z "$RESOLVED_SOURCE" ]]; then
      # curl-pipe-bash fallback: no local source, default to the canonical URL.
      log_raw "no local source; defaulting to $DEFAULT_SOURCE_URL"
      SOURCE_URL="$DEFAULT_SOURCE_URL"
      validate_source_url "$SOURCE_URL"
      downloaded="$TMP_DIR/source-download.py"
      download_file "$SOURCE_URL" "$downloaded" || fail "unable to download source from $DEFAULT_SOURCE_URL; check your internet connection or pass --source <path>"
      RESOLVED_SOURCE="$downloaded"
    fi
  fi
  [[ -n "$RESOLVED_SOURCE" && -r "$RESOLVED_SOURCE" ]] || fail "source program not found; pass --source <path>"
  verify_sha256_if_requested "$RESOLVED_SOURCE"
  resolve_skill_file
  resolve_config_file
  log_raw "source=$RESOLVED_SOURCE"
}

validate_source_program() {
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" - "$RESOLVED_SOURCE" <<'PY'
import pathlib
import sys
source = pathlib.Path(sys.argv[1])
compile(source.read_text(encoding='utf-8'), str(source), 'exec')
PY
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" "$RESOLVED_SOURCE" version >/dev/null
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" "$RESOLVED_SOURCE" help >/dev/null
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" "$RESOLVED_SOURCE" status >/dev/null
  local fixture output
  fixture="$(mktemp -d "$TMP_DIR/source-fixture.XXXXXX")"
  printf 'demo\n' > "$fixture/README.md"
  output="$(PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" "$RESOLVED_SOURCE" "$fixture" --only -e .md --scan-styling minimal --auto-copy false --scan-timeout "$DEFAULT_SCAN_TIMEOUT")"
  grep -Fq 'README.md' <<< "$output" || fail 'source validation did not render fixture file'
  grep -Fq 'largest README.md' <<< "$output" || fail 'source validation did not render filtered summary metadata'
}

# ─── STAGING ────────────────────────────────────────────────────────────────
# Stage the source and command wrappers in a temp directory, validate the
# staged runtime, then atomically move the staged app dir into place.

copy_source_to_stage() {
  install -m "$PERMISSIONS_EXECUTABLE" "$RESOLVED_SOURCE" "$STAGED_APP_DIR/$RUNTIME_SOURCE_NAME"
  if [[ -n "$RESOLVED_SKILL" && -r "$RESOLVED_SKILL" ]]; then
    install -m "$PERMISSIONS_DATA_FILE" "$RESOLVED_SKILL" "$STAGED_APP_DIR/$SKILL_FILE_NAME"
  fi
  if [[ -n "$RESOLVED_CONFIG" && -r "$RESOLVED_CONFIG" ]]; then
    install -m "$PERMISSIONS_DATA_FILE" "$RESOLVED_CONFIG" "$STAGED_APP_DIR/$CONFIG_FILE_NAME"
  fi
  printf '%s\n' "$MANAGED_MARKER" > "$STAGED_APP_DIR/.managed"
  printf '%s\n' "$INSTALLER_VERSION" > "$STAGED_APP_DIR/.installer-version"
}

write_command_wrapper() {
  # Generate the bash wrapper that invokes the managed runtime. The wrapper
  # re-detects Python at runtime so it survives a Python upgrade.
  local target="$1"
  local command_name="$2"
  local app_dir="$3"
  local python_bin="$4"
  cat > "$target" <<EOF
#!/usr/bin/env bash
# $command_name -- Project Summarizer command
# $MANAGED_MARKER
# Runs the managed PRS runtime installed by install.sh.
set -Eeuo pipefail
APP_DIR=$(shell_quote "$app_dir")
PYTHON_BIN=$(shell_quote "$python_bin")
SOURCE_FILE="\$APP_DIR/$RUNTIME_SOURCE_NAME"
if [[ ! -r "\$SOURCE_FILE" ]]; then
  printf 'prs: managed runtime missing: %s\n' "\$SOURCE_FILE" >&2
  exit 127
fi
if [[ ! -x "\$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="\$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="\$(command -v python)"
  else
    printf 'prs: Python %s+ runtime missing. Re-run install.sh.\n' "$MINIMUM_PYTHON_VERSION" >&2
    exit 127
  fi
fi
export PYTHONDONTWRITEBYTECODE=1
exec "\$PYTHON_BIN" "\$SOURCE_FILE" "\$@"
EOF
  chmod "$PERMISSIONS_EXECUTABLE" "$target"
}

write_stage_commands() {
  write_command_wrapper "$STAGED_BIN_DIR/$PRIMARY_COMMAND" "$PRIMARY_COMMAND" "$STAGED_APP_DIR" "$PYTHON_BIN"
  write_command_wrapper "$STAGED_BIN_DIR/$ALIAS_COMMAND" "$ALIAS_COMMAND" "$STAGED_APP_DIR" "$PYTHON_BIN"
}

validate_staged_runtime() {
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" - "$STAGED_APP_DIR/$RUNTIME_SOURCE_NAME" <<'PY'
import pathlib
import sys
source = pathlib.Path(sys.argv[1])
compile(source.read_text(encoding='utf-8'), str(source), 'exec')
PY
  "$STAGED_BIN_DIR/$PRIMARY_COMMAND" version >/dev/null
  "$STAGED_BIN_DIR/$PRIMARY_COMMAND" help >/dev/null
  "$STAGED_BIN_DIR/$PRIMARY_COMMAND" status >/dev/null
  "$STAGED_BIN_DIR/$ALIAS_COMMAND" version >/dev/null
  local fixture output
  fixture="$(mktemp -d "$TMP_DIR/staged-fixture.XXXXXX")"
  printf 'demo\n' > "$fixture/README.md"
  output="$("$STAGED_BIN_DIR/$PRIMARY_COMMAND" "$fixture" --only -e .md --scan-styling minimal --auto-copy false --scan-timeout "$DEFAULT_SCAN_TIMEOUT")"
  grep -Fq 'README.md' <<< "$output" || fail 'staged command did not render fixture file'
  grep -Fq 'largest README.md' <<< "$output" || fail 'staged command did not render filtered summary metadata'
}

# ─── COMMAND OWNERSHIP AND BACKUP ───────────────────────────────────────────
# Before replacing any existing command, classify it (managed, legacy, or
# foreign) and decide whether to back it up or abort. The invariant: a foreign
# command is never silently overwritten.

is_managed_command() {
  local target="$1"
  [[ -f "$target" ]] || return 1
  grep -Fqs "$MANAGED_MARKER" "$target" 2>/dev/null
}

is_managed_bridge() {
  local target="$1"
  [[ -f "$target" ]] || return 1
  grep -Fqs "$BRIDGE_MARKER" "$target" 2>/dev/null
}

looks_like_legacy_project_summarizer() {
  local target="$1"
  [[ -f "$target" ]] || return 1
  grep -Eqs 'Project Summarizer|project-summarizer|PROGRAM_NAME = .prs.|prs.py' "$target" 2>/dev/null
}

replace_policy_allows_backup() {
  local target="$1"
  [[ ! -e "$target" && ! -L "$target" ]] && return 0
  [[ "$FORCE" == 1 ]] && return 0
  [[ "$NO_REPLACE" == 1 ]] && return 1
  return 0
}

ensure_command_can_be_replaced() {
  local target="$1"
  [[ ! -e "$target" && ! -L "$target" ]] && return 0
  is_managed_command "$target" && return 0
  looks_like_legacy_project_summarizer "$target" && return 0
  replace_policy_allows_backup "$target" && return 0
  fail "existing command is protected by --no-replace: $target"
}

backup_path_if_present() {
  local target="$1"
  local result_var="$2"
  local backup=''
  if [[ -e "$target" || -L "$target" ]]; then
    backup="$target.rollback.$$"
    mv "$target" "$backup"
    log_raw "backup: $target -> $backup"
  fi
  printf -v "$result_var" '%s' "$backup"
}

backup_profile() {
  local target="$1"
  local existing
  for existing in "${PROFILE_BACKUP_TARGETS[@]:-}"; do
    [[ "$existing" == "$target" ]] && return 0
  done
  PROFILE_BACKUP_TARGETS+=("$target")
  if [[ -e "$target" ]]; then
    local backup="$target.rollback.$$"
    cp -p "$target" "$backup" 2>/dev/null || cp "$target" "$backup"
    PROFILE_BACKUP_FILES+=("$backup")
  else
    PROFILE_BACKUP_FILES+=("")
  fi
}

# ─── COMMIT AND PATH INTEGRATION ────────────────────────────────────────────

commit_runtime() {
  if [[ "$DRY_RUN" == 1 ]]; then
    log_raw 'dry-run: commit runtime skipped'
    return 0
  fi
  mkdir -p "$(dirname -- "$APP_DIR")"
  if [[ -e "$APP_DIR" ]]; then
    BACKUP_APP_DIR="$APP_DIR.rollback.$$"
    mv "$APP_DIR" "$BACKUP_APP_DIR"
  fi
  mv "$STAGED_APP_DIR" "$APP_DIR"
  STAGED_APP_DIR=''
}

install_command_files() {
  if [[ "$DRY_RUN" == 1 ]]; then
    log_raw 'dry-run: command install skipped'
    return 0
  fi
  mkdir -p "$BIN_DIR"
  ensure_command_can_be_replaced "$BIN_DIR/$PRIMARY_COMMAND"
  ensure_command_can_be_replaced "$BIN_DIR/$ALIAS_COMMAND"
  backup_path_if_present "$BIN_DIR/$PRIMARY_COMMAND" BACKUP_PRIMARY_COMMAND
  backup_path_if_present "$BIN_DIR/$ALIAS_COMMAND" BACKUP_ALIAS_COMMAND
  write_command_wrapper "$BIN_DIR/$PRIMARY_COMMAND" "$PRIMARY_COMMAND" "$APP_DIR" "$PYTHON_BIN"
  write_command_wrapper "$BIN_DIR/$ALIAS_COMMAND" "$ALIAS_COMMAND" "$APP_DIR" "$PYTHON_BIN"
}

path_contains_directory() {
  local directory="$1"
  local search_path="${2:-$ORIGINAL_PATH}"
  case ":$search_path:" in
    *":$directory:"*) return 0 ;;
    *) return 1 ;;
  esac
}

path_directory_can_host_command() {
  local directory="$1"
  [[ -n "$directory" && "$directory" == /* && -d "$directory" && -w "$directory" ]] || return 1
  [[ "$directory" != *$'\n'* && "$directory" != *$'\r'* ]] || return 1
  case "$directory" in
    */sbin|*/sbin/) return 1 ;;
  esac
  return 0
}

first_writable_active_path_directory() {
  local entry
  IFS=':' read -r -a entries <<< "$ORIGINAL_PATH"
  for entry in "${entries[@]:-}"; do
    path_directory_can_host_command "$entry" || continue
    printf '%s' "$entry"
    return 0
  done
  return 1
}

ensure_bridge_target_replaceable() {
  local target="$1"
  local managed_launcher="$2"
  [[ ! -e "$target" && ! -L "$target" ]] && return 0
  if [[ "$target" -ef "$managed_launcher" ]] 2>/dev/null; then
    return 0
  fi
  is_managed_bridge "$target" && return 0
  is_managed_command "$target" && return 0
  replace_policy_allows_backup "$target" && return 0
  fail "existing active PATH command is protected by --no-replace: $target"
}

write_active_bridge() {
  local command_name="$1"
  local target_dir="$2"
  local backup_var="$3"
  local launcher="$BIN_DIR/$command_name"
  local target="$target_dir/$command_name"
  ensure_bridge_target_replaceable "$target" "$launcher"
  if [[ "$target" -ef "$launcher" ]] 2>/dev/null; then
    return 0
  fi
  backup_path_if_present "$target" "$backup_var"
  cat > "$target" <<EOF
#!/usr/bin/env bash
# $command_name -- Project Summarizer active PATH bridge
# $BRIDGE_MARKER
exec $(shell_quote "$launcher") "\$@"
EOF
  chmod "$PERMISSIONS_EXECUTABLE" "$target"
}

create_active_command_bridge() {
  # If $BIN_DIR is not on PATH, bridge into the first writable PATH directory
  # so `prs` works immediately without a shell restart. The bridge is a thin
  # exec wrapper, not a copy.
  [[ "$NO_ACTIVE_BRIDGE" == 1 || "$NO_PATH_REPAIR" == 1 || "$DRY_RUN" == 1 ]] && return 0
  path_contains_directory "$BIN_DIR" && return 0
  local directory
  directory="$(first_writable_active_path_directory || true)"
  [[ -n "$directory" ]] || return 0
  ACTIVE_PATH_BRIDGE_DIR="$directory"
  ALIAS_PATH_BRIDGE_DIR="$directory"
  write_active_bridge "$PRIMARY_COMMAND" "$directory" BACKUP_ACTIVE_PRIMARY
  write_active_bridge "$ALIAS_COMMAND" "$directory" BACKUP_ACTIVE_ALIAS
}

shell_name() {
  basename -- "${SHELL:-sh}"
}

profile_targets() {
  # Return the profile files to edit for the current shell. One per line,
  # most-specific first. Fish gets a dedicated conf.d file.
  local shell
  shell="$(shell_name)"
  case "$shell" in
    zsh) printf '%s\n' "${ZDOTDIR:-$HOME}/.zshrc" "${ZDOTDIR:-$HOME}/.zprofile" ;;
    fish) printf '%s\n' "$HOME/.config/fish/conf.d/$PRODUCT_SLUG.fish" ;;
    bash) printf '%s\n' "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" ;;
    ksh|mksh) printf '%s\n' "$HOME/.kshrc" "$HOME/.profile" ;;
    *) printf '%s\n' "$HOME/.profile" ;;
  esac
}

write_path_block_to_profile() {
  local profile="$1"
  mkdir -p "$(dirname -- "$profile")"
  touch "$profile"
  if grep -Fqs "$PATH_BLOCK_BEGIN" "$profile" 2>/dev/null; then
    return 0
  fi
  backup_profile "$profile"
  if [[ "$profile" == *.fish ]]; then
    # shellcheck disable=SC2016  # $PATH must remain literal in the generated fish snippet
    printf '\n%s\nif not contains -- %s $PATH\n    fish_add_path -- %s\nend\n%s\n' \
      "$PATH_BLOCK_BEGIN" "$(fish_quote "$BIN_DIR")" "$(fish_quote "$BIN_DIR")" "$PATH_BLOCK_END" >> "$profile"
  else
    # shellcheck disable=SC2016  # "$PATH" must remain literal in the generated shell snippet
    printf '\n%s\nexport PATH=%s:"$PATH"\n%s\n' \
      "$PATH_BLOCK_BEGIN" "$(single_quote "$BIN_DIR")" "$PATH_BLOCK_END" >> "$profile"
  fi
}

repair_shell_path() {
  [[ "$NO_PATH_REPAIR" == 1 || "$DRY_RUN" == 1 ]] && return 0
  path_contains_directory "$BIN_DIR" && return 0
  local profile
  while IFS= read -r profile; do
    [[ -n "$profile" ]] || continue
    write_path_block_to_profile "$profile"
  done < <(profile_targets)
}

# ─── POST-INSTALL VALIDATION ────────────────────────────────────────────────

validate_installed_commands() {
  if [[ "$DRY_RUN" == 1 ]]; then
    return 0
  fi
  [[ -x "$BIN_DIR/$PRIMARY_COMMAND" ]] || fail "installed command missing: $BIN_DIR/$PRIMARY_COMMAND"
  [[ -x "$BIN_DIR/$ALIAS_COMMAND" ]] || fail "installed alias missing: $BIN_DIR/$ALIAS_COMMAND"
  [[ -f "$APP_DIR/$CONFIG_FILE_NAME" ]] || fail "installed default config missing: $APP_DIR/$CONFIG_FILE_NAME"
  "$BIN_DIR/$PRIMARY_COMMAND" version >/dev/null
  "$BIN_DIR/$PRIMARY_COMMAND" help >/dev/null
  "$BIN_DIR/$PRIMARY_COMMAND" status >/dev/null
  "$BIN_DIR/$ALIAS_COMMAND" version >/dev/null
  "$BIN_DIR/$ALIAS_COMMAND" status >/dev/null
  local fixture output
  fixture="$(mktemp -d "$TMP_DIR/install-fixture.XXXXXX")"
  printf 'demo\n' > "$fixture/README.md"
  output="$("$BIN_DIR/$PRIMARY_COMMAND" "$fixture" --only -e .md --scan-styling minimal --auto-copy false --scan-timeout "$DEFAULT_SCAN_TIMEOUT")"
  grep -Fq 'README.md' <<< "$output" || fail 'installed command did not render fixture file'
  grep -Fq 'largest README.md' <<< "$output" || fail 'installed command did not render fixture summary'
  if path_contains_directory "$BIN_DIR" "$ORIGINAL_PATH" || command -v "$PRIMARY_COMMAND" >/dev/null 2>&1; then
    PATH="$ORIGINAL_PATH" "$PRIMARY_COMMAND" version >/dev/null 2>&1 || true
  fi
}

write_manifest() {
  [[ "$DRY_RUN" == 1 ]] && return 0
  local manifest="$APP_DIR/$MANIFEST_NAME"
  local skill_entry=''
  local config_entry=''
  if [[ -n "$RESOLVED_SKILL" && -r "$RESOLVED_SKILL" ]]; then
    skill_entry="skill=$(printf '%q' "$RESOLVED_SKILL")"
  else
    skill_entry="skill=''"
  fi
  if [[ -n "$RESOLVED_CONFIG" && -r "$RESOLVED_CONFIG" ]]; then
    config_entry="config=$(printf '%q' "$RESOLVED_CONFIG")"
  else
    config_entry="config=''"
  fi
  cat > "$manifest" <<EOF
product=$(printf '%q' "$PRODUCT_TITLE")
primary_command=$(printf '%q' "$PRIMARY_COMMAND")
alias_command=$(printf '%q' "$ALIAS_COMMAND")
installer_version=$(printf '%q' "$INSTALLER_VERSION")
installed_at=$(printf '%q' "$(utc_now)")
platform=$(printf '%q' "$PLATFORM")
arch=$(printf '%q' "$ARCH")
python=$(printf '%q' "$PYTHON_BIN")
source=$(printf '%q' "$RESOLVED_SOURCE")
$skill_entry
$config_entry
app_dir=$(printf '%q' "$APP_DIR")
bin_dir=$(printf '%q' "$BIN_DIR")
state_dir=$(printf '%q' "$STATE_DIR")
config_dir=$(printf '%q' "$CONFIG_DIR")
cache_dir=$(printf '%q' "$CACHE_DIR")
log=$(printf '%q' "$LOG_FILE")
EOF
  chmod "$PERMISSIONS_FILE_PRIVATE" "$manifest" 2>/dev/null || true
}

finalize_install() {
  INSTALL_COMMITTED=1
  rm -rf "${BACKUP_APP_DIR:-}" 2>/dev/null || true
  local backup
  for backup in "${PROFILE_BACKUP_FILES[@]:-}"; do
    [[ -n "$backup" && -e "$backup" ]] && rm -f "$backup" 2>/dev/null || true
  done
  for backup in "${BACKUP_PRIMARY_COMMAND:-}" "${BACKUP_ALIAS_COMMAND:-}" "${BACKUP_ACTIVE_PRIMARY:-}" "${BACKUP_ACTIVE_ALIAS:-}"; do
    if [[ -n "$backup" && -e "$backup" ]]; then
      log_raw "preserved replaced command backup: $backup"
    fi
  done
  return 0
}

# ─── EXISTING INSTALL DETECTION ─────────────────────────────────────────────
# Detect whether prs is already installed, compare versions, and decide
# whether to proceed with a fresh install, an upgrade, or a no-op. This makes
# the installer idempotent: re-running it upgrades or repairs rather than
# blindly overwriting.

EXISTING_INSTALL_VERSION=''

detect_existing_install() {
  # Populate EXISTING_INSTALL_VERSION by reading the manifest, if any. The
  # manifest is written with printf %q, so each value is shell-quoted. We
  # extract only the installer_version field (a simple date string that does
  # not need unquoting beyond stripping surrounding quotes).
  EXISTING_INSTALL_VERSION=''
  local manifest="$APP_DIR/$MANIFEST_NAME"
  if [[ ! -f "$manifest" ]]; then
    return 0
  fi
  local line key value
  while IFS='=' read -r key value; do
    [[ "$key" == "installer_version" ]] || continue
    # Strip surrounding single quotes added by printf %q for simple strings.
    value="${value#\'}"
    value="${value%\'}"
    EXISTING_INSTALL_VERSION="$value"
    break
  done < "$manifest"
}

should_skip_install() {
  # If the existing install is the same version as this installer and the
  # runtime + commands + config are intact, skip the install (no-op). Otherwise
  # proceed (upgrade or repair). Always returns 1 under --force or --dry-run.
  [[ "$FORCE" == 0 && "$DRY_RUN" == 0 ]] || return 1
  [[ -n "$EXISTING_INSTALL_VERSION" ]] || return 1
  [[ "$EXISTING_INSTALL_VERSION" == "$INSTALLER_VERSION" ]] || return 1
  [[ -f "$APP_DIR/$RUNTIME_SOURCE_NAME" ]] || return 1
  [[ -x "$BIN_DIR/$PRIMARY_COMMAND" ]] || return 1
  [[ -x "$BIN_DIR/$ALIAS_COMMAND" ]] || return 1
  [[ -f "$APP_DIR/$CONFIG_FILE_NAME" ]] || return 1
  return 0
}

# ─── MAIN EXECUTION ─────────────────────────────────────────────────────────
# High-level narrative: parse args → detect platform → resolve paths → detect
# existing install → acquire lock → render banner → (if already installed and
# same version: skip) → resolve Python → resolve source → stage → commit →
# install commands → bridge PATH → repair shell PATH → validate → manifest →
# finalize → progress done → summary.

plan_steps() {
  TOTAL_STEPS=14
  if [[ "$DRY_RUN" == 1 ]]; then
    TOTAL_STEPS=7
  fi
}

main() {
  parse_args "$@"
  setup_colors
  detect_platform
  SCRIPT_DIR="$(script_directory)"
  setup_paths
  detect_existing_install

  # If the same version is already installed and intact, short-circuit.
  if should_skip_install; then
    if [[ "$QUIET" == 0 ]]; then
      printf '%sAlready installed (%s) — nothing to do.%s\n' "$COLOR_INFO" "$EXISTING_INSTALL_VERSION" "$RESET"
      printf '%sRe-run with --force to reinstall.%s\n' "$DIM" "$RESET"
    fi
    return 0
  fi

  if [[ "$DRY_RUN" == 0 ]]; then
    acquire_install_lock
  fi
  create_temporary_workspace
  plan_steps
  render_banner

  run_step 'resolve Python runtime' ensure_python
  run_step 'ensure clipboard integration' ensure_clipboard_integration

  run_step 'resolve source program' resolve_source
  run_step 'validate source program' validate_source_program

  run_step 'copy source into managed stage' copy_source_to_stage
  run_step 'write staged command wrappers' write_stage_commands
  run_step 'validate staged runtime' validate_staged_runtime

  if [[ "$DRY_RUN" == 0 ]]; then
    run_step 'commit managed runtime' commit_runtime
    run_step 'install command wrappers' install_command_files
    run_step 'create active PATH bridge' create_active_command_bridge
    run_step 'repair shell PATH' repair_shell_path

    run_step 'validate installed commands' validate_installed_commands
    run_step 'write install manifest' write_manifest
    run_step 'finalize rollback state' finalize_install
    PROGRESS_FINAL_LABEL='install complete'
    progress_finalize 'done'
    render_summary
  else
    # The staged runtime was already validated in the common section above;
    # dry-run stops here without touching the live filesystem.
    PROGRESS_FINAL_LABEL='dry-run complete'
    progress_finalize 'done'
  fi
}

main "$@"

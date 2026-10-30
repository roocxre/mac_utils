#!/bin/bash
# Installs Mac Utils and the components you pick: a pinned uv and Python, the virtualenv, the
# components' packages from uv.lock, their models and setup steps (manage.py setup), then their
# launchd agents and the install record (manage.py add). Run it again to add components or to update;
# what's installed stays installed. The setup app runs it with --payload, --json and --yes.
#
#   ./install.sh [--components id,id|all] [--payload DIR] [--uv PATH] [--python PATH]
#                [--json] [--yes] [--no-agents]
#
#   --components  what to install (./manage.py components lists the ids); what they require comes too.
#                 Without it: what's installed already (an update), or it asks.
#   --payload     copy this code tree to ~/Library/Application Support/MacUtils/app and install there,
#                 keeping the .venv, models/, bin/ and brokers/registry/ already there
#   --uv          use this uv instead of the pinned one (found on PATH, or downloaded and checked)
#   --python      use this Python 3.14 instead of `uv python install` (development and tests)
#   --json        print one JSON object per line on stdout: step, progress and finished events
#   --yes         don't ask before going ahead
#   --no-agents   record the components, but don't load their launchd agents
#
# MACUTILS_NO_LAUNCHD=1 writes the agents' plists without calling launchctl (tests).
set -euo pipefail

UV_VERSION=0.12.24          # released 2026-10-08
UV_SHA256=0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732   # uv-aarch64-apple-darwin.tar.gz
UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz"
PYTHON_VERSION=3.14.8       # a CPython build that uv $UV_VERSION pins

# The same folders as lib/identity.py.
SUPPORT_DIR="$HOME/Library/Application Support/MacUtils"
INSTALL_DIR="$SUPPORT_DIR/app"
LOG_DIR="$HOME/Library/Logs/MacUtils"
LOG="$LOG_DIR/install.log"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P)
ARGS="$*"

COMPONENTS=
PAYLOAD=
UV_ARG=
PYTHON_ARG=
JSON=0
YES=0
AGENTS=1

usage() { sed -n '7,19p' "$0" | sed 's/^# \{0,1\}//'; }

need_value() {
    if [ "$2" -lt 2 ]; then
        echo "$1 needs a value" >&2
        exit 2
    fi
}

while [ $# -gt 0 ]; do
    case $1 in
        --components) need_value "$1" $#; COMPONENTS=$2; shift 2 ;;
        --components=*) COMPONENTS=${1#*=}; shift ;;
        --payload) need_value "$1" $#; PAYLOAD=$2; shift 2 ;;
        --payload=*) PAYLOAD=${1#*=}; shift ;;
        --uv) need_value "$1" $#; UV_ARG=$2; shift 2 ;;
        --uv=*) UV_ARG=${1#*=}; shift ;;
        --python) need_value "$1" $#; PYTHON_ARG=$2; shift 2 ;;
        --python=*) PYTHON_ARG=${1#*=}; shift ;;
        --json) JSON=1; shift ;;
        --yes|-y) YES=1; shift ;;
        --no-agents) AGENTS=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# ------------------------------------------------------------------ output

esc() {   # the text as the inside of a JSON string
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\t'/ }
    s=${s//$'\r'/ }
    s=${s//$'\n'/\\n}
    printf '%s' "$s"
}

step() {  # step ID STATUS TITLE [DETAIL]
    local id=$1 status=$2 title=$3 detail=${4-}
    if [ "$JSON" = 1 ]; then
        printf '{"event":"step","id":"%s","title":"%s","status":"%s","detail":"%s"}\n' \
            "$id" "$(esc "$title")" "$status" "$(esc "$detail")"
        return 0
    fi
    case $status in
        start) printf '==> %s\n' "$title" ;;
        failed) printf '    FAILED: %s\n' "$detail" >&2 ;;
        skipped) printf '    skipped%s\n' "${detail:+: $detail}" ;;
        *) if [ -n "$detail" ]; then printf '    %s\n' "$detail"; fi ;;
    esac
}

FINISHED=0
finish() {  # finish true|false MESSAGE
    FINISHED=1
    if [ "$JSON" = 1 ]; then
        printf '{"event":"finished","ok":%s,"message":"%s"}\n' "$1" "$(esc "$2")"
    else
        printf '\n%s\n' "$2"
    fi
}

fail() {  # fail ID TITLE DETAIL: report the step as failed, finish, exit 1
    step "$1" failed "$2" "$3"
    finish false "$2: $3"
    exit 1
}

LAST_LINE=?
trap 'LAST_LINE=$LINENO' ERR
trap 'if [ "$FINISHED" = 0 ]; then finish false "stopped unexpectedly (install.sh line $LAST_LINE; see $LOG)"; fi' EXIT

mkdir -p "$LOG_DIR"
printf '\n==== %s install.sh %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$ARGS" >>"$LOG"

quietly() {  # run a command: its output goes to the log with --json, else to the terminal
    printf '$ %s\n' "$*" >>"$LOG"
    if [ "$JSON" = 1 ]; then "$@" >>"$LOG" 2>&1; else "$@"; fi
}

why() {  # what to say about a command that failed: the end of what it wrote to the log
    if [ "$JSON" = 1 ]; then
        awk '/^(\$ |==== )/ { n = 0; next } { line[n++ % 4] = $0 } END { for (i = n - 4; i < n; i++) if (i >= 0) printf "%s ", line[i % 4] }' "$LOG" \
            | tr -d '\000-\010\013\014\016-\037' | cut -c1-400
    else
        printf 'see above; the log is %s' "$LOG"
    fi
}

relay() {  # run manage.py; with --json pass its events on, all but its own "finished"
    if [ "$JSON" = 0 ]; then
        "$@"
        return
    fi
    local rc
    printf '$ %s --json\n' "$*" >>"$LOG"
    set +e
    "$@" --json 2>>"$LOG" | while IFS= read -r line; do
        case $line in
            *'"event": "finished"'*|*'"event":"finished"'*) printf '%s\n' "$line" >>"$LOG" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done
    rc=${PIPESTATUS[0]}
    set -e
    return "$rc"
}

ask() {  # ask QUESTION: the answer, or nothing when there's no one to ask
    local answer=
    if [ "$JSON" = 0 ] && [ "$YES" = 0 ] && [ -t 0 ]; then
        read -r -p "$1" answer || true
    fi
    printf '%s' "$answer"
}

# ------------------------------------------------------------------ 0. this Mac, and where the code comes from

TITLE="Check this Mac"
step check start "$TITLE"
if [ "$(id -u)" = 0 ]; then
    fail check "$TITLE" "run it as yourself, not with sudo"
fi
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != 1 ]; then
    fail check "$TITLE" "Mac Utils needs a Mac with Apple silicon"
fi
MACOS=$(sw_vers -productVersion)
if [ "${MACOS%%.*}" -lt 14 ]; then
    fail check "$TITLE" "Mac Utils needs macOS 14 or later; this is $MACOS"
fi
case $COMPONENTS in
    *[!a-z0-9,-]*) fail check "$TITLE" "component ids are lowercase words separated by commas: $COMPONENTS" ;;
esac

COPY=0
if [ -n "$PAYLOAD" ]; then
    SRC=$(cd "$PAYLOAD" 2>/dev/null && pwd -P) || fail check "$TITLE" "no folder at $PAYLOAD"
    ROOT=$INSTALL_DIR
    if [ "$SRC" != "$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P || true)" ]; then
        COPY=1
    fi
else
    SRC=$SCRIPT_DIR
    ROOT=$SCRIPT_DIR
fi
for file in manage.py components.json models.lock.json pyproject.toml uv.lock lib/identity.py; do
    if [ ! -f "$SRC/$file" ]; then
        fail check "$TITLE" "$SRC isn't a Mac Utils code tree (no $file)"
    fi
done
step check done "$TITLE" "macOS $MACOS; installing into $ROOT"

# ------------------------------------------------------------------ 1. uv

uv_version() { "$1" --version 2>/dev/null | awk 'NR == 1 { print $2 }'; }

download_uv() {
    local tmp sum
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/macutils-uv.XXXXXX")
    if ! quietly curl --fail --silent --show-error --location --retry 3 --output "$tmp/uv.tar.gz" "$UV_URL"; then
        rm -rf "$tmp"
        fail uv "$TITLE" "couldn't download $UV_URL: $(why)"
    fi
    sum=$(shasum -a 256 "$tmp/uv.tar.gz" | awk '{ print $1 }')
    if [ "$sum" != "$UV_SHA256" ]; then
        rm -rf "$tmp"
        fail uv "$TITLE" "the download's SHA-256 is $sum, not the pinned $UV_SHA256"
    fi
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    keep_uv "$tmp/uv-aarch64-apple-darwin/uv"
    rm -rf "$tmp"
}

keep_uv() {  # copy a uv to tools/uv without its attributes (-X: no quarantine), and use that copy
    mkdir -p "$SUPPORT_DIR/tools"
    cp -X "$1" "$KEPT_UV.new"
    chmod 755 "$KEPT_UV.new"
    mv -f "$KEPT_UV.new" "$KEPT_UV"
    UV=$KEPT_UV
}

TITLE="Get uv $UV_VERSION"
step uv start "$TITLE"
KEPT_UV="$SUPPORT_DIR/tools/uv"
ON_PATH=$(command -v uv 2>/dev/null || true)
if [ -n "$UV_ARG" ]; then
    if [ ! -x "$UV_ARG" ]; then
        fail uv "$TITLE" "$UV_ARG isn't a program"
    fi
    UV=$UV_ARG
    # One from an app bundle (or still quarantined) may not be allowed to run where it is: run a copy.
    case $UV_ARG in
        *.app/Contents/*) keep_uv "$UV_ARG" ;;
        *) if xattr -p com.apple.quarantine "$UV_ARG" >/dev/null 2>&1; then keep_uv "$UV_ARG"; fi ;;
    esac
    FOUND=$(uv_version "$UV" || true)
    if [ "$FOUND" = "$UV_VERSION" ]; then
        step uv done "$TITLE" "using $UV"
    else
        step uv done "$TITLE" "using $UV (uv ${FOUND:-?}, not the pinned $UV_VERSION)"
    fi
elif [ -n "$ON_PATH" ] && [ "$(uv_version "$ON_PATH")" = "$UV_VERSION" ]; then
    UV=$ON_PATH
    step uv done "$TITLE" "using $UV"
elif [ -x "$KEPT_UV" ] && [ "$(uv_version "$KEPT_UV")" = "$UV_VERSION" ]; then
    UV=$KEPT_UV
    step uv done "$TITLE" "using $UV"
else
    download_uv
    step uv done "$TITLE" "downloaded and checked: $UV"
fi

# Python, the package cache and the virtualenv stay under Mac Utils' own folders.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$SUPPORT_DIR/python}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$SUPPORT_DIR/cache/uv}"
unset VIRTUAL_ENV UV_PROJECT_ENVIRONMENT

# ------------------------------------------------------------------ 2. Python

python_version() { "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null; }

TITLE="Get Python $PYTHON_VERSION"
step python start "$TITLE"
if [ -n "$PYTHON_ARG" ]; then
    HAVE=$(python_version "$PYTHON_ARG" || true)
    case $HAVE in
        3.14.*) ;;
        *) fail python "$TITLE" "$PYTHON_ARG is Python ${HAVE:-?}, not 3.14" ;;
    esac
    BASE_PY=$PYTHON_ARG
    step python done "$TITLE" "using $BASE_PY (Python $HAVE)"
else
    quietly "$UV" python install "$PYTHON_VERSION" --no-bin || fail python "$TITLE" "$(why)"
    BASE_PY=$("$UV" python find "$PYTHON_VERSION" --managed-python --no-project 2>>"$LOG") \
        || fail python "$TITLE" "uv can't find the Python it installed: $(why)"
    step python done "$TITLE" "$BASE_PY"
fi

# ------------------------------------------------------------------ what to install

TITLE="Check what's installed"
step plan start "$TITLE"
plan() {  # read manage.py's plan, run from the source tree (-B: never write into it, e.g. the app bundle)
    local out key value
    printf '$ manage.py plan %s\n' "$1" >>"$LOG"
    out=$("$BASE_PY" -B "$SRC/manage.py" plan $(printf '%s' "$1" | tr ',' ' ') 2>>"$LOG") \
        || fail plan "$TITLE" "$(why)"
    REQUESTED= NEW= REINSTALL= EXTRAS= REFRESH=
    while IFS='=' read -r key value; do
        case $key in
            requested) REQUESTED=$value ;;
            new) NEW=$value ;;
            reinstall) REINSTALL=$value ;;
            extras) EXTRAS=$value ;;
            refresh) REFRESH=$value ;;
        esac
    done <<EOF
$out
EOF
}
plan "$COMPONENTS"
if [ -z "$REQUESTED" ]; then
    # Nothing asked for and nothing installed yet.
    if [ "$JSON" = 0 ] && [ "$YES" = 0 ] && [ -t 0 ]; then
        "$BASE_PY" -B "$SRC/manage.py" components
        COMPONENTS=$(ask "Components to install (ids separated by commas, or all): ")
    fi
    case $COMPONENTS in
        "") fail plan "$TITLE" "nothing is installed yet: say which components with --components id,id (or all)" ;;
        *[!a-z0-9,-]*) fail plan "$TITLE" "component ids are lowercase words separated by commas: $COMPONENTS" ;;
    esac
    plan "$COMPONENTS"
fi
step plan done "$TITLE" "new: ${NEW:-none}; installed already, set up again: ${REINSTALL:-none}"
if [ "$JSON" = 0 ] && [ "$YES" = 0 ] && [ -t 0 ]; then
    case $(ask "Go ahead? [Y/n] ") in
        n*|N*) FINISHED=1; echo "Nothing changed."; exit 1 ;;
    esac
fi

# ------------------------------------------------------------------ the code

if [ "$COPY" = 1 ]; then
    TITLE="Copy the code"
    step copy start "$TITLE"
    mkdir -p "$INSTALL_DIR"
    # --delete replaces the code but never what these patterns match: they're kept as they are.
    quietly rsync -a --delete \
        --exclude=/.venv --exclude=/models --exclude=/bin --exclude=/brokers/registry --exclude=/.git \
        --exclude=/dataflow/extension/icons --exclude=__pycache__ --exclude=.DS_Store \
        "$SRC/" "$INSTALL_DIR/" || fail copy "$TITLE" "$(why)"
    if [ -f "$SRC/bin/ocr-helper" ]; then
        mkdir -p "$INSTALL_DIR/bin"
        cp -X "$SRC/bin/ocr-helper" "$INSTALL_DIR/bin/ocr-helper.new"   # -X: no quarantine or other attributes
        chmod 755 "$INSTALL_DIR/bin/ocr-helper.new"
        mv -f "$INSTALL_DIR/bin/ocr-helper.new" "$INSTALL_DIR/bin/ocr-helper"
    fi
    # Setup writes into the code (the extension's key, its icons), so it must be writable.
    find "$INSTALL_DIR" -path "$INSTALL_DIR/.venv" -prune -o ! -perm -u+w -exec chmod u+w {} +
    step copy done "$TITLE" "$SRC -> $INSTALL_DIR"
fi
cd "$ROOT"
VPY="$ROOT/.venv/bin/python"

# ------------------------------------------------------------------ 3. the virtualenv

TITLE="Make the virtualenv"
step venv start "$TITLE"
WANT=$(python_version "$BASE_PY" || true)
HAVE=
if [ -x "$VPY" ]; then
    HAVE=$(python_version "$VPY" || true)
fi
if [ -n "$HAVE" ] && [ "$HAVE" = "$WANT" ]; then
    step venv skipped "$TITLE" "kept .venv (Python $HAVE)"
else
    quietly env UV_PYTHON_DOWNLOADS=never "$UV" venv --clear --python "$BASE_PY" .venv \
        || fail venv "$TITLE" "$(why)"
    step venv done "$TITLE" "$ROOT/.venv (Python $WANT)"
fi

# ------------------------------------------------------------------ 4. the packages

TITLE="Install the Python packages"
step deps start "$TITLE"
EXTRA_ARGS=
for extra in $EXTRAS; do
    EXTRA_ARGS="$EXTRA_ARGS --extra $extra"
done
# --frozen: exactly what uv.lock says. The extras are those of every installed component, not just
# the new ones, since sync removes packages no extra asks for.
quietly env UV_PYTHON_DOWNLOADS=never "$UV" sync --frozen $EXTRA_ARGS || fail deps "$TITLE" "$(why)"
step deps done "$TITLE" "the core${EXTRAS:+ and $EXTRAS}"

# ------------------------------------------------------------------ 5. setup steps

TITLE="Set up the components"
step setup start "$TITLE"
relay "$VPY" "$ROOT/manage.py" setup $REQUESTED || fail setup "$TITLE" "$(why)"
# New code drops what some setup steps wrote into it (the extension's key): redo those.
for item in $REFRESH; do
    relay "$VPY" "$ROOT/manage.py" setup "${item%%:*}" --only "${item#*:}" || fail setup "$TITLE" "$(why)"
done
step setup done "$TITLE"

# ------------------------------------------------------------------ 6. agents and the record

TITLE="Load the agents"
NO_AGENTS=
if [ "$AGENTS" = 0 ]; then
    TITLE="Record the components"
    NO_AGENTS=--no-agents
fi
step agents start "$TITLE"
relay "$VPY" "$ROOT/manage.py" add $REQUESTED $NO_AGENTS || fail agents "$TITLE" "$(why)"
step agents done "$TITLE"

if [ "$JSON" = 1 ]; then
    finish true "installed $REQUESTED"
else
    finish true "Installed: $REQUESTED
See what's installed:  \"$VPY\" \"$ROOT/manage.py\" components
Check on it:           \"$VPY\" \"$ROOT/manage.py\" doctor"
fi

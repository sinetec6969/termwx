#!/bin/sh
# termwx installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/sinetec6969/termwx/HEAD/install.sh | sh
#   ./install.sh [--no-setup] [--ref <branch|tag>]
#   ./install.sh --uninstall [--purge]
#
# Installs termwx into its own virtual environment, links the `termwx` command
# into ~/.local/bin, makes sure that directory is on your PATH, and runs
# `termwx setup` to pick your location and airport. Run it again to upgrade.
#
# Environment overrides:
#   TERMWX_HOME     where the virtual environment lives (default ~/.local/share/termwx)
#   TERMWX_BIN_DIR  where the `termwx` command is linked (default ~/.local/bin)
set -eu

REPO="sinetec6969/termwx"
REF="HEAD"  # the repository's default branch
REF_SET=0
SETUP=1
ACTION=install
PURGE=0
TERMWX_HOME="${TERMWX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/termwx}"
BIN_DIR="${TERMWX_BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/termwx"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/termwx"
MARK_BEGIN="# >>> termwx >>>"
MARK_END="# <<< termwx <<<"

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m'); BLUE=$(printf '\033[1;34m'); YELLOW=$(printf '\033[1;33m')
    RED=$(printf '\033[1;31m'); GREEN=$(printf '\033[1;32m'); RESET=$(printf '\033[0m')
else
    BOLD=""; BLUE=""; YELLOW=""; RED=""; GREEN=""; RESET=""
fi

say() { printf '%s==>%s %s\n' "$BLUE" "$RESET" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

usage() {
    cat <<EOF
termwx installer

usage: install.sh [--no-setup] [--ref <branch|tag>]
       install.sh --uninstall [--purge]

  --no-setup     don't run 'termwx setup' after installing
  --ref REF      install this branch or tag from GitHub (default: the default branch)
  --uninstall    remove termwx, its command link and the PATH lines it added
  --purge        with --uninstall: also delete settings and cached data

Installs into $TERMWX_HOME and links $BIN_DIR/termwx.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-setup) SETUP=0 ;;
        --ref) [ $# -ge 2 ] || die "--ref needs a branch or tag"; REF=$2; REF_SET=1; shift ;;
        --ref=*) REF=${1#--ref=}; REF_SET=1 ;;
        --uninstall) ACTION=uninstall ;;
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

# Startup files that get the PATH line, for the user's login shell.
rc_files() {
    case "$(basename "${SHELL:-sh}")" in
        zsh) echo "${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash)
            echo "$HOME/.bashrc"
            if [ "$(uname -s)" = Darwin ]; then echo "$HOME/.bash_profile"; fi
            ;;
        fish) echo "${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/termwx.fish" ;;
        *) echo "$HOME/.profile" ;;
    esac
}

# Every file a previous install could have touched, whatever the shell is now.
all_rc_files() {
    echo "${ZDOTDIR:-$HOME}/.zshrc"
    echo "$HOME/.bashrc"
    echo "$HOME/.bash_profile"
    echo "$HOME/.profile"
    echo "${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/termwx.fish"
}

path_block() {
    case "$1" in
        *.fish) printf 'contains -- "%s" $PATH; or set -gx PATH "%s" $PATH\n' "$BIN_DIR" "$BIN_DIR" ;;
        *) printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac\n' "$BIN_DIR" "$BIN_DIR" ;;
    esac
}

add_block() {
    file=$1
    if [ -f "$file" ] && grep -qF "$MARK_BEGIN" "$file"; then
        return 1
    fi
    mkdir -p "$(dirname "$file")"
    if [ -s "$file" ]; then printf '\n' >>"$file"; fi
    {
        printf '%s\n' "$MARK_BEGIN"
        path_block "$file"
        printf '%s\n' "$MARK_END"
    } >>"$file"
}

remove_block() {
    file=$1
    [ -f "$file" ] && grep -qF "$MARK_BEGIN" "$file" || return 1
    tmp="${file}.termwx-tmp"
    # Drop the block and the blank separator line add_block put before it.
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        skip { if ($0 == e) skip = 0; next }
        $0 == b { blank = 0; skip = 1; next }
        { if (blank) print ""; blank = 0 }
        $0 == "" { blank = 1; next }
        { print }
        END { if (blank) print "" }' "$file" >"$tmp"
    cat "$tmp" >"$file"  # keeps the file's permissions and any symlink
    rm -f "$tmp"
    case "$file" in
        */conf.d/termwx.fish) if ! grep -q '[^[:space:]]' "$file"; then rm -f "$file"; fi ;;
    esac
}

uninstall() {
    case "$TERMWX_HOME" in
        ""|/|"$HOME"|"$HOME/") die "refusing to delete TERMWX_HOME=$TERMWX_HOME" ;;
    esac
    if [ -L "$BIN_DIR/termwx" ]; then
        rm -f "$BIN_DIR/termwx"
        say "Removed $BIN_DIR/termwx"
    fi
    if [ -d "$TERMWX_HOME" ]; then
        rm -rf "$TERMWX_HOME"
        say "Removed $TERMWX_HOME"
    fi
    all_rc_files | while IFS= read -r f; do
        if remove_block "$f"; then say "Removed the PATH lines from $f"; fi
    done
    if [ "$PURGE" = 1 ]; then
        rm -rf "$CONFIG_DIR" "$CACHE_DIR"
        say "Removed settings ($CONFIG_DIR) and cache ($CACHE_DIR)"
    elif [ -d "$CONFIG_DIR" ]; then
        say "Kept your settings in $CONFIG_DIR (use --purge to delete them)"
    fi
    printf '%stermwx uninstalled.%s\n' "$GREEN" "$RESET"
}

find_python() {
    for cand in python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1 &&
            "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

install() {
    PY=$(find_python) || die "termwx needs Python 3.11 or newer.
  Debian/Ubuntu: sudo apt install python3 python3-venv
  Fedora:        sudo dnf install python3
  macOS:         brew install python   (or https://www.python.org/downloads/)"
    if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
        die "$PY can't create virtual environments.
  Debian/Ubuntu: sudo apt install python3-venv   (or python$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')-venv)"
    fi
    say "Using $("$PY" --version 2>&1) ($(command -v "$PY"))"

    # Install from this clone when the script sits in one, otherwise from GitHub.
    script_dir=""
    if [ -f "$0" ]; then script_dir=$(cd "$(dirname "$0")" && pwd); fi
    if [ -n "${TERMWX_SOURCE:-}" ]; then
        src=$TERMWX_SOURCE
    elif [ "$REF_SET" = 0 ] && [ -n "$script_dir" ] && grep -qs '^name = "termwx"' "$script_dir/pyproject.toml"; then
        src=$script_dir
    else
        src="https://github.com/$REPO/archive/$REF.tar.gz"
    fi
    say "Installing termwx from $src"

    venv="$TERMWX_HOME/venv"
    if [ ! -x "$venv/bin/python" ]; then
        mkdir -p "$TERMWX_HOME"
        "$PY" -m venv "$venv" || die "couldn't create a virtual environment in $venv"
    fi
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip ||
        warn "couldn't upgrade pip; continuing with the bundled one"
    # Dependencies first, then termwx itself again with --force-reinstall --no-deps so a
    # re-run always picks up new code, even when the version number hasn't changed.
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$src" ||
        die "pip couldn't install termwx (see the messages above)"
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps "$src" ||
        die "pip couldn't install termwx (see the messages above)"

    mkdir -p "$BIN_DIR"
    ln -sf "$venv/bin/termwx" "$BIN_DIR/termwx"
    say "Linked $BIN_DIR/termwx"

    on_path=0
    case ":$PATH:" in *":$BIN_DIR:"*) on_path=1 ;; esac
    if [ "$on_path" = 0 ]; then
        rc_files | while IFS= read -r f; do
            if add_block "$f"; then say "Added $BIN_DIR to PATH in $f"; else say "PATH is already set up in $f"; fi
        done
    fi
    other=$(command -v termwx 2>/dev/null || true)
    if [ "$on_path" = 1 ] && [ -n "$other" ] && [ "$other" != "$BIN_DIR/termwx" ]; then
        warn "another termwx comes first on your PATH: $other"
    fi

    if [ "$SETUP" = 1 ]; then
        if [ -f "$CONFIG_DIR/config.toml" ]; then
            say "Keeping your settings in $CONFIG_DIR/config.toml (run 'termwx setup' to change them)"
        elif (exec </dev/tty) 2>/dev/null; then
            printf '\n'
            # /dev/tty: stdin is the script itself under `curl | sh`
            "$BIN_DIR/termwx" setup </dev/tty || warn "setup didn't finish; run 'termwx setup' any time"
        else
            say "No terminal to ask questions on; run 'termwx setup' later"
        fi
    fi

    ver=$("$BIN_DIR/termwx" --version 2>/dev/null || echo termwx)
    printf '\n%s✔ %s installed.%s\n' "$GREEN" "$ver" "$RESET"
    if [ "$on_path" = 1 ]; then
        printf '  Run %stermwx%s to start, %stermwx setup%s to change settings.\n' "$BOLD" "$RESET" "$BOLD" "$RESET"
    else
        case "$(basename "${SHELL:-sh}")" in
            fish) now="set -gx PATH \"$BIN_DIR\" \$PATH" ;;
            *) now="export PATH=\"$BIN_DIR:\$PATH\"" ;;
        esac
        printf '  Open a new terminal (or run: %s%s%s), then run %stermwx%s.\n' "$BOLD" "$now" "$RESET" "$BOLD" "$RESET"
    fi
    printf '  Upgrade: run the installer again · Remove: add --uninstall (curl … | sh -s -- --uninstall)\n'
}

if [ "$ACTION" = uninstall ]; then uninstall; else install; fi

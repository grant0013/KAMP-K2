#!/usr/bin/env bash
# KAMP-K2 one-shot installer for Linux and macOS.
#
# Mirror of install.ps1 / bootstrap.ps1 for the *nix crowd. Detects Python 3,
# creates a local venv, installs paramiko into it (avoids PEP 668 conflicts
# on Ubuntu 23.04+, Debian 12+, and recent Homebrew Python), clones the repo,
# prompts for the printer IP, and runs the same install_k2.py that the
# Windows flow does.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/grant0013/KAMP-K2/main/install.sh | bash
#
# Or, if you've already cloned the repo:
#   ./install.sh
#
# Non-interactive:
#   ./install.sh --host 192.168.1.42
#   ./install.sh --host 192.168.1.42 --revert
#
# Prompts are read from /dev/tty so the curl|bash form works (stdin is the
# pipe carrying the script, not the terminal).

set -euo pipefail

REPO_URL="https://github.com/grant0013/KAMP-K2"
REPO_GIT="${REPO_URL}.git"
REPO_ZIP="${REPO_URL}/archive/refs/heads/main.zip"
INSTALL_DIR="${KAMP_K2_DIR:-$HOME/KAMP-K2}"
BACKUP_DIR="$INSTALL_DIR/backups"
VENV_DIR="$INSTALL_DIR/.venv"

# Interactive prompts come from /dev/tty regardless of how we were launched.
TTY="/dev/tty"

# ANSI colours, auto-disabled when not on a terminal or NO_COLOR is set.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_YEL=$'\033[33m'
    C_RED=$'\033[31m';  C_GRAY=$'\033[90m';  C_RESET=$'\033[0m'
else
    C_CYAN=; C_GREEN=; C_YEL=; C_RED=; C_GRAY=; C_RESET=
fi

step() { printf '%s[*] %s%s\n' "$C_CYAN"  "$1" "$C_RESET"; }
ok()   { printf '%s[+] %s%s\n' "$C_GREEN" "$1" "$C_RESET"; }
warn() { printf '%s[!] %s%s\n' "$C_YEL"   "$1" "$C_RESET"; }
err()  { printf '%s[x] %s%s\n' "$C_RED"   "$1" "$C_RESET"; }

PRINTER_HOST=""
PRINTER_PASSWORD="creality_2024"
BOARD="auto"
REVERT=0
DRYRUN=0
CLEAN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host)     PRINTER_HOST="$2"; shift 2 ;;
        --password) PRINTER_PASSWORD="$2"; shift 2 ;;
        --board)    BOARD="$2"; shift 2 ;;
        --revert)   REVERT=1; shift ;;
        --dry-run)  DRYRUN=1; shift ;;
        --clean-reinstall) CLEAN=1; shift ;;
        --help|-h)
            cat <<EOF
KAMP-K2 installer (Linux/macOS)

Usage: install.sh [options]

  --host IP              Printer IP address (prompted if omitted)
  --password PASS        SSH password (default: creality_2024)
  --board auto|F008|F021 Force board type (default: auto-detect)
                         F021 = K2 / K2 Combo / K2 Pro (single-Z)
                         F008 = K2 Plus (dual-Z)
  --revert               Uninstall KAMP-K2 and restore Creality originals
  --clean-reinstall      Revert then reinstall in one step
  --dry-run              Show what would change without modifying the printer
  --help                 This message

Environment:
  KAMP_K2_DIR            Override install directory (default: \$HOME/KAMP-K2)
  NO_COLOR               Disable ANSI colour output
EOF
            exit 0 ;;
        *) err "Unknown option: $1"; echo "Try: install.sh --help"; exit 1 ;;
    esac
done

echo
printf '%s================================%s\n' "$C_CYAN" "$C_RESET"
printf '%s KAMP-K2 installer (Linux/macOS)%s\n' "$C_CYAN" "$C_RESET"
printf '%s================================%s\n' "$C_CYAN" "$C_RESET"
echo

# --- python3 -----------------------------------------------------------------

PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
    fi
done

if [ -z "$PY" ]; then
    err "Python 3.8+ not found on PATH."
    echo
    case "$(uname -s)" in
        Darwin)
            warn "Install via Homebrew:  brew install python"
            warn "...or Xcode CLT:       xcode-select --install"
            ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                warn "Install via apt:       sudo apt-get install -y python3 python3-venv"
            elif command -v dnf >/dev/null 2>&1; then
                warn "Install via dnf:       sudo dnf install -y python3"
            elif command -v pacman >/dev/null 2>&1; then
                warn "Install via pacman:    sudo pacman -S python"
            else
                warn "Install python3 (>=3.8) via your distro's package manager."
            fi
            ;;
        *) warn "Install Python 3.8 or newer." ;;
    esac
    exit 1
fi
ok "Python found: $("$PY" --version 2>&1)"

# Debian/Ubuntu split out the venv module into a separate package.
if ! "$PY" -c 'import venv' 2>/dev/null; then
    err "Python 'venv' module missing."
    warn "On Debian/Ubuntu: sudo apt-get install -y python3-venv"
    exit 1
fi

# --- download repo -----------------------------------------------------------

download_repo() {
    step "Fetching KAMP-K2 into $INSTALL_DIR..."

    # Preserve backups (outside $INSTALL_DIR) before we wipe anything.
    local preserved=""
    if [ -d "$BACKUP_DIR" ]; then
        preserved="$(mktemp -d -t kamp-k2-backups.XXXXXX)"
        rm -rf "$preserved"
        mv "$BACKUP_DIR" "$preserved"
    fi
    rm -rf "$INSTALL_DIR"

    if command -v git >/dev/null 2>&1; then
        git clone --depth 1 --quiet "$REPO_GIT" "$INSTALL_DIR"
    else
        # Fallback: zip download. unzip is present on every current macOS and
        # most Linux distros; on minimal containers we bail with a clear hint.
        local tmpzip tmpex
        tmpzip="$(mktemp -t kamp-k2.XXXXXX.zip)"
        curl -fsSL "$REPO_ZIP" -o "$tmpzip"
        tmpex="$(mktemp -d -t kamp-k2-ex.XXXXXX)"
        if command -v unzip >/dev/null 2>&1; then
            unzip -q "$tmpzip" -d "$tmpex"
        elif command -v bsdtar >/dev/null 2>&1; then
            bsdtar -xf "$tmpzip" -C "$tmpex"
        else
            err "Neither 'git', 'unzip', nor 'bsdtar' is available."
            warn "Install one of them, or clone the repo manually:"
            warn "  git clone $REPO_GIT $INSTALL_DIR"
            exit 1
        fi
        mv "$tmpex/KAMP-K2-main" "$INSTALL_DIR"
        rm -rf "$tmpex" "$tmpzip"
    fi

    if [ -n "$preserved" ]; then
        mkdir -p "$BACKUP_DIR"
        if [ -n "$(ls -A "$preserved" 2>/dev/null)" ]; then
            mv "$preserved"/* "$BACKUP_DIR"/
        fi
        rm -rf "$preserved"
    fi
    ok "Repo ready at $INSTALL_DIR"
}

# If we're running out of an existing clone (e.g. user ran `./install.sh`
# from inside the repo) just use it in place. Detect by the presence of
# install_k2.py next to this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -n "${SCRIPT_DIR:-}" ] && [ -f "$SCRIPT_DIR/install_k2.py" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
    BACKUP_DIR="$INSTALL_DIR/backups"
    VENV_DIR="$INSTALL_DIR/.venv"
    ok "Running from existing clone at $INSTALL_DIR"
else
    download_repo
fi

# --- venv + paramiko ---------------------------------------------------------

if [ ! -x "$VENV_DIR/bin/python" ]; then
    step "Creating Python venv at $VENV_DIR..."
    "$PY" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"
# Quiet pip; upgrade is optional and we don't want a single slow mirror to
# block the install.
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
if ! "$VENV_PY" -c 'import paramiko' >/dev/null 2>&1; then
    step "Installing paramiko into venv..."
    "$VENV_PY" -m pip install --quiet paramiko
fi
PARAMIKO_VER="$("$VENV_PY" -c 'import paramiko; print(paramiko.__version__)')"
ok "paramiko $PARAMIKO_VER ready"

# --- IP prompt ---------------------------------------------------------------

if [ -z "$PRINTER_HOST" ]; then
    if [ ! -r "$TTY" ]; then
        err "No --host given and no controlling terminal for prompting."
        warn "Re-run with:  install.sh --host 192.168.x.x"
        exit 1
    fi
    echo
    printf '%sFind your printer'"'"'s IP on the touchscreen:%s\n' "$C_YEL" "$C_RESET"
    printf '%s  Settings -> Network -> IP Address (e.g. 192.168.1.170)%s\n' "$C_YEL" "$C_RESET"
    echo
    while :; do
        printf "Enter your printer's IP address: "
        read -r PRINTER_HOST <"$TTY"
        PRINTER_HOST="$(printf '%s' "$PRINTER_HOST" | tr -d '[:space:]')"
        if [[ "$PRINTER_HOST" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
            break
        fi
        warn "That doesn't look like an IPv4 address. Try again."
    done
fi

# --- installer invocation ----------------------------------------------------

run_installer() {
    mkdir -p "$BACKUP_DIR"
    local extra=("$@")
    local flags=()
    [ "$DRYRUN" -eq 1 ] && flags+=(--dry-run)
    (
        cd "$INSTALL_DIR"
        PYTHONIOENCODING=utf-8 "$VENV_PY" install_k2.py \
            --host "$PRINTER_HOST" \
            --password "$PRINTER_PASSWORD" \
            --board "$BOARD" \
            --local-backup-dir "$BACKUP_DIR" \
            "${flags[@]}" "${extra[@]}"
    )
}

if [ "$REVERT" -eq 1 ]; then
    step "Running revert against $PRINTER_HOST..."
    run_installer --revert
    exit $?
fi

if [ "$CLEAN" -eq 1 ]; then
    step "Running clean reinstall against $PRINTER_HOST..."
    run_installer --clean-reinstall
    exit $?
fi

# --- detect existing install -------------------------------------------------

step "Checking printer state at $PRINTER_HOST..."
set +e
DETECT_OUT="$(
    cd "$INSTALL_DIR" && "$VENV_PY" install_k2.py \
        --host "$PRINTER_HOST" \
        --password "$PRINTER_PASSWORD" \
        --detect 2>&1
)"
DETECT_RC=$?
set -e

STATUS="$(printf '%s\n' "$DETECT_OUT" | grep 'KAMPK2_STATUS=' | head -1 | sed 's/.*KAMPK2_STATUS=//')"
BOARD_DETECTED="$(printf '%s\n' "$DETECT_OUT" | grep 'KAMPK2_BOARD=' | head -1 | sed 's/.*KAMPK2_BOARD=//')"
STATUS="${STATUS:-unknown}"
BOARD_DETECTED="${BOARD_DETECTED:-unknown}"

if [ "$DETECT_RC" -ne 0 ] && [ "$STATUS" = "unknown" ]; then
    warn "Detect step exited with code $DETECT_RC. Output was:"
    printf '%s\n' "$DETECT_OUT"
fi

show_menu() {
    # Menu text goes to stderr so the final `echo $choice` is the only thing
    # captured by $(show_menu).
    {
        echo
        printf '%s================================================%s\n' "$C_CYAN" "$C_RESET"
        printf '%s KAMP-K2 is already installed on this printer.%s\n' "$C_CYAN" "$C_RESET"
        printf '%s Board detected: %s%s\n' "$C_CYAN" "$BOARD_DETECTED" "$C_RESET"
        printf '%s================================================%s\n' "$C_CYAN" "$C_RESET"
        echo
        echo "  [1] Update / reinstall (pulls latest from GitHub)"
        echo "  [2] Revert (restore original Creality configs, remove KAMP-K2)"
        echo "  [3] Clean reinstall (wipe everything and install fresh)"
        echo "      - recommended if previous installs left duplicates"
        echo "      - equivalent to Revert + Update in one step"
        echo "  [4] Exit without changes"
        echo
    } >&2
    local choice
    while :; do
        printf "Choose [1-4]: " >&2
        read -r choice <"$TTY"
        case "$choice" in 1|2|3|4) echo "$choice"; return;; esac
    done
}

RC=0
if [ "$STATUS" = "installed" ]; then
    CHOICE="$(show_menu)"
    case "$CHOICE" in
        1) step "Running update/reinstall against $PRINTER_HOST..."
           run_installer || RC=$? ;;
        2) step "Running revert against $PRINTER_HOST..."
           run_installer --revert || RC=$? ;;
        3) step "Running clean reinstall against $PRINTER_HOST..."
           run_installer --clean-reinstall || RC=$? ;;
        4) ok "Exited without changes."; exit 0 ;;
    esac
elif [ "$STATUS" = "fresh" ]; then
    ok "No existing install detected. Proceeding with fresh install."
    step "Running installer against $PRINTER_HOST (board=$BOARD_DETECTED)..."
    run_installer || RC=$?
else
    warn "Could not determine install state."
    if [ ! -r "$TTY" ]; then
        err "No TTY for confirmation. Re-run with --clean-reinstall or --revert."
        exit 1
    fi
    printf "Proceed with install anyway? [y/N]: "
    read -r go <"$TTY"
    [ "$go" = "y" ] || { ok "Exited without changes."; exit 0; }
    run_installer || RC=$?
fi

echo
printf '%s================================================================%s\n' "$C_CYAN" "$C_RESET"
if [ "$RC" -eq 0 ]; then
    ok "KAMP-K2 install complete."
    echo
    echo "Next step:"
    echo "  Slice a test print in Orca/Prusa/etc. with 'exclude_object'"
    echo "  enabled, send it to your printer via the slicer (NOT via USB"
    echo "  drive or touchscreen), and watch the console for the"
    echo "  'Adapted probe count' line during bed meshing."
    echo
    printf '%sLocal backups kept at: %s%s\n' "$C_GRAY" "$BACKUP_DIR" "$C_RESET"
    printf '%sThese survive printer firmware updates. Keep them safe.%s\n' "$C_GRAY" "$C_RESET"
    echo
    printf '%sTo revert later:  %s/install.sh --host %s --revert%s\n' \
        "$C_GRAY" "$INSTALL_DIR" "$PRINTER_HOST" "$C_RESET"
else
    err "KAMP-K2 install FAILED (exit code $RC)."
    echo
    warn "Scroll up and read the messages above -- look for any"
    warn "line starting with [x] or [!]. If you need help, open an"
    warn "issue and paste the full terminal output:"
    warn "  https://github.com/grant0013/KAMP-K2/issues"
fi
printf '%s================================================================%s\n' "$C_CYAN" "$C_RESET"
echo

exit "$RC"

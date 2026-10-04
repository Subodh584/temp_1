#!/usr/bin/env bash
# LiteView one-command installer for Linux (X11) and macOS:
#
#   curl -fsSL https://raw.githubusercontent.com/Subodh584/temp_1/main/install.sh | bash
#
# Downloads LiteView, installs its Python packages, installs Tailscale and signs
# this computer in, makes LiteView start at login, and starts it now.
# Re-run it to update.
set -euo pipefail

REPO=Subodh584/temp_1
DIR="${LITEVIEW_DIR:-$HOME/.local/share/liteview}"
LOG="$HOME/.liteview.log"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[31mLiteView install failed: %s\033[0m\n' "$*" >&2; exit 1; }

# Prints Tailscale's state: Running, NeedsLogin, Stopped, ... or nothing if unavailable.
ts_state() {
  tailscale status --json 2>/dev/null |
    python3 -c 'import json, sys; print(json.load(sys.stdin).get("BackendState", ""))' 2>/dev/null || true
}

setup_tailscale() {
  if [ "$(uname)" = Darwin ]; then
    if ! command -v tailscale >/dev/null; then
      warn "Tailscale is not installed. For access over the internet, install it from"
      warn "https://tailscale.com/download/mac, sign in, and re-run this command."
      return
    fi
  elif ! command -v tailscale >/dev/null; then
    say "Installing Tailscale (you may be asked for your sudo password)..."
    curl -fsSL https://tailscale.com/install.sh | sh ||
      { warn "Installing Tailscale failed; LiteView will only work on this local network."; return; }
  fi
  if [ "$(uname)" = Linux ] && command -v systemctl >/dev/null; then
    sudo systemctl enable --now tailscaled >/dev/null 2>&1 || true
  fi

  if [ "$(ts_state)" != Running ]; then
    say "Connecting this computer to Tailscale..."
    warn "If a link appears below, open it and sign in with the SAME Tailscale account"
    warn "you use on the other computer. The install continues once you have signed in."
    if [ "$(uname)" = Darwin ]; then tailscale up || true; else sudo tailscale up || true; fi
  fi
  if [ "$(ts_state)" = Running ]; then
    TS_FLAG="--tailscale-only"
  else
    warn "Tailscale is not connected, so LiteView will only work on this local network."
    warn "Re-run this command after signing in to Tailscale to enable access over the internet."
  fi
}

main() {
command -v python3 >/dev/null || die "python3 is not installed."
command -v curl >/dev/null || die "curl is not installed."
if [ "$(uname)" = Linux ] && [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
  warn "This is a Wayland session: LiteView can't capture the screen or control input here."
  warn "Log out and pick an X11/Xorg session on the login screen, then re-run this command."
fi

# Stop a running copy so its files can be replaced.
pkill -f "$DIR/host.py" 2>/dev/null || true

say "Downloading LiteView to $DIR ..."
mkdir -p "$DIR"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$DIR" --strip-components=1

say "Installing Python packages (first time takes a minute)..."
if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv" || die "creating a virtualenv failed. On Debian/Ubuntu run: sudo apt install python3-venv"
fi
"$DIR/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$DIR/requirements.txt"

TS_FLAG=""
setup_tailscale

PY="$DIR/.venv/bin/python"
if [ "$(uname)" = Darwin ]; then
  say "Making LiteView start automatically when you log in..."
  PLIST="$HOME/Library/LaunchAgents/com.liteview.host.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.liteview.host</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DIR/host.py</string>${TS_FLAG:+<string>$TS_FLAG</string>}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
  say "Starting LiteView in the background..."
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  warn "macOS will ask for Screen Recording and Accessibility permission for Python. Allow both,"
  warn "then re-run this command so LiteView restarts with the permissions."
else
  say "Making LiteView start automatically when you log in..."
  mkdir -p "$HOME/.config/autostart"
  cat >"$HOME/.config/autostart/liteview.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=LiteView
Exec=sh -c '"$PY" "$DIR/host.py" $TS_FLAG >>"$LOG" 2>&1'
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
  say "Starting LiteView in the background..."
  nohup "$PY" "$DIR/host.py" $TS_FLAG >>"$LOG" 2>&1 </dev/null &
  disown
fi

sleep 4
if ! pgrep -f "$DIR/host.py" >/dev/null; then
  warn "LiteView stopped right after starting. Last lines of $LOG:"
  tail -n 15 "$LOG" >&2 || true
  exit 1
fi

echo
printf '\033[1;32mLiteView is running. On the other computer, open:\033[0m\n'
"$PY" "$DIR/host.py" --show-address $TS_FLAG
echo
echo "It starts automatically at every login. Log file: $LOG"
echo "To stop it: pkill -f '$DIR/host.py'"
}

# Everything runs from main so bash has read the whole script before anything
# (like sudo) runs; this matters for `curl ... | bash`.
main "$@"

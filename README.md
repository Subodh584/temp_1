# LiteView

A tiny two-computer remote desktop tool.

- **Host** (the computer being controlled) runs `host.py`.
- **Controller** opens `http://<host-ip>:8765` in any browser. It needs nothing installed.

## Install on the host (one command)

On the computer you want to control, run:

**Windows** (PowerShell):
```powershell
irm https://raw.githubusercontent.com/Subodh584/temp_1/main/install.ps1 | iex
```

**Linux (X11) / macOS** (terminal):
```bash
curl -fsSL https://raw.githubusercontent.com/Subodh584/temp_1/main/install.sh | bash
```

This one command:

- downloads LiteView (on Windows it also installs Python if needed)
- installs Tailscale if needed (except on macOS) and, if this computer isn't signed in yet, shows a
  sign-in link. Sign in with the **same account** you use on the controlling computer.
- opens the firewall port (Windows asks for permission once)
- makes LiteView start automatically at login, then starts it right away
- prints the address and password to use from the other computer

Once Tailscale is connected, the command sets LiteView to accept connections only through Tailscale.
Run the same command again to update LiteView. The output log is saved in `~/.liteview.log`.

## Run the host manually

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip install -r requirements.txt
.venv/bin/python host.py                       # Windows: .venv\Scripts\python host.py
```

The host prints the URL and password. The password is generated once and saved in
`~/.liteview_password`, so it stays the same between runs. Override it with
`--password` or the `LITEVIEW_PASSWORD` env var.

Options: `--port`, `--fps`, `--quality`, `--max-width`, `--view-only`, `--tailscale-only`, and the optional `--module thirdeye` flag to enable the bundled ThirdEye module adapter.

### Optional module integration

The repository also includes a lightweight `module_2` adapter for a ThirdEye-style Python integration. It is disabled by default but can be enabled on the host with:

```bash
python host.py --module thirdeye
```

This exposes `/module/third-eye`, `/module/third-eye/health`, and `/module/third-eye/info` without changing the normal LiteView remote-desktop behavior.

## Connect over the internet (Tailscale)

LiteView uses [Tailscale](https://tailscale.com) (free for personal use) to connect over the internet.
Tailscale gives the host a fixed private address that works from any network, even behind
college Wi-Fi or mobile hotspots. The connection is encrypted, and you don't need to change any router settings.

1. Install Tailscale on **both** computers and sign in with the **same account**.
2. On the host, run:
   ```bash
   python host.py --tailscale-only
   ```
   It prints `From anywhere (Tailscale): http://100.x.y.z:8765`. This address stays the same.
3. On the controller, open that address in a browser from anywhere and enter the password.

`--tailscale-only` makes the host reachable **only** through Tailscale, so nobody else on the
host's Wi-Fi can reach the port. If Tailscale isn't connected yet (for example, just after boot), the
host waits for it to connect before starting. Bookmark the address. You can also use the host's
MagicDNS name, such as `http://my-laptop:8765`.

### If the Tailscale address doesn't connect

1. **Both** computers must be logged in to the same Tailscale account. On Linux, run
   `sudo tailscale up`, and `tailscale status` should list both computers.
2. From the controller, run `tailscale ping <host-tailscale-ip>`. If that fails, the problem is
   Tailscale itself, not LiteView.
3. **Windows firewall** (most common): Windows treats the Tailscale adapter as a *Public*
   network, so the rule it created for Python on your home Wi-Fi doesn't cover it. In an
   **admin** PowerShell on the host, run:
   ```powershell
   netsh advfirewall firewall add rule name="LiteView (Tailscale)" dir=in action=allow protocol=TCP localport=8765 remoteip=100.64.0.0/10
   ```
   This opens the port only to Tailscale addresses.

Only one controller can be connected at a time. A new login takes over from the previous session. If the connection drops, the viewer reconnects automatically.

## Supported hosts

| Host OS | Works? |
|---|---|
| Windows | Yes |
| Linux on X11 (Xorg) | Yes |
| Linux on Wayland (default on Fedora/Ubuntu GNOME) | No. Screen capture comes back black and input injection is blocked. |
| macOS | Yes, after granting Screen Recording and Accessibility permissions to the terminal/Python. |

The controller can run any OS with a modern browser.

## Start automatically ("always connectable")

The one-command installer already sets this up. To set it up by hand:

- **Windows:** press Win+R, run `shell:startup`, and put a shortcut there to
  `C:\path\to\.venv\Scripts\pythonw.exe C:\path\to\host.py --tailscale-only`.
  Also turn on Tailscale's "Run unattended" option so it connects at startup
  (Tailscale tray icon → Preferences → Run unattended).
- **Linux (X11):** add a `.desktop` file to `~/.config/autostart/` that runs the same command.

If you connect without Tailscale on the local network, allow TCP port 8765 through the host's firewall.

## Notes

- LiteView itself doesn't encrypt traffic; Tailscale does. Don't port-forward 8765 to the open internet.
- Click **Fullscreen** (Chrome/Edge) to capture keys like Alt+Tab and the Windows key.
  Ctrl+Alt+Del can't be sent.

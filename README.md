# PC Remote

Use your phone as a trackpad and keyboard for a Windows PC across the room. It's a small Python
server plus one web page, reached over your [Tailscale](https://tailscale.com) network. Nothing
to install on the phone, and there's no screen streaming: you watch the PC's own display.

- **Trackpad:** drag to move, tap to click, two-finger tap to right-click, two-finger drag to
  scroll (with momentum). The strip on the right edge scrolls with one finger.
- **Mouse buttons:** Left / Mid / Right can be held while you drag on the trackpad, and the
  **Hold** toggle latches the left button for one-handed dragging.
- **Typing:** uses your phone's own keyboard, including autocorrect and swipe typing.
- **Modifiers:** tap Ctrl / Alt / Shift / Win once to apply to the next key or click.
  Double-tap to lock one down until you tap it again. For example, lock Alt and tap Tab to step
  through the Alt+Tab switcher.
- **Keys:** arrows and navigation, F1–F12, media and volume, and common shortcuts (copy, paste,
  undo, show desktop, task view, close tab, …). Alt+F4 only fires after a short press-and-hold.
- **Settings (⚙︎):** pointer speed, acceleration, scroll speed, natural scrolling, tap-to-click,
  vibration, and keep-screen-on. They're saved on the phone.

## Requirements

- Windows 10/11 (input goes through the Win32 `SendInput` API)
- Python 3.10+
- Tailscale on the PC and the phone, with
  [HTTPS certificates](https://tailscale.com/kb/1153/enabling-https) enabled for your tailnet

## Setup

```
pip install -r requirements.txt
tailscale serve --bg --https=8777 http://127.0.0.1:8777
```

The `tailscale serve` command only needs to run once, because Tailscale remembers it across
reboots. It publishes the server to your tailnet only (not the internet).

Then start the server, either by double-clicking `start.bat` or with:

```
python server.py
```

On the phone, open `https://<pc-name>.<your-tailnet>.ts.net:8777`. `tailscale serve status`
prints the exact address. You can add it to your home screen for an app-like launcher.

To remove the Tailscale entry: `tailscale serve --https=8777 off`

### Without `tailscale serve`

You can run `python server.py --bind <pc-tailscale-ip>` and open `http://<pc-tailscale-ip>:8777`
instead. With that setup you have to allow the port through Windows Firewall yourself (ideally
only for `100.64.0.0/10`), and keep-screen-on isn't available because the page isn't served over
HTTPS.

## Security

- The server listens on `127.0.0.1` only, so only `tailscale serve` (or local programs) can reach
  it, not your LAN.
- It only answers requests addressed to this machine's own names (localhost and its Tailscale
  name and IPs). It only accepts control connections that come from its own page. Other websites
  open in a browser therefore can't drive your mouse or keyboard, whether through cross-site
  WebSockets or DNS rebinding.
- Anyone on your tailnet who can reach this machine can use the remote. Use Tailscale ACLs if you
  share your tailnet with others.
- When a phone disconnects or goes to sleep, the server releases any buttons or keys it was
  holding down.

## Limitations

- Windows doesn't let a normal program send input to elevated (administrator) windows, UAC
  prompts, or the lock screen. Run the server as administrator if you need that.
- Windows' "Enhance pointer precision" adds its own acceleration on top of the app's. Tune
  pointer speed and acceleration in ⚙︎.
- Typed text is sent as Unicode characters, which works in normal apps. Some games and remote
  desktop clients only accept real key presses. The shortcut buttons and modifier combos always
  send real key presses.

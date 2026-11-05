# Pred Overlay

A small Windows tray app that shows who you are playing with in Predecessor: rank, VP, account level and age, and whether you met someone in your last three matches. It draws over the game in the draft, on the loading screen and while you hold TAB. In the main menu it shows how many players are queueing in each mode of your region.

Unofficial fan project, not affiliated with or endorsed by Omeda Studios.

![Icon](predoverlay/assets/icon.png)

## What you see

| Where | What |
|---|---|
| Draft | Both teams in the bottom-right corner: name, rank and VP, level, account age, "met before"; the header names the mode once the match loads ("OVERCLOCKED · 5V5") |
| Loading screen | The same panel in the top-left corner; it hides once the match starts |
| TAB held | Hover a player on the scoreboard to get their card |
| Main menu | A small READY badge and live queue counts for your region: Ranked, Standard, Overclocked, ARAM, Crucible and any featured mode the game adds later (up to five at a time; VS AI only while someone waits) |

Press **F8** in game to show or hide the panel until the next phase (draft, loading, match).

## How it gets the data

- **Your own account.** The app signs in to Omeda's game backend the way the game does, with a ticket from the Steam client running on your PC. Nothing to type, no password. The session is stored in a private file in your user profile and renews by itself through Steam.
- **Read-only calls.** It asks the backend for the current match roster, public profiles (rank, level), your own match history and matchmaking queue sizes. Nothing else.
- **The game's log file** (`%LOCALAPPDATA%\Predecessor\Saved\Logs\Predecessor.log`) tells it when you are in the menu, the draft or a match.
- **A screen read on your first TAB of a match.** The app reads the player names on the scoreboard rows with Windows' built-in OCR, so it knows which row is which player. Only those rows, and only until every row is matched (at most a few tries per match).

It never opens, reads or modifies the game process or its memory, injects nothing and captures no network traffic.

## Requirements

- Windows 10 or 11, 64-bit.
- Predecessor from **Steam** (Epic and consoles are not supported), installed in any Steam library.
- Steam running and signed in to the account you play on.
- The game launched at least once on this PC.
- Display mode **Windowed Fullscreen** (borderless). Exclusive Fullscreen hides the overlay.
- A 16:9 resolution. Tested at 2560x1440; other 16:9 sizes scale. On other aspect ratios the TAB card may not line up.
- For TAB cards: at least one Windows OCR language (most Windows installs have one). Without it, everything else still works.

## Install

1. Download `PredOverlay-v<version>-win64.zip` from [Releases](https://github.com/TheWeaklessOne/pred-overlay/releases/latest).
2. Right-click the zip, **Properties**, tick **Unblock**, OK.
3. Extract it to `%LOCALAPPDATA%\Programs`. The zip already contains a `PredOverlay` folder, so you get `%LOCALAPPDATA%\Programs\PredOverlay\PredOverlay.exe`.
4. Run `PredOverlay.exe`. The build is not code-signed, so Windows SmartScreen may warn: click **More info**, then **Run anyway**.
5. A gold diamond appears in the tray. On this first start the app adds **Pred Overlay** to the Start menu and the desktop and turns on **Start with Windows** (switch it off in the tray menu if you prefer).

There is no separate installer on purpose: Windows 11's Smart App Control blocks unsigned installers outright, while the app itself runs.

To check your setup: open the `PredOverlay` folder in Explorer, type `powershell` in the address bar and press Enter, then run `.\PredOverlayCLI.exe --check`.

## Updates

The app checks GitHub for a new release a minute after it starts and then every 6 hours. With **Update automatically** on (the default) it downloads the release, checks it against the published SHA-256 and installs it as soon as you are not in a draft or a match (in the menu, or with the game closed): it closes, swaps its folder for the new version and starts again (a few seconds). If the new version does not start, the previous one comes back and that release is only offered, not installed by itself again. Your settings and session are kept. With the switch off, the tray offers **Install update** instead. **Check for updates** asks right away.

## Tray menu

- **Status**: Ready · waiting for a match, Match found, In draft, In match, Sign-in needed, Omeda not answering, Paused.
- **Overlay enabled**: switch the overlay off and on.
- **Show preview**: sample panels for about 30 seconds (one cycle: draft, loading, TAB, menu), no game needed. Double-clicking the icon does the same.
- **Queue region**: which region's queues the main menu shows.
- **Sign in with Steam again**: after starting Steam, after switching Steam accounts, or if sign-in failed.
- **Start with Windows**, **Check for updates** (or **Install update** when one is ready), **Update automatically**, **Open log folder**, **About**, **Quit**.

## Files and settings

Everything lives in `%LOCALAPPDATA%\PredOverlay`:

| File | Contents |
|---|---|
| `session.json` | Your game session tokens. Private to your Windows user; never share it. |
| `settings.json` | Region, hotkey, monitor, on/off |
| `logs\overlay.log` | Phases, counts and errors; no tokens, no player names. Rotates to `overlay.log.1` to `.3`. |
| `match-cache\` | Details of your finished matches, for "met before" |
| `logs\update.log` | What the last update did |

`settings.json` can be edited while the app is closed:

```json
{
  "region": "EUROPE",
  "hotkey": "F8",
  "screen": 0,
  "enabled": true
}
```

`region` is one of `EUROPE`, `NA`, `SA`, `ASIA`, `OCE`, `MENA`. `hotkey` is `F1` to `F12`. `screen` is the monitor index, 0 for the primary one. An invalid value falls back to its default.

## Troubleshooting

Run `.\PredOverlayCLI.exe --check` first, and look at `logs\overlay.log` (tray menu, **Open log folder**).

| Problem | Fix |
|---|---|
| Tray says "Sign-in needed" | Start Steam with your Predecessor account, then **Sign in with Steam again**. The app also retries by itself, but after a failed attempt it waits up to 10 minutes. |
| "Predecessor has not run on this PC yet" | Launch the game once. The app refuses to sign in before that, so it never creates an empty game profile for a Steam account that has not played. |
| "Predecessor (Steam) was not found in any Steam library" | Point the app at the game's `steam_api64.dll` (see below), then restart the app. |
| Sign-in keeps failing with "Steam API did not start", "Steam is not logged in" or "did not return a web-API ticket" | Make sure the Steam account signed in owns Predecessor and Steam is not updating. As a last resort, `.\PredOverlayCLI.exe --sign-in-browser` signs in through Steam's website; that session lasts about a day and then needs the same step again. |
| Nothing shows in game | Check Windowed Fullscreen, that **Overlay enabled** is on, and that the game runs on the monitor set in `screen`. The overlay draws only while the game window is in front. |
| No card on TAB | Hover a player's row while holding TAB. If the log says "scoreboard reading failed", install an OCR language (see below). |
| "Omeda not answering" | The game backend is down or unreachable; the app retries by itself. |

Point the app at the game's DLL (PowerShell, then restart the app):

```powershell
[Environment]::SetEnvironmentVariable('PRED_STEAM_API', 'D:\Games\Steam\steamapps\common\Predecessor\Engine\Binaries\ThirdParty\Steamworks\Steamv157\Win64\steam_api64.dll', 'User')
```

Install an OCR language (PowerShell as administrator):

```powershell
Get-WindowsCapability -Online | Where-Object Name -like 'Language.OCR*'
Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
```

`PredOverlayCLI.exe` without arguments runs the tray app with a console window; closing that window quits the app. Use `PredOverlay.exe` for normal use.

## Uninstall

Run `.\PredOverlayCLI.exe --uninstall` from the app folder: it removes the shortcuts and autostart. Then **Quit** from the tray and delete `%LOCALAPPDATA%\Programs\PredOverlay` and, to remove settings and the session too, `%LOCALAPPDATA%\PredOverlay`.

## Run from source

```powershell
git clone https://github.com/TheWeaklessOne/pred-overlay
cd pred-overlay
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m predoverlay --console     # tray app with the log in this console
.venv\Scripts\python -m predoverlay --preview     # sample panels, works without the game
.venv\Scripts\python -m unittest discover -s tests
```

Build the exe folder with `.venv\Scripts\pyinstaller packaging\PredOverlay.spec --noconfirm`; it lands in `dist\PredOverlay`. Pushing a `v*` tag builds a release on GitHub Actions.

## Caveats

The app talks to Omeda's backend with your account the way the game client does. That interface is not a public API: Omeda can change or block it at any time, and how they view third-party tools is up to them. Use it at your own risk.

## License

[MIT](LICENSE)

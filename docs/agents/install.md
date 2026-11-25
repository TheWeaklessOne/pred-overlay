# Installing Pred Overlay: instructions for an AI agent

This file is for an agent that installs, updates, repairs or removes the app on a user's
PC. The README has ready prompts that point here. If you are here to work on the
code, read [AGENTS.md](../../AGENTS.md) instead.

You are helping a user install and set up Pred Overlay on their Windows PC. Follow these steps in order, verify each one, and explain to the user what you are doing. All commands are PowerShell; they work in Windows PowerShell 5.1 and PowerShell 7, except step 5, which needs Windows PowerShell.

## Rules

- Never ask the user for passwords or tokens, and never print, copy or upload `%LOCALAPPDATA%\PredOverlay\session.json`. Sign-in happens on Pred.gg's own website in the user's browser. The user signs in there themselves; never fill in that page for them and never ask for what they type.
- No administrator rights are needed, except for installing an OCR language (step 5). Ask the user before doing that.
- Do not touch the game's process, files or memory. The only game file you may change is `GameUserSettings.ini` for the display mode (step 4), and only after the user agrees.
- Download releases only from `https://github.com/TheWeaklessOne/pred-overlay/releases`. If the user gave you a zip instead, step 2 says how to use it.

## 1. Preflight

Confirm all of the following with the user or with the commands shown:

- Windows 10/11 64-bit: `[Environment]::Is64BitOperatingSystem` is `True`.
- Predecessor runs on this PC (tested with the Steam version). Console players cannot use this app; stop and say so.
- The game has been launched at least once on this PC:
  `Test-Path "$env:LOCALAPPDATA\Predecessor\Saved\Logs\Predecessor.log"` is `True`. If not, ask the user to start the game once and come back.
- The user has a Pred.gg account, or is fine creating one at sign-in.

## 2. Install or repair

Run this as one PowerShell command: many agent shells forget variables between calls. It downloads the latest release, checks its SHA-256 against the release's `SHA256SUMS.txt`, quits a running copy and replaces the program folder. Settings and the session in `%LOCALAPPDATA%\PredOverlay` are kept.

```powershell
& {
  $ErrorActionPreference = 'Stop'
  $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
  $programs = "$env:LOCALAPPDATA\Programs"
  $dest = "$programs\PredOverlay"
  $release = Invoke-RestMethod https://api.github.com/repos/TheWeaklessOne/pred-overlay/releases/latest
  $asset = $release.assets | Where-Object name -like 'PredOverlay-*-win64.zip' | Select-Object -First 1
  $sums = $release.assets | Where-Object name -eq 'SHA256SUMS.txt'
  if (-not $asset -or -not $sums) { throw 'The release lacks its zip or SHA256SUMS.txt' }
  $zip = Join-Path $env:TEMP $asset.name
  Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing
  Invoke-WebRequest $sums.browser_download_url -OutFile "$zip.sha256" -UseBasicParsing
  $line = Get-Content "$zip.sha256" | Where-Object { $_ -like "*  $($asset.name)" } | Select-Object -First 1
  if (-not $line -or ($line -split '\s+')[0] -ne (Get-FileHash $zip -Algorithm SHA256).Hash) { throw 'Checksum mismatch' }

  Unblock-File $zip
  $cli = "$dest\PredOverlayCLI.exe"
  if (Test-Path $cli) { & $cli --quit }   # quits as from the tray menu
  Get-Process PredOverlay, PredOverlayCLI -ErrorAction SilentlyContinue | Stop-Process -PassThru | Wait-Process
  if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
  New-Item -ItemType Directory -Force $programs | Out-Null
  Expand-Archive $zip -DestinationPath $programs -Force
  Test-Path "$dest\PredOverlay.exe"
}
```

If the user gave you a zip, drop the lines from `$release` to the checksum check and set `$zip` to its path instead.

The zip contains a `PredOverlay` folder, so it lands in `%LOCALAPPDATA%\Programs\PredOverlay`. Later versions install themselves (see Updates); repeat this step only to repair an install.

Each release also has `PredOverlay-v<version>-setup.exe`, a setup wizard for people who install by hand. It puts the same app in the same folder. Use the zip as above: the command checks it against `SHA256SUMS.txt` and needs no clicks. (Run silently, the setup would also start nothing.) Replacing a setup install this way is fine; its entry in Windows' Apps list stays valid.

If Windows says "An Application Control policy has blocked this file" for `PredOverlay.exe`, Smart App Control is on and does not trust this unsigned file: make sure it came from the official release. Never turn Smart App Control off for the user; it cannot be turned back on without reinstalling Windows.

## 3. Check the setup

```powershell
& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --check
```

Each line is `OK`, `WARN` or `FAIL`. `FAIL` blocks the overlay, `WARN` limits it. On a fresh install `session` says "not signed in yet", which is expected: sign-in is part of the first start.

| Line | If not OK |
|---|---|
| `played` | Ask the user to launch the game once. |
| `display` | See step 4. |
| `names` | "the overlay's name reader did not load": the app's own recognizer (Windows ML, `assets\namereader.onnx`) is unavailable and Windows OCR reads the names alone; `ocr` must then be OK. A missing file means a damaged download: repeat step 2. |
| `ocr` | "no Windows OCR language installed": see step 5. With `names` OK this is a WARN: the names are still read, but the TAB card has no hero line and unsure names get no second reading. With `names` not OK it is a FAIL and the panels stay empty. "Windows OCR runtime missing": the download is damaged, repeat step 2. |
| `session` | Before the first start this is expected. Afterwards see Troubleshooting. |
| `gui` | The download is damaged; repeat step 2. |

## 4. Display mode

The overlay needs **Windowed Fullscreen** at a 16:9 resolution. The simplest way is in the game's video settings (window mode: Windowed Fullscreen). If the user prefers that you do it, close the game first, back up `%LOCALAPPDATA%\Predecessor\Saved\Config\WindowsClient\GameUserSettings.ini`, then set `FullscreenMode=1`, `LastConfirmedFullscreenMode=1` and `PreferredFullscreenMode=1` in it.

## 5. OCR language (needs admin)

Only if `--check` reports no OCR language. With the user's consent, in Windows PowerShell (`powershell.exe`, not `pwsh`: these cmdlets fail in PowerShell 7) run as administrator:

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
```

## 6. First start

```powershell
Start-Process "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlay.exe"
```

- If Windows SmartScreen appears, tell the user to click **More info**, then **Run anyway** (the build is not code-signed).
- A gold diamond icon appears in the tray with a "Pred Overlay is running" notification.
- On its first start the app adds a **Pred Overlay** shortcut to the Start menu and the desktop, turns on Start with Windows and opens its sign-in panel. Ask the user to click **Sign in on Pred.gg**, sign in on Pred.gg's page in the browser (or create an account there) and allow Pred Overlay; the panel then says "Signed in". If the tab was closed, **Open the page again** in the panel reopens it; the tray menu's **Sign in with Pred.gg…** brings the panel back. Check: `Test-Path "$([Environment]::GetFolderPath('Desktop'))\Pred Overlay.lnk"` and `(Get-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run).PredOverlay` both give a result.
- The app reads nothing until the game runs: the tray says "Waiting for Predecessor". Ask the user to start the game.
- Verify about 15 seconds after the game started: `& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --check` reports `session  signed in`, the tray does not say "Sign-in needed", and in `Get-Content "$env:LOCALAPPDATA\PredOverlay\logs\overlay.log" -Tail 30` there is no `failed` line after the latest `starting` line. A network failure shows as `Pred.gg offline since …` and retries by itself; a single `search failed` or `profiles failed` line is a refused or unusable answer, also retried.
- Ask the user to right-click the tray icon and choose **Show preview**: sample panels appear for about 30 seconds.

## 7. Configure

Ask the user, then set from the tray menu:

- **Start with Windows**: on after the first start; switch it off only if they want.
- **Update automatically**: on by default. New releases then install themselves whenever the user is not in a draft or match.

The F8 hotkey and the monitor are in `%LOCALAPPDATA%\PredOverlay\settings.json` (`hotkey`: `F1`..`F12`, `screen`: monitor index, 0 = primary). Edit them only while the app is not running; invalid values fall back to the defaults.

Tell the user how it behaves: panels appear in the draft (bottom right, their own team only) and on the loading screen (top left, both teams); in a match, hold TAB and hover a player for their card; F8 shows or hides the panel until the next phase. Console players show as `CONSOLE PLAYER` with a made-up two-word tag: Pred.gg hides their names, and the app learns who they were a few minutes after each match, so their data shows from the next match with them on. Met for the first time, a cleanly read name Pred.gg does not know shows as `CONSOLE PLAYER?`. The session renews itself; **Sign out** in the tray menu deletes it.

## Troubleshooting

Read `%LOCALAPPDATA%\PredOverlay\logs\overlay.log` first; look at the lines after the latest `starting`.

| Log or symptom | Cause and fix |
|---|---|
| Tray "Sign-in needed" | Pred.gg refused the session, or there is none. Click the notification or the tray menu's **Sign in with Pred.gg…**, then **Sign in on Pred.gg** in the panel. |
| `sign-in failed: AuthTimeout` or `AuthCancelled` | Pred.gg's page was left for more than three minutes, the user did not allow Pred Overlay, or **Cancel** was pressed in the sign-in panel. **Sign in with Pred.gg…** in the tray, then **Sign in on Pred.gg**. |
| `sign-in failed: AuthError` with "local sign-in listener" | Another sign-in is open, or another program uses port 47833. Close it and retry. |
| `sign-in failed: AuthError` otherwise | Pred.gg did not accept the answer, or the PC could not reach Pred.gg. A message with `TransportError: <kind>` in its brackets is the second: `SSLCertVerificationError` points at antivirus HTTPS scanning or a company proxy whose certificate Windows does not trust (the app lets Windows verify certificates, so what a browser on that PC accepts, it accepts), `gaierror` at DNS, `TimeoutError` at the connection. Retry; if it keeps failing, collect diagnostics (below). |
| Row says CONSOLE PLAYER? / PRED.GG HIDES CONSOLE NAMES | A name read cleanly that Pred.gg does not know: almost always a console player met for the first time. Not an error; the app learns them after the match. |
| Row says NOT FOUND ON PRED.GG / NAME MAY BE MISREAD | The name was not read with certainty and no spelling of it was found. Report it with the diagnostics zip if it keeps happening for one player. |
| Row says PRED.GG DID NOT ANSWER | Pred.gg has not answered for that player. The app asks again every three minutes for as long as the match runs, and the row fills in when an answer comes. See `Pred.gg offline` below. |
| Rows say NOT SIGNED IN ON PRED.GG / SIGN IN FROM THE TRAY ICON | The names were read, but the app has no Pred.gg session, so it asks nothing. Sign in from the tray (**Sign in with Pred.gg…**); the rows fill in at once, also in the middle of a draft. |
| Row says NO PROFILE ON PRED.GG | Pred.gg found the name but gave no profile for it. Not an error of the app. |
| `learned N players from a finished match` | Not an error: players (console players among them) matched to their names after a match. |
| `read draft #N: …` / `read loading #N: …` | Not an error: one line per reading of a screen (a draft reading like the last one is not repeated): names with text, sure ones, how long it took, and `taken`, `nothing new` or `dropped (why)`. |
| `loading over: … 0 readings …, skipped: …` | The loading screen went by unread; the reasons count the skipped moments (`in front: <window class>` when another window was in front, a reading still running, the panel moving). The TAB board then gives the players it shows. Report it with the diagnostics zip if it keeps happening. |
| `screen: loading shown (…)` / `not shown` | Not an error: the loading screen's pixels appeared or went, with the log's phase at that moment. |
| `draft: names read, but no pick-screen header or own name` | The draft's names were not taken: the pick screen looked different from what the app knows (another layout, or your own name unread). Report it with the diagnostics zip. |
| `known players could not be read` | `known-players.json` could not be opened (another program holds it?). The app runs without saving learned players; restart it later. |
| `known players not saved` | Writing `known-players.json` failed once; the app keeps them in memory and tries again at the next match. |
| `roster reading failed` or `scoreboard reading failed` | No OCR language and no name reader: step 5, then `--check`. |
| `Windows OCR did not read (…); names by the model alone` | No OCR language: the app's recognizer reads the names alone. Step 5 adds the hero line on TAB cards. |
| `name model ready: …` | Not an error: the app's recognizer loaded when the game started. |
| `name model not loaded (…)` or `name model failed (…)` | The recognizer is unavailable (Windows older than 10 version 2004, a damaged download): Windows OCR reads the names alone, so `ocr` must be OK. Repeat step 2; if it persists, collect diagnostics. |
| `Pred.gg offline` / tray "Pred.gg not answering" | Pred.gg is unreachable; it retries by itself. Check the internet connection. |
| Nothing visible in game | Exclusive Fullscreen (step 4), overlay switched off in the tray, or the game is on another monitor than `screen`. |
| No tray app at hand | `PredOverlayCLI.exe --sign-in` runs the same browser sign-in from a console; a running app picks the new session up by itself. |
| `previous run ended abruptly` | The last run stopped without quitting: a crash, Task Manager, or a power cut. If it says `crash.log grew`, `logs\crash.log` holds the Python stacks of that moment; otherwise the process was killed or crashed outside Python. |
| `previous run ended with Windows, not by itself` | Windows went down while the app ran: a blue screen, a power cut, a reset or a forced shutdown. Not an error of the app; an ordinary shutdown or restart ends it with `stopped: Windows ended the session`. |
| `… repeated N× over M min` | The line before it came N more times; the log keeps one copy every 10 minutes so the history survives. |
| `heartbeat:` and `at stop:` lines | Every 30 minutes, and once when the app stops: uptime, memory, handles, GDI/USER objects, threads. A number that only grows across heartbeats is a leak worth reporting. |

To collect everything for a bug report, run `& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --diagnostics`. It saves `PredOverlay-diagnostics-<date>-<time>.zip` on the desktop and prints its path: the logs (`overlay.log*`, `update.log`, `crash.log`), `settings.json`, the `--check --json` results, version and screen details and Windows' crash reports for `PredOverlay.exe` from the last 14 days. Tokens and the profile path are redacted; `session.json` and `known-players.json` are never included. Give the user the path and let them decide whether to send it; do not upload it yourself.

## Updates

The app checks GitHub a minute after start and every hour, downloads a new release, verifies its SHA-256 and swaps its own folder for it when the user is not in a draft or match, then restarts with an "Updated to" notification. `%LOCALAPPDATA%\PredOverlay\logs\update.log` records each swap. If an update failed there, step 2 installs the latest release by hand.

## Uninstall

If `%LOCALAPPDATA%\Programs\PredOverlay.uninstall` exists, the user installed with the setup. Its uninstaller quits the app and removes the shortcuts, autostart and the program folder. Run silently, it keeps `%LOCALAPPDATA%\PredOverlay`:

```powershell
$uninstaller = Get-ChildItem "$env:LOCALAPPDATA\Programs\PredOverlay.uninstall\unins*.exe" | Select-Object -First 1
Start-Process $uninstaller.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
```

Otherwise:

```powershell
& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --quit
& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --uninstall
Get-Process PredOverlay, PredOverlayCLI -ErrorAction SilentlyContinue | Stop-Process -PassThru | Wait-Process
Remove-Item "$env:LOCALAPPDATA\Programs\PredOverlay" -Recurse -Force
```

`--uninstall` removes the shortcuts and autostart. Remove `%LOCALAPPDATA%\PredOverlay` too only if the user also wants their settings, session and learned players gone.

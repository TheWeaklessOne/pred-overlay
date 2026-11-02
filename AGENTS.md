# Installing Pred Overlay: instructions for an AI agent

You are helping a user install and set up Pred Overlay on their Windows PC. Follow these steps in order, verify each one, and explain to the user what you are doing. All commands are PowerShell; they work in Windows PowerShell 5.1 and PowerShell 7.

## Rules

- Never ask the user for passwords or tokens, and never print, copy or upload `%LOCALAPPDATA%\PredOverlay\session.json`. Sign-in happens through the user's running Steam client; there is nothing to type.
- No administrator rights are needed, except for installing an OCR language (step 5). Ask the user before doing that.
- Do not touch the game's process, files or memory. The only game file you may change is `GameUserSettings.ini` for the display mode (step 4), and only after the user agrees.
- Download releases only from `https://github.com/TheWeaklessOne/pred-overlay/releases`. If the user gave you a zip instead, skip the download in step 2 and use that file as `$zip`.

## 1. Preflight

Confirm all of the following with the user or with the commands shown:

- Windows 10/11 64-bit: `[Environment]::Is64BitOperatingSystem` is `True`.
- Predecessor is the **Steam** version. Epic or console players cannot use this app; stop and say so.
- Steam is running and signed in to the account they play on:
  `(Get-ItemProperty HKCU:\Software\Valve\Steam\ActiveProcess).ActiveUser` is not `0`.
- The game has been launched at least once on this PC:
  `Test-Path "$env:LOCALAPPDATA\Predecessor\Saved\Logs\Predecessor.log"` is `True`. If not, ask the user to start the game once and come back.

## 2. Install or update

```powershell
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
$programs = "$env:LOCALAPPDATA\Programs"
$dest = "$programs\PredOverlay"
$release = Invoke-RestMethod https://api.github.com/repos/TheWeaklessOne/pred-overlay/releases/latest
$asset = $release.assets | Where-Object name -like 'PredOverlay-*-win64.zip' | Select-Object -First 1
if (-not $asset) { throw 'No release zip found' }
$zip = Join-Path $env:TEMP $asset.name
Invoke-WebRequest $asset.browser_download_url -OutFile $zip

$sums = $release.assets | Where-Object name -eq 'SHA256SUMS.txt'
if ($sums) {
  Invoke-WebRequest $sums.browser_download_url -OutFile "$zip.sha256"
  $want = ((Get-Content "$zip.sha256") -split '\s+')[0]
  if ($want -ne (Get-FileHash $zip -Algorithm SHA256).Hash) { throw 'Checksum mismatch' }
}
```

Then install. An update replaces only the program folder; settings and the session in `%LOCALAPPDATA%\PredOverlay` are kept.

```powershell
Unblock-File $zip
Get-Process PredOverlay, PredOverlayCLI -ErrorAction SilentlyContinue | Stop-Process -PassThru | Wait-Process
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory -Force $programs | Out-Null
Expand-Archive $zip -DestinationPath $programs -Force
Test-Path "$dest\PredOverlay.exe"
```

The zip contains a `PredOverlay` folder, so it lands in `%LOCALAPPDATA%\Programs\PredOverlay`. Later versions install themselves (see Updates); repeat this step only to repair an install or to move from a version older than 0.1.3, which had no updater.

There is no installer exe on purpose: Smart App Control blocks unsigned installers. If Windows says "An Application Control policy has blocked this file" for `PredOverlay.exe`, Smart App Control is on and does not trust this file: make sure it came from the official release zip. Never turn Smart App Control off for the user; it cannot be turned back on without reinstalling Windows.

## 3. Check the setup

```powershell
& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --check
```

Each line is `OK`, `WARN` or `FAIL`. `FAIL` blocks the overlay, `WARN` limits it. On a fresh install `session` says "not signed in yet", which is expected: sign-in happens on first start.

| Line | If not OK |
|---|---|
| `steam` | Ask the user to start Steam and sign in. |
| `game` | Predecessor (Steam) not found. If it is installed in an unusual place, find `steam_api64.dll` under the game folder (`Get-ChildItem <game folder> -Recurse -Filter steam_api64.dll`; the right one has `Steamworks` and `Win64` in its path) and set it for the user and for this shell: `[Environment]::SetEnvironmentVariable('PRED_STEAM_API', '<full path>', 'User'); $env:PRED_STEAM_API = '<full path>'`. |
| `played` | Ask the user to launch the game once. |
| `display` | See step 4. |
| `ocr` | "no Windows OCR language installed": see step 5, optional (only the TAB card needs it). "Windows OCR runtime missing": the download is damaged, repeat step 2. |
| `session` | Before the first start this is expected. Afterwards see Troubleshooting. |
| `gui` | The download is damaged; repeat step 2. |

## 4. Display mode

The overlay needs **Windowed Fullscreen**. The simplest way is in the game's video settings (window mode: Windowed Fullscreen). If the user prefers that you do it, close the game first, back up `%LOCALAPPDATA%\Predecessor\Saved\Config\WindowsClient\GameUserSettings.ini`, then set `FullscreenMode=1`, `LastConfirmedFullscreenMode=1` and `PreferredFullscreenMode=1` in it.

## 5. OCR language (optional, needs admin)

Only if `--check` reports no OCR language. With the user's consent, in an elevated PowerShell:

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
```

## 6. First start

```powershell
Start-Process "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlay.exe"
```

- If Windows SmartScreen appears, tell the user to click **More info**, then **Run anyway** (the build is not code-signed).
- A gold diamond icon appears in the tray with a "Pred Overlay is running" notification.
- On its first start the app adds a **Pred Overlay** shortcut to the Start menu and the desktop and turns on Start with Windows. Check: `Test-Path "$([Environment]::GetFolderPath('Desktop'))\Pred Overlay.lnk"` and `(Get-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run).PredOverlay` both give a result.
- Within a few seconds the app signs in through Steam. Steam may briefly show the user as playing Predecessor; that is the sign-in, and it ends by itself.
- Verify after about 15 seconds: `& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --check` reports `session  signed in`, and in `Get-Content "$env:LOCALAPPDATA\PredOverlay\logs\overlay.log" -Tail 30` there is no `failed` line after the latest `starting` line. A single `display failed` or `queues failed` is a network hiccup and retries by itself.
- Ask the user to right-click the tray icon and choose **Show preview**: sample panels appear for about 30 seconds.

## 7. Configure

Ask the user, then set from the tray menu:

- **Queue region**: Europe, North America, South America, Asia, Oceania or Middle East.
- **Start with Windows**: on after the first start; switch it off only if they want.
- **Update automatically**: on by default. New releases then install themselves whenever the user is not in a draft or match.

The F8 hotkey and the monitor are in `%LOCALAPPDATA%\PredOverlay\settings.json` (`hotkey`: `F1`..`F12`, `screen`: monitor index, 0 = primary). Edit them only while the app is not running; invalid values fall back to the defaults.

Tell the user how it behaves: panels appear in the draft (bottom right) and on the loading screen (top left); in a match, hold TAB and hover a player for their card; F8 shows or hides the panel until the next phase.

## Troubleshooting

Read `%LOCALAPPDATA%\PredOverlay\logs\overlay.log` first; look at the lines after the latest `starting`.

| Log or symptom | Cause and fix |
|---|---|
| `Steam is not running.` | Start Steam, then tray menu, **Sign in with Steam again** (the automatic retry waits up to 10 minutes after a failure). |
| `Steam is not logged in.` | Sign in to Steam, then **Sign in with Steam again**. |
| `Predecessor has not run on this PC yet` | Launch the game once, then **Sign in with Steam again**. |
| `was not found in any Steam library` | Set `PRED_STEAM_API` (step 3, `game` row) and restart the app. |
| `Steam API did not start` | The signed-in Steam account does not own Predecessor, or Steam is mid-update. |
| `Steam did not return a web-API ticket in time` | Steam is busy or offline; retry with **Sign in with Steam again**. |
| `scoreboard reading failed` | No OCR language: step 5. |
| `Omeda offline` / tray "Omeda not answering" | The game backend is unreachable; it retries by itself. Check the internet connection. |
| Nothing visible in game | Exclusive Fullscreen (step 4), overlay switched off in the tray, or the game is on another monitor than `screen`. |
| Steam client sign-in impossible | `PredOverlayCLI.exe --sign-in-browser` opens Steam's website for sign-in. The session lasts about a day; repeat when it expires. |

## Updates

The app checks GitHub a minute after start and every 6 hours, downloads a new release, verifies its SHA-256 and swaps its own folder for it when the user is not in a draft or match, then restarts with an "Updated to" notification. `%LOCALAPPDATA%\PredOverlay\logs\update.log` records each swap. If an update failed there, step 2 installs the latest release by hand.

## Uninstall

```powershell
& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --uninstall
Get-Process PredOverlay -ErrorAction SilentlyContinue | Stop-Process -PassThru | Wait-Process
Remove-Item "$env:LOCALAPPDATA\Programs\PredOverlay" -Recurse -Force
```

`--uninstall` removes the shortcuts and autostart. Remove `%LOCALAPPDATA%\PredOverlay` too only if the user also wants their settings and session gone.

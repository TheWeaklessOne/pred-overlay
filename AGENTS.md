# Working on Pred Overlay

Notes for anyone changing the code, people and coding agents alike. If you came here to
install the app for a user, follow [docs/agents/install.md](docs/agents/install.md)
(https://raw.githubusercontent.com/TheWeaklessOne/pred-overlay/main/docs/agents/install.md)
instead.

The app is a PySide6 tray program for Windows. It reads player names from the game's
screen and log file and looks them up on Pred.gg with the user's own account.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the parts fit together; read
it before changing anything in `predoverlay/overlay/`.

## Setup

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]" -c packaging\constraints.txt
git config core.hooksPath .githooks
```

The last line turns on the repository's hooks: `pre-commit` runs the privacy check,
ruff and the formatter on staged files, `pre-push` runs the privacy check and the whole test suite.

## Checks

CI (`.github/workflows/build.yml`) runs these on pushes to `main`, on `v*` tags and on
pull requests, then
builds the exe folder and runs its self-test. Run them yourself before you commit:

```powershell
.venv\Scripts\python tools\check_public.py
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
$env:QT_QPA_PLATFORM = 'offscreen'; .venv\Scripts\python -m unittest discover -s tests; Remove-Item Env:QT_QPA_PLATFORM
```

The tests need Windows for the OCR and Windows ML parts; elsewhere those tests skip.
With `QT_QPA_PLATFORM=offscreen` Qt draws without opening a window, so the suite can
run while someone plays on the same PC. Keep it that way: a test must never show a
window, call the network or read the real `%LOCALAPPDATA%\PredOverlay`.

If you have recorded matches of your own (`--record`, see README), run
`python -m predoverlay.overlay.replay --all` after changing the screen readers or the
core. It reruns every recorded match and fails when a check that held before breaks.

## Rules

These come from what Omeda said is fine, from Pred.gg's API and from the people the app
reads about.
A change that breaks one of them will not be merged.

- **Three sources only: the screen, the game's log file and Pred.gg's API.** Never call
  Omeda's servers, never open the game's process or read its memory, hook no input,
  capture no network traffic. Omeda's answer (through the official Discord) was that reading
  the screen and the log and using a public API is fine, and that calling their backend with a player's session is not.
  `tools/check_public.py` fails on Omeda's backend host names.
- **Pred.gg through the app's OAuth client only**, with the tokens the user's own
  sign-in gave (PKCE, `predgg.py`). Never take a token from the Pred.gg website or ask
  a user for one. Keep the request budget: one search per screen of names, the retry
  schedule (`CARD_RETRY` in `core.py`), the User-Agent that names the app.
- **Other players' names stay on the user's PC.** Not in logs, not in the diagnostics
  zip, not in this repository. Test fixtures and screenshots use invented names
  (`Test_Name42`, `PREDOVERLAY7`, the preview's players). Recordings and screen
  captures hold real names: never commit them, never paste them into an issue.
- **Tokens are never logged, printed or shown.** Errors name the failure by its class
  (`TransportError: SSLCertVerificationError`), not by a text that could hold a token.
  `session.json` is never read for output and never goes into the diagnostics zip.
- **No visible console windows.** Every `subprocess` call in `predoverlay/` passes
  `creationflags=CREATE_NO_WINDOW` (see `updater.py`); players run this next to a
  fullscreen game, and a console window would take its focus.
- **Draw only in the documented places**: the draft, the loading screen, while TAB is
  held and on the end screen. The overlay must not cover the game's own controls (the
  TAB card keeps clear of the mute and report buttons).

## Where things are

| Path | What |
|---|---|
| `predoverlay/app.py`, `tray.py` | Start, single instance, the tray menu, clean exit |
| `predoverlay/predgg.py`, `auth.py` | Pred.gg sign-in (PKCE on a loopback port) and the session file |
| `predoverlay/updater.py` | Update check, download, SHA-256 check and the folder swap |
| `predoverlay/overlay/core.py` | The reducer: events in, state and commands out. No I/O |
| `predoverlay/overlay/tracker.py`, `lanes.py` | The loop that feeds `core` and runs its commands |
| `predoverlay/overlay/roster.py`, `scoreboard.py`, `screens.py`, `namereader.py` | Reading names from the screen |
| `predoverlay/overlay/window.py`, `paint/` | The overlay window and everything it draws |
| `predoverlay/overlay/recorder.py`, `replay.py` | Recording matches locally and replaying them |
| `tools/` | Screenshots, the icon, replaying frames, the privacy check |
| `docs/agents/install.md` | The install guide an agent follows for a user |

## Making a change

- Keep changes small and in the style around them. `core.py` stays free of I/O so it
  can be tested by feeding it events.
- Add a test that fails without your change. For screen reading, add an invented
  sample, not a real screenshot.
- Update what users read: README for what they see, `docs/agents/install.md` for
  install steps and log lines, `docs/ARCHITECTURE.md` for how it works. After a change
  to the panel or the card, redraw the screenshots with `python tools/make_screenshots.py`.
- Commit messages: a subject that says what changed in plain words ("Show a player's
  matches in all on the TAB card"), and a body with why and what you checked.
- A release is a version bump in `predoverlay/__init__.py` and a `v<version>` tag on
  `main`. CI refuses a tag that does not match the version, builds the zip and its
  SHA-256 and publishes the release; installed copies pick it up within an hour.

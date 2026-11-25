---
name: Bug report
about: Something in the overlay does not work as the README says
---

**What happened, and where** (draft, loading screen, TAB, end screen, tray, sign-in, update):


**What you expected:**


**Version** (tray menu, About) **and your screen** (resolution, Windowed Fullscreen?):


**Diagnostics.** In PowerShell, run
`& "$env:LOCALAPPDATA\Programs\PredOverlay\PredOverlayCLI.exe" --diagnostics` and attach the zip
it saves on your desktop. Tokens and your Windows profile path are blanked out, but an issue
is public: look through the zip first.

Please do not paste other players' names or screenshots that show them: describe the row
instead ("the third enemy row stays empty").

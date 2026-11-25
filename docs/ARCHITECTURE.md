# Architecture: screen, log and Pred.gg

The overlay uses three sources and nothing else:

1. **The game's log file** (`%LOCALAPPDATA%\Predecessor\Saved\Logs\Predecessor.log`):
   the phase (menu, match found, draft, loading, match, end), the player's own
   ID and name, the game mode, and the match ID once the match is over.
2. **The screen**: the names the game itself shows. The draft shows your own
   team, the loading screen and the TAB scoreboard show both teams. They are
   read from pixels, like a screenshot tool would (the overlay's own recognizer,
   then Windows OCR): the overlay never opens the game's process or memory.
3. **Pred.gg's API**, with the player's own Pred.gg account (OAuth 2 with PKCE):
   who a name belongs to, their ranks, mains and recent matches.

It never talks to Omeda's servers. Omeda's answer (November 2026, through the
official Predecessor Discord) was that reading the screen and the log and using a
public API is fine, and that calling their
backend with a player's session is not: that route can show what the game does
not (the enemy team in the draft).

## What each phase shows

| Phase | On screen | From Pred.gg |
|---|---|---|
| Menu | the badge | nothing |
| Draft | your team, bottom right | rank, peak, main, met before, last played; your day so far |
| Loading screen | both teams, top left | the same for the enemy team |
| Match, TAB held | the card of the row under the cursor | the same, plus the hero on the scoreboard |
| End of match | the card of the row under the cursor | the same |

"Account age" is the time since the player's first match on Pred.gg. The
enemy team is never shown in the draft: the game does not show it either.

## Data flow

```
log file ──► phases.LogFollower ──► tracker ──► core.reduce ──► view ──► window
screen   ──► roster.Reader (OCR) ──┘    ▲             │
                                        │             ▼ commands
                              lanes.PredggIO ◄────────┘
```

* `overlay/core.py` is a pure reducer: events in, commands out, `view(state)`
  for the window. Screen readings arrive as events like any lane result.
* The window reads the draft or loading screen every few seconds
  (`roster.py`): each name box in three versions (as it is, and dark on white
  at two brightness cuts suited to that screen), because the loading screen's
  art behind a name fools one version or another, rarely all. The loading screen
  is read until two readings after every name of it is in (ten readings at most);
  the draft is read to its end, because its rows change with every pick.
* A draft reading counts only when its ALLY header is read in the same grab,
  or (a game in another language) one row reads as my own name: the pick screen
  and the waiting screen before it, which show the same names in the same rows.
  The role screen between them has other text where the names go.
* The role screen's own names are read too: a plate under each role a player
  picked, stacked when several pick the same (`roster.ROLE_X`, `plate`). A box
  counts only with a plate in it (a dark band under an even border line), and a
  reading only with the column labels ("MIDLANE") or my own name on a plate. It
  only adds names: who has not picked yet has no plate and does not go.
* The players are seats (`seats.py`: `a0`.. your team, `e0`.. the other), one per
  player, each with what every screen read of the name: the draft (plates and pick
  rows), the loading screen and the TAB board. Every reading of every screen goes
  through one function, `seats.place`: an entry joins the seat its name matches;
  one with no name to match (a console tag, a misread) joins the seat of its role
  when the screen tells roles (loading columns, board rows); else it opens a seat
  while the team has room. Who a seat's name is on Pred.gg is kept apart, as its
  identity (`identity.py`: unknown, asked, found, not found, known from the log,
  my recent matches or a learned name).
* A seat's name counts once two different versions agree, or one version gave the
  same name in three readings (four characters at least): a static screen repeats
  a misread in the same version, so repeats alone are weak. The first screen whose
  versions agree gives the reading; the others' spellings are searched as well.
* Draft rows are matched to seats by name, not by place: players swap rows when
  they trade roles. A seat no full pick-screen reading has seen three times
  running was a misread and goes.
* On the loading screen my team's draft names are matched to the columns by
  name; the columns are roles (carry, support, mid, jungle, offlane), which is
  what the learning below needs. Names the draft did not give come from here;
  when every column is read and placed, a draft name no column took goes.
* The TAB board is a source like the others (the end board gives cards only: it is
  read through the victory cinematic, whose words pass for names). It is read when TAB
  is first held and a few times more while rows or players are missing: both teams
  in role order, with the hero each plays. A player the loading screen
  missed (the app started late, the screen went unread) gets a seat and a lookup
  from it, and its role, so that match still teaches console names.
* Each screen spells a name in its own font over its own background, so each
  keeps its spelling: the draft's (with a narrow box for short names), the
  loading screen's and the TAB board's. A name Pred.gg did not know is searched
  again under a spelling not tried yet; Pred.gg's search keeps dots and spaces
  ("KV.DV." finds the player, "KVDV" does not). On the evening's frames this
  found a teammate the draft never read and the loading screen misread.
* Each name is looked up on Pred.gg (`playersPaginated(search)`), all names of
  a screen in one request. A candidate is taken only when its name matches the
  reading (case, symbols, accents and OCR look-alikes folded; the same spelling
  wins over a look-alike, then the one who played last). A name with no safe
  candidate stays on the panel without data.
* My own row is known from the log: its user ID is my Pred.gg player ID.
* My last 20 matches (with everyone in them and the points each moved me) give
  "met before", the day so far, and names (with Pred.gg's spelling) that need
  no search.

## Console players

Pred.gg hides console players' names (`isNameConsole`, and `HIDDEN` in match
lists), so a console player cannot be found by the name the game shows.

* After a match the core asks Pred.gg for it (a minute later, or at once when the
  next match is accepted, then with a
  backoff). Its players come with team and role; the loading screen showed the
  same teams in the same role columns, so each name read there gets the player
  in that role. One player per role and team, or nothing is learned for it, and
  a team is learned only if every name Pred.gg does show (its PC players) sits
  in the column of that player's role. Matches still to learn from are kept in
  the same file, so closing the game right after a match loses nothing: they
  are asked for at the next start.
* A player found through a learned name whose Pred.gg profile shows a PC name
  other than the one on screen was learned wrong: that name is searched instead.
* `known-players.json` keeps `HMAC-SHA256(key, name)` → player ID, with a random
  key made on this PC. No name is ever stored. Since 0.5.3 each player has three
  entries: the hash of the name's folded key, of its spelling as read
  (`lookup.strict_key`: case, spaces, edge marks, l/I and -/_ aside; digits,
  other punctuation and inner marks kept), and one saying the spelling is known.
  The next match with that player finds them by the spelling, so "Gh0st" or
  "ghost_" is not taken for a learned "Ghost"; a player learned before 0.5.3 is
  still found by the key alone.
* The panel never shows a console player's name: the row reads `CONSOLE PLAYER`
  with a two-word tag from the same keyed hash (`aliases.label`), the same on this
  PC every time. A name Pred.gg does not know at all reads `UNKNOWN PLAYER` (or
  `CONSOLE PLAYER?` for a clean reading), with a tag too. No name shows while a
  row is still being looked up.
* The TAB reader matches scoreboard rows to the seats by the names the screen
  showed (and the core places every row too), so console players have their
  cards.

## Requests

Per match, about: one to five searches (a name read later gets its own, so on the
role screen, where names come one by one as roles are picked, often one each; a
name read while a search is out waits for its answer), one or two requests for
profiles, one for my recent matches (asked again while the match that just ended
is not listed yet), and one for the finished match.

The profile request (`lookup.profile_query`, all players in one) carries, besides
ranks and seasons, how each one plays (`lookup._PLAYS`, about 10 KB a player):
every role and hero with games, wins, kills, deaths and assists (Pred.gg's own
totals over all their matches), their last ten matches (team, hero, result, end
time, match ID) and the ten players most often on their side (`commonPlayers`).
An error inside those parts is let go (`Client.query(tolerate=...)`): the ranks
still show. From them (`core.shown_stats` puts heroes under the game's names,
"Wood" is Mourn):

* the TAB card's ON HERO / ON ROLE / LAST 10 line (`rows.plays`) and facts
  (`rows.habits`): a fresh account winning a lot, one hero lately (6 of the last
  10) or for good (35% of 50 matches or more), the games played today;
* parties (`core.parties`): two of a team are a party when Pred.gg counts at
  least 10 games of theirs together (and 2% of their matches), or they shared a
  side in three of their last ten; DUO / TRIO / N-STACK on the panel, the
  partners and games together on the card;
* the draft row's record on the hero picked (`rows.picked_line`), the hero read
  from the line under the name on the pick screen (`roster.HERO_DY`).

Every HTTPS request (Pred.gg's API and token endpoint, portraits, the update check and
download) is verified by Windows itself (`transport.tls_context`, the `truststore`
package, the app's one dependency beyond Qt and the Windows bindings): Python alone
trusts only the root certificates Windows has already fetched, and a fresh PC may not
have met Pred.gg's yet.

Profiles wait for a search in flight, not for one waiting out a backoff.
Failed lookups back off (5, 15, 60, 180 s). After the fourth failure the rows say
that Pred.gg did not answer, and the search and the profile request go on every three
minutes: an outage that ends during the match still gives the rows their data. A refused
session (HTTP 401 after one token refresh, or a refused refresh) stops all reads
until the user signs in again, and drops the reads already queued. A 403 from
the API counts as a refused request, not a refused session.

## Sign-in

The app is a public OAuth client ("Pred Overlay (public)" on Pred.gg) without a
secret. Sign-in opens Pred.gg's authorize page in the browser; Pred.gg sends the
code back to `http://127.0.0.1:47833/callback`, and the app exchanges it with
its PKCE verifier. The access and refresh tokens are kept in
`%LOCALAPPDATA%\PredOverlay\session.json` and never logged; they are refreshed
shortly before they expire. Sign out deletes them. While a sign-in is needed
the tracker watches the session file, so a sign-in from the console
(`--sign-in`) is taken up by a running app; the app's own refresh writes are
told apart from that. The app knows at start whether it is signed in.

## Screen reading

Geometry is in design pixels of a 2560x1440 screen, scaled to the real one
(16:9 assumed, centred otherwise), measured on the 5.4.4 client. Readers run on
worker threads and one recognition runs at a time (WinRT's OCR engine is shared).

Names are read first by the overlay's own recognizer (`namereader.py`,
`assets/namereader.onnx`, 3.5 MB, ONNX opset 9 so that Windows ML runs it from
Windows 10 2004 on): a small CRNN with CTC trained on synthetic lines only, drawn
in the fonts identified from the screens (Josefin Sans SemiBold on the draft,
Barlow Medium on the loading screen, Barlow Bold on the TAB and end boards) at
the pixel sizes of 720p to 4K screens, over backgrounds cut from real frames
(the loading screen's name band with the name painted out, the bar left of the
draft row being picked), with names of every shape (emoticons, look-alikes,
runs, punctuation, 1 to 16 characters). Glyphs those fonts lack are drawn as the
game draws them, from its fallback fonts (matched on real frames: Noto Sans for Greek, Cyrillic and superscripts at the
main font's size and weight, Noto Sans SC for CJK, Noto Sans Arabic, Noto Color
Emoji). It writes Latin and Cyrillic letters, and one GAP mark (U+FFFD) for each
run of glyphs it cannot write: emoji, symbols, CJK, Arabic, Greek letters with no
Latin twin, superscript, fullwidth and circled letters (charset.py, lookup._unit).
A Cyrillic or Greek letter drawn like a Latin one is written as the Latin letter,
and every key folds them alike (scoreboard._key). It runs on Windows ML on the
CPU, in pure Python with no numpy: about 5 ms a line, loaded when the game starts.
Each reading carries the probability of its text (the CTC sum over every path
that spells it):

* sure (at least 0.99): taken as it is, as two agreeing versions; Windows OCR is
  not asked for that box. A loading-screen line under 20 px high (screens below
  900p) is never sure;
* unsure: the names the line may hold (mine from the log, those read on an
  earlier screen, the board's team) are scored against it; one at least 0.2
  likely, at least as likely as the reading and 5 nats ahead of any other name is
  taken;
* otherwise the reading is one version next to Windows OCR's, and below 0.5 it
  is left out (a static screen read again would repeat it as false agreement).

Windows OCR still reads the draft's ALLY header and the TAB card's hero line.
Without an OCR language the recognizer reads the names alone (the log says so
once); without Windows ML or the model file, Windows OCR reads alone as before
(the log says that too).

Measured on 2026-11-19 (by the training project, which is not part of this repository) on frames of nine 1440p
matches, 751 name lines of other players: the recognizer (v8) reads the name right
for 97 % of them, and its searches find 96 %; every misread is a name in Greek or
CJK letters. On the three matches
kept out of its training it read every name. 83 % of the readings are sure, and
no sure reading named someone else; the only sure misreads are 12 lines of one name
in Greek letters with a hooked Latin letter, read as the plain Latin letters,
which match nobody. The same frames shrunk to 720p give 97 % as well (v7: 91 %).
On the draft rows being picked, v7 wrote a GAP for the bar on 8 of 23; v8 on none.
The model now shipped, v9 (2026-11-20), also learned loading lines over art at
full brightness, the role screen's plates, the mouse cursor on a name, and names
of spaced letters or dots. On the same 751 lines it reads as v8 did, with the
same sure misreads of that one name. It also read every name of the 257 lines from
eight matches captured after v8 was trained. On 10,011 synthetic lines it read
94.8 % against v8's 93.7 %, and plain names 95.5 % against 94.2 %.
A GAP-marked reading is searched by its longest piece and accepted only when the
rest matches with the gaps in place (lookup.gap_key); a reading with no mark is not
taken when Pred.gg has both a plain name and the same name with a symbol. Pred.gg's
search keeps scripts apart, so a reading with Cyrillic letters is also searched in
Cyrillic, and, for names that mix the scripts, by its longest part with no twin
letters. Names wholly in CJK, Arabic or Greek letters stay unfound: nothing in them
can be searched by. Pred.gg's search is also asked for the spellings OCR cannot
tell apart ("_" read as "-", "l" as "I" in Barlow).
Measured on 2026-11-18 frames of five ranked matches, against the names the TAB
board showed: the draft gave 18 of 20 names right, and after the loading screen
18 of 20 allies and 19 of 20 enemies were right (every row had a name; Windows OCR
alone, before the model). The model reads ten name lines in 60 to 80 ms on a current desktop CPU; Windows OCR, where
it is asked, adds about 7 ms a version.
When the loading screen went unread (the app started in the middle of a match, or
the game was not in front), the TAB board's rows give the missing players their
seats (see the seats above), and the board is read again to place them.

`--capture` (a hidden debug flag) saves full-screen frames of each phase to
`%LOCALAPPDATA%\PredOverlay\captures` to build and test these readers with.

## Records of matches

Started with `--record` (a development aid, off otherwise: the files hold other
players' names), the app records every match locally (`recorder.py`) in a folder of
`%LOCALAPPDATA%\PredOverlay\matches` (the newest 20): the core's state as the match
began, every event it took with its time and the commands it gave, and one frame of
the loading screen. The core is a pure reducer, so
`python -m predoverlay.overlay.replay <folder>` reruns a match exactly, and after a
change shows what the panel would have done. Checks judge each match (`replay.py`).
Two judge the core on what the recording gave it, whoever recorded it: a loading
screen or TAB board with every name in it seats every player, and a match takes at
most 12 searches. The others also depend on when the recording window read each
screen, so they fail only for a match this same version recorded: it replays as
recorded, every player had a seat within 15 s of the loading screen and at the first
TAB. `--all` runs them over every recorded match, a gate before a build is installed.
The files never leave the PC (not in the log, not in the diagnostics zip).

The loading screen is told from its pixels (`screens.py`: the MIDLANE label's white
glyphs and the midlane icon against masks, about 1 ms a look, twice a second from the
accept until the match starts). While they show it, it is read, whatever the log's
phase says; the log's phase still starts readings when the pixels are not looked at.
A reading waits for the panel's motion 0.3 s at most. While the game loads a map it can
stop answering for seconds, and Windows puts a "ghost" window in its place: that ghost
counts as the game in front (the screen still shows it).

The log has one line per screen reading, never a name: `read loading #2: 9 names,
7 sure, 410 ms -> taken` or `-> dropped (why)`; a draft reading like the previous one
is not repeated. When the loading screen is over, `loading over:` says how long it
showed, how many readings ran, the players it gave and why ticks read nothing.

Installs end the running app with `PredOverlayCLI --quit`, which asks it (through
the single-instance socket) to quit as from its tray menu, so `previous run ended
abruptly` keeps meaning a crash or a kill.

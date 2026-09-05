# TYR Uploader

Sends your Tyr replays to [TYR.pages] so your matches show up on the site.
You start it once and forget about it.

## What it does not do

This matters more than the feature list, so it goes first.

- **It does not load into Tyr.** No DLL, no injection, no plugin, nothing
  attached to the game process.
- **It does not read the game's memory.**
- **It does not draw anything on your screen while you play.** No overlay.
- **It does not touch anything except one folder.** It reads
  `%LOCALAPPDATA%\Tyr\Saved\Demos` and it writes a small settings file in
  `%APPDATA%\tyr-uploader`. That is the whole of its contact with your PC.
- **It does not send your whole replay history unless you ask it to.** The
  first time you press Start it writes down what is already in the folder and
  leaves all of it alone. Only matches you play from then on go up. There is a
  tick box if you want the old ones too, and it is off until you tick it.
- **It has no third-party dependencies.** Python's standard library and
  nothing else. That is on purpose: it is what makes the next section
  possible.

## Read it before you run it

Two files, and they are the whole program:

| File | Lines | What is in it |
|---|---|---|
| `tyr_uploader.py` | 634 | Everything that decides anything: which files to send, when, and what the site's answer meant. |
| `tyr_uploader_gui.py` | 479 | The window. Boxes, buttons and a log. It imports the first file and reimplements none of it. |

About a fifth of that is comments explaining why something is the way it is,
so there is rather less code than the line count suggests.

There is nothing else. No vendored packages, no compiled blob doing the real
work, no network code outside the one `upload()` function. If you want to know
what it sends, that function is about forty lines and the answer is "the bytes
of a .replay file, and a token that says who you are".

You are being asked to hand this program a credential. Ten minutes of reading
is a reasonable price for that, and the program is written to be readable in
ten minutes.

## Getting a token

1. Open [TYR.pages] and sign in with Steam.
2. Go to the **Upload** page.
3. Find the **Upload token** panel, below the drop zone.
4. Type a name for it — anything, it is just so you can tell them apart later —
   and press **Create token**.
5. The token is shown **once**. Copy it. The site stores only a hash of it and
   cannot show it to you again; if you lose it, make another.

Paste it into the token box in the uploader.

A token can **only upload**. It cannot read your data, cannot change your
privacy settings, and resolves to exactly one Steam account. If it leaks, you
revoke that one row on the same page and it is dead. That is the reason it is
a token and not your password.

## Running it

The .exe is not published yet, so for now the source route below is the one
that works. The .exe notes are here for when it is.

**With the .exe:** double-click `TyrUploader.exe`. Paste your token, check the
folder it found is right, press **Start**. Leave it running while you play.

To have it start with Windows, put a shortcut to it in
`Win+R` → `shell:startup`, and add `--start` to the shortcut's target so it
opens already watching.

**From source**, if you would rather run code you can see:

```
python tyr_uploader_gui.py            the same window
python tyr_uploader.py --token TOK    no window, prints to the console
python tyr_uploader.py --dry-run      list what it would send, send nothing
python tyr_uploader.py --once         send what is waiting, then exit
```

Python 3.8 or newer. Nothing to `pip install`.

## Windows will warn you the first time. Here is exactly what you will see.

The .exe is **not code signed** — a certificate costs real money every year and
this is a free tool for a game with a few thousand players. Unsigned means
Windows has never seen this file before and says so. Twice.

**1. When you download it.** Chrome or Edge will flag it as not commonly
downloaded. In Chrome, open the three-dot menu next to the download and choose
**Keep**; Edge is the same idea under **More** → **Keep**. You may have to
confirm a second time.

**2. When you run it.** A blue box appears:

> **Windows protected your PC**
> Microsoft Defender SmartScreen prevented an unrecognized app from starting.
> Running this app might put your PC at risk.

There is no visible Run button. Click **More info**, which expands to show the
app name and "Publisher: Unknown publisher", and *then* a **Run anyway** button
appears. That is the whole trick — the button is hidden behind the link.

**3. Possibly, a Properties → Unblock.** If Windows keeps objecting, right-click
the .exe, choose **Properties**, tick **Unblock** at the bottom of the General
tab, and press OK.

**We are not going to tell you to turn Defender off, add an exclusion, or
disable SmartScreen.** Anyone who tells you to do that for their tool is
telling you to lower your defences permanently to run one program. Click
through the warning for this one file instead, and only after you have decided
you trust it.

**If you have Smart App Control switched on** (a stricter Windows 11 feature,
off on most machines) it may refuse outright with no Run anyway option at all.
It cannot be bypassed per-app. In that case run from source instead — the
Python route above has no .exe and triggers none of this.

**Prefer not to click through a warning at all?** Run from source. It is the
same program, and you can read it first.

## What the log tells you

Every file gets one line saying what happened to it. The words are chosen
carefully, because "rejected" covers two completely different situations.

**Accepted** — it is on the site.

**Declined, in amber** — your file is fine and the site is not taking it. The
common one is:

> CUSTOM MATCH — a real, undamaged replay, declined because the site only
> collects matchmaking games. Nothing is wrong with this file

Custom lobbies have uneven rosters and often end early, so counting them would
skew the per-tank and per-map win rates the whole site is built on. **This is
not your replay being broken.** A normal matchmade game from the same folder
goes straight in.

**Rejected, in red** — something is actually wrong with the file: it is not a
replay, or it is damaged, or it is from a build the site cannot read yet. The
line says which.

**Waiting** — the site accepts 15 uploads per 5 minutes and 60 per hour. If you
have a backlog you will hit that, and the uploader will say how long it is
waiting and count down. It picks up exactly where it stopped. Nothing is lost
and nothing is sent twice.

## Where it keeps things

`%APPDATA%\tyr-uploader\`

- `config.ini` — your token and the folder you chose.
- `uploaded.json` — which files it has already dealt with, and what the site
  said about each. Delete it to make the uploader forget everything and start
  over.

Uninstalling is deleting the .exe. If you want it gone completely, delete that
folder too.

## Building the .exe yourself

```
pip install pyinstaller
python build_exe.py
```

Output is `dist/TyrUploader.exe`, about 11 MB, which is a whole Python
interpreter and Tk. PyInstaller is needed to *build* it and is not part of the
program — the thing you run still has no dependencies.

A build you do yourself is also the answer to "how do I know the .exe matches
the source". Build it and compare behaviour; the source is right here.

## Licence

MIT. See `LICENSE`.

[TYR.pages]: https://tyrpages.legomaster188.workers.dev

"""TYR.pages replay uploader.

Watches the folder Tyr writes replays into and sends new ones to TYR.pages.
That is the whole program. It does not load into the game, it does not read
the game's memory, and it does not draw anything on screen. It looks at one
folder and posts files.

    python tyr_uploader.py --token YOUR_TOKEN     watch and upload
    python tyr_uploader.py --once                 send what is waiting, exit
    python tyr_uploader.py --dry-run              show what it would send
    python tyr_uploader.py --send-existing        include replays already on disk

There is a window over this: tyr_uploader_gui.py, same functions, a token
box and a log. It imports this file rather than repeating any of it, so
whatever you can read here is the whole of what either front end does.

Get a token from the Upload page on the site while signed in with Steam.

Standard library only. Nothing to install beyond Python 3.
"""
import argparse
import configparser
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE = "https://tyrpages.legomaster188.workers.dev"
UPLOAD_PATH = "/api/verify"

# The game writes replays here. LOCALAPPDATA is per user, so it is resolved at
# runtime rather than baked in.
DEFAULT_REPLAY_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Tyr" / "Saved" / "Demos"

# The server refuses anything larger, before it even reads the body.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

# The server allows 15 uploads per 5 minutes and 60 per hour. Going slower than
# that on purpose, so a backlog trickles out instead of tripping the limit.
SECONDS_BETWEEN_UPLOADS = 25

# How long a file must stop changing before it counts as finished. A replay
# grows while the match is still running, so grabbing it the moment it appears
# gets you a truncated file. An earlier tool in this project skipped that check
# and 959 of the 1,213 files it captured were unusable mid-write snapshots.
SETTLE_SECONDS = 20
POLL_SECONDS = 15

STATE_NAME = "uploaded.json"

# Stamped on the rows the first-run baseline writes, and matched on exactly to
# undo it later. A literal because unbaseline() has to recognise rows written
# by an older copy of this program.
BASELINE_NOTE = "already on disk at first run"

# How long to wait before retrying when the server does not say. The origin
# does send retryAfterSeconds on a 429, so this is only for the case where it
# somehow does not.
DEFAULT_RETRY_SECONDS = 5 * 60

# Backoff for a file the server could not decide about -- a 500, a dropped
# connection, an upload that did not arrive intact. Those are all worth
# another go, but "another go" cannot mean every poll forever: at
# POLL_SECONDS that is an 8 MB file going up the wire four times a minute for
# as long as the program is open, which is the same hole the DISPOSITIONS
# note describes, reached through the other door.
#
# So each failure pushes the next attempt further out, and after
# MAX_RETRIES the file is written off with a note saying so. Written off is
# not silent: it says which file and why, and deleting the state file starts
# everything over.
RETRY_BACKOFF = (30, 2 * 60, 10 * 60, 30 * 60, 60 * 60)
MAX_RETRIES = len(RETRY_BACKOFF)


# ---------------------------------------------------------------------------
# What the server said, in words a player can act on.
#
# The verifier returns a typed outcome code and its own headline. Both are
# used: the headline because it is written for the person who uploaded the
# file and stays true if the server's wording changes, the code because this
# program has to DECIDE something about the file and cannot do that from prose.
#
# The decision is the disposition, and it is not the same question as "did it
# go on the site":
#
#   KEPT      on the site.
#   DECLINED  a real, undamaged replay the site does not collect. Nothing to
#             fix and nothing to retry.
#   REJECTED  the file itself did not pass. Retrying the same bytes will get
#             the same answer.
#   RETRY     nothing was decided about the file. Try it again later.
#
# KEPT, DECLINED and REJECTED are all TERMINAL: the file is written into the
# sent list and never offered again. Only RETRY leaves it in the queue.
#
# That distinction is the fix for a real bug. The old code recorded a file
# only when it was kept, so a declined one stayed a candidate forever and went
# back up the wire every poll -- every 15 seconds, ~8 MB at a time, until the
# rate limiter shut the account out. A custom match is the common way to hit
# it, because most people have one.
KEPT, DECLINED, REJECTED, RETRY = "kept", "declined", "rejected", "retry"

# Codes come from OUTCOME_RULES / POLICY_OUTCOMES in tools/replay_verify.py.
DISPOSITIONS = {
    # Accepted.
    "ACCEPTED": KEPT,
    "ACCEPTED_PARTIAL": KEPT,
    "CORROBORATED": KEPT,
    # Declined on policy. The file is fine. Saying otherwise is what cost us
    # a bug report from someone whose replay was perfectly good.
    "CUSTOM_MATCH": DECLINED,
    "PARTIAL_MATCH": DECLINED,
    "NOT_ELIGIBLE": DECLINED,
    "NOT_YOURS": DECLINED,
    # Something is wrong with the file.
    "NOT_A_REPLAY": REJECTED,
    "DAMAGED": REJECTED,
    "UNSUPPORTED": REJECTED,
    "INCONSISTENT": REJECTED,
    "CONTRADICTED": REJECTED,
    # The upload did not arrive intact. The verifier says so itself: nothing
    # is known to be wrong with the replay, so this one is worth another go.
    "UNREADABLE": RETRY,
}

# Said in this program's own voice, one line, for the log. The server's
# headline is used when it sends one; this is the fallback, and it is also
# what makes the log readable when the server is a version ahead or behind.
PLAIN = {
    "ACCEPTED": "accepted, it is on the site",
    "ACCEPTED_PARTIAL": "accepted; some checks could not run, which does not "
                        "count against the file",
    "CORROBORATED": "accepted and verified -- another player's recording of "
                    "the same match agrees",
    "CUSTOM_MATCH": "CUSTOM MATCH -- a real, undamaged replay, declined "
                    "because the site only collects matchmaking games. "
                    "Nothing is wrong with this file",
    "PARTIAL_MATCH": "not a full game -- a real, undamaged replay, declined "
                     "because the site only counts completed matches. "
                     "Nothing is wrong with this file",
    "NOT_ELIGIBLE": "a real replay, but not a match the site collects. "
                    "Nothing is wrong with this file",
    "NOT_YOURS": "recorded by another player, so it has to be uploaded by "
                 "them. Your own recording of the match will go in",
    "NOT_A_REPLAY": "not a Tyr replay file",
    "DAMAGED": "damaged or incomplete -- most often a copy that was cut "
               "short. The original in Saved\\Demos is the one to send",
    "UNSUPPORTED": "a replay from a build the site cannot read yet. Worth "
                   "reporting; this one is not your fault",
    "INCONSISTENT": "the file disagrees with itself",
    "CONTRADICTED": "disagrees with another recording of the same match",
    "UNREADABLE": "the upload did not arrive in one piece. Nothing is known "
                  "to be wrong with the replay",
    "REJECTED": "did not pass one of the checks",
}


class Result:
    """What one upload attempt came back with.

    Unpacks as (ok, message) so the two-value callers keep working, and
    carries the typed detail for anything that wants to act on it.
    """

    __slots__ = ("ok", "disposition", "code", "message", "retry_after")

    def __init__(self, ok, disposition, code, message, retry_after=None):
        self.ok = ok
        self.disposition = disposition
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def __iter__(self):
        return iter((self.ok, self.message))

    @property
    def terminal(self):
        """True when re-sending the same bytes cannot produce a different
        answer, so the file should be written off rather than queued again."""
        return self.disposition in (KEPT, DECLINED, REJECTED)

    def __repr__(self):
        return "Result(%s, %s, %r)" % (self.disposition, self.code, self.message)


def describe(code, headline="", detail=""):
    """One line for the log. The server's headline wins when there is one."""
    said = (headline or "").strip().rstrip(".")
    if code in ("CUSTOM_MATCH", "PARTIAL_MATCH", "NOT_ELIGIBLE"):
        # These are the ones people misread as breakage, so this program says
        # it in full rather than trusting a headline to carry it.
        return PLAIN[code]
    if said:
        return said + ((" -- " + detail.strip()) if detail else "")
    return PLAIN.get(code, code or "no reason given")


def _minutes(seconds):
    """'4 min 20 s' -- so a wait is a duration a person can plan around."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d s" % seconds
    if seconds % 60 == 0:
        return "%d min" % (seconds // 60)
    return "%d min %d s" % (seconds // 60, seconds % 60)


def _wait(seconds, should_stop=None):
    """sleep(), but a GUI's Stop button does not have to wait it out."""
    if not should_stop:
        time.sleep(seconds)
        return False
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_stop():
            return True
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))
    return should_stop()


def state_path(cfg_dir):
    return cfg_dir / STATE_NAME


def load_state(cfg_dir):
    p = state_path(cfg_dir)
    if not p.exists():
        return {"sent": {}, "retry": {}, "baselined": False}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data.get("sent"), dict):
            data.setdefault("baselined", False)
            # Added after "sent" existed, so a state file written by an
            # earlier copy has to keep working rather than crash on startup.
            if not isinstance(data.get("retry"), dict):
                data["retry"] = {}
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"sent": {}, "retry": {}, "baselined": False}


def save_state(cfg_dir, state):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_path(cfg_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path(cfg_dir))


def file_hash(path):
    """SHA1 of the contents, so a replay that was renamed or copied is not
    sent twice."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_settled(path):
    """True once the file has stopped changing and the game has let go of it."""
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0:
        return False
    if time.time() - st.st_mtime < SETTLE_SECONDS:
        return False
    # On Windows the game holds the file open while recording, so opening it
    # fails until it is finished. A stronger signal than mtime on its own.
    try:
        with open(path, "rb"):
            pass
    except OSError:
        return False
    return True


def upload(path, token, site, timeout=180, on_phase=None):
    """Send one replay. Returns a Result, which unpacks as (ok, message).

    The server takes the RAW BYTES with the filename in the query string, not
    a multipart form, and the query key is "name". It then streams back
    newline delimited JSON describing each check as it runs, with the verdict
    in the final "done" event. A 200 on its own only means the upload arrived,
    so the body has to be read to find out whether it was kept.

    on_phase, if given, is called with a short progress string as the server
    reports one. It is the only sign of life during a decode, which runs at
    roughly a second per megabyte, and a window with no sign of life for
    fifteen seconds looks broken.
    """
    url = site.rstrip("/") + UPLOAD_PATH + "?name=" + urllib.parse.quote(path.name)
    req = urllib.request.Request(
        url,
        data=path.read_bytes(),
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Authorization": "Bearer " + token,
            "User-Agent": "tyr-uploader/1.0",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            report = None
            # Read line by line rather than resp.read(): the whole point of
            # the ndjson is that the checks arrive while they run.
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")
                if kind == "error":
                    msg = ev.get("message") or ev.get("error") or "rejected"
                    return Result(False, RETRY, "SERVER_ERROR", msg)
                if kind == "phase" and on_phase:
                    on_phase(ev.get("label") or ev.get("phase") or "")
                elif kind == "check" and on_phase:
                    chk = ev.get("check") or {}
                    if chk.get("title"):
                        on_phase(chk["title"])
                if kind == "done":
                    report = ev.get("report") or {}
            if report is None:
                return Result(False, RETRY, "NO_VERDICT",
                              "the server stopped before it said anything")

            # report["outcome"] is the typed answer, added when the verifier
            # learned to say WHICH kind of no it was giving. report["verdict"]
            # is the older three-word field and is still the one that decides
            # whether the file was stored, so both are read: the verdict for
            # the fact, the outcome for the sentence.
            #
            # report["summary"] is a DICT of counts, not prose. Treating it as
            # prose is what used to raise TypeError here on every single
            # upload, inside a try that does not catch TypeError, so the watch
            # loop reported "cycle failed, will retry" and nothing was ever
            # recorded as sent.
            outcome = report.get("outcome") or {}
            code = outcome.get("code") or report.get("verdict") or "REJECTED"
            kept = report.get("verdict") != "REJECTED"
            disposition = DISPOSITIONS.get(code, KEPT if kept else REJECTED)
            text = describe(code, outcome.get("headline"), "")
            if kept and not report.get("stored", True):
                text += " (the server did not keep the file, though)"
            return Result(disposition == KEPT, disposition, code, text)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        wait = None
        try:
            parsed = json.loads(raw)
            msg = parsed.get("error") or raw[:200]
            wait = parsed.get("retryAfterSeconds")
        except json.JSONDecodeError:
            msg = raw[:200]
        if e.code == 429:
            # The origin sends Retry-After as well, so the header is the
            # fallback for the body and a fixed wait is the fallback for both.
            if not wait:
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
            wait = int(wait) if wait else DEFAULT_RETRY_SECONDS
            return Result(False, RETRY, "RATE_LIMITED",
                          "rate limited by the site (%s)" % msg, retry_after=wait)
        if e.code in (401, 403):
            return Result(False, REJECTED, "BAD_TOKEN",
                          "the site would not accept the token: %s" % msg)
        if e.code == 413:
            return Result(False, REJECTED, "TOO_BIG", msg)
        if 500 <= e.code < 600:
            # No "will try again" here: run_once() says when, and saying it
            # twice in one line reads like two different things happened.
            return Result(False, RETRY, "SERVER_DOWN",
                          "the site answered HTTP %s" % e.code)
        return Result(False, REJECTED, "HTTP_%s" % e.code,
                      "HTTP %s: %s" % (e.code, msg))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Result(False, RETRY, "OFFLINE", "connection failed: %s" % e)


def candidates(replay_dir, state):
    """New, finished replays, oldest first so a backlog goes out in order."""
    if not replay_dir.exists():
        return []
    now = time.time()
    retry = state.get("retry") or {}
    out = []
    for p in sorted(replay_dir.glob("*.replay"), key=lambda q: q.stat().st_mtime):
        if p.name in state["sent"]:
            continue
        if p.stat().st_size > MAX_UPLOAD_BYTES:
            continue
        if not is_settled(p):
            continue
        # Still serving a backoff from an earlier failure. See RETRY_BACKOFF.
        due = (retry.get(p.name) or {}).get("next", 0)
        if due and now < due:
            continue
        out.append(p)
    return out


def baseline(replay_dir, cfg_dir, state):
    """Mark everything already on disk as history.

    Installing this should not fire off your entire back catalogue at somebody
    who did not ask for it. Only matches played from now on get sent, unless
    --send-existing says otherwise.
    """
    n = 0
    for p in replay_dir.glob("*.replay"):
        if p.name not in state["sent"]:
            state["sent"][p.name] = {"hash": None, "at": int(time.time()),
                                     "result": BASELINE_NOTE}
            n += 1
    state["baselined"] = True
    save_state(cfg_dir, state)
    return n


def unbaseline(cfg_dir, state):
    """Undo a baseline, so 'send the ones already on disk' works after the
    first run rather than only during it.

    Only the rows the baseline itself wrote are removed, matched on the exact
    note it stamps. A file that was genuinely uploaded keeps its row and is
    not sent a second time -- asking for your back catalogue is not asking to
    duplicate what already went.
    """
    names = [k for k, v in state["sent"].items()
             if isinstance(v, dict) and v.get("result") == BASELINE_NOTE]
    for name in names:
        del state["sent"][name]
    state["baselined"] = False
    save_state(cfg_dir, state)
    return len(names)


def _printer(verbose):
    def log(level, text):
        if verbose or level in ("ok", "declined", "error"):
            print("[tyr] " + text)
    return log


def run_once(replay_dir, cfg_dir, token, site, dry_run=False, verbose=True,
             log=None, should_stop=None):
    """One pass over the folder. Returns {"sent": n, "retry_after": secs|None}.

    log(level, text) receives every line this would print. Levels are
    info / ok / declined / error, which is enough for a window to colour
    them and means the GUI never has to parse a sentence.
    """
    log = log or _printer(verbose)
    stop = should_stop or (lambda: False)
    state = load_state(cfg_dir)
    hashes = {v.get("hash") for v in state["sent"].values()
              if isinstance(v, dict) and v.get("hash")}
    todo = candidates(replay_dir, state)
    log("info", "%d new replay(s) in %s" % (len(todo), replay_dir))
    sent = 0
    for path in todo:
        if stop():
            break
        digest = file_hash(path)
        if digest in hashes:
            state["sent"][path.name] = {"hash": digest, "at": int(time.time()),
                                        "result": "same file under another name"}
            save_state(cfg_dir, state)
            log("info", "%s: already sent under another name" % path.name)
            continue
        if dry_run:
            log("info", "would send %s (%d KB)"
                % (path.name, path.stat().st_size // 1024))
            continue
        log("info", "sending %s (%d KB)..."
            % (path.name, path.stat().st_size // 1024))
        res = upload(path, token, site,
                     on_phase=lambda s: log("phase", "  " + s) if s else None)

        if res.disposition == RETRY:
            # Nothing was decided about this file, so it stays in the queue.
            # A rate limit ends the pass rather than walking the rest of the
            # list into the same wall: the limit is per account, not per file,
            # and it costs this file no attempt because nothing was tried.
            if res.code == "RATE_LIMITED":
                log("error", "%s: %s" % (path.name, res.message))
                return {"sent": sent, "retry_after": res.retry_after}
            if res.code == "BAD_TOKEN":
                log("error", "%s: %s" % (path.name, res.message))
                return {"sent": sent, "retry_after": None, "token_bad": True}

            row = dict(state["retry"].get(path.name) or {})
            tries = int(row.get("tries", 0)) + 1
            if tries >= MAX_RETRIES:
                # Out of attempts. Written into the sent list so it stops,
                # and said out loud so it is not a file that silently
                # vanished from the queue.
                state["retry"].pop(path.name, None)
                state["sent"][path.name] = {
                    "hash": digest, "at": int(time.time()), "outcome": res.code,
                    "result": "gave up after %d tries: %s" % (tries, res.message)}
                save_state(cfg_dir, state)
                log("error", "%s: gave up after %d tries (%s). Delete %s to "
                             "start this file over."
                    % (path.name, tries, res.message, state_path(cfg_dir)))
                continue
            back = RETRY_BACKOFF[min(tries - 1, len(RETRY_BACKOFF) - 1)]
            state["retry"][path.name] = {"tries": tries,
                                         "next": int(time.time()) + back}
            save_state(cfg_dir, state)
            log("error", "%s: %s. Trying again in %s (attempt %d of %d)."
                % (path.name, res.message, _minutes(back), tries + 1, MAX_RETRIES))
            if _wait(SECONDS_BETWEEN_UPLOADS, stop):
                break
            continue

        # KEPT, DECLINED and REJECTED are all final answers about these
        # bytes, so all three are written down. See DISPOSITIONS: recording
        # only the successes is what made a declined replay upload itself
        # forever.
        state["retry"].pop(path.name, None)      # settled; no backoff to keep
        state["sent"][path.name] = {"hash": digest, "at": int(time.time()),
                                    "outcome": res.code, "result": res.message}
        save_state(cfg_dir, state)
        hashes.add(digest)
        if res.disposition == KEPT:
            sent += 1
            log("ok", "%s: %s" % (path.name, res.message))
        elif res.disposition == DECLINED:
            # Deliberately NOT "FAILED". This file is fine.
            log("declined", "%s: %s" % (path.name, res.message))
        else:
            log("error", "%s: %s" % (path.name, res.message))
        if _wait(SECONDS_BETWEEN_UPLOADS, stop):
            break
    return {"sent": sent, "retry_after": None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--token", help="upload token from the site's Upload page")
    ap.add_argument("--dir", type=Path, default=None, help="replay folder to watch")
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--once", action="store_true", help="send what is waiting, then exit")
    ap.add_argument("--dry-run", action="store_true", help="list what would be sent")
    ap.add_argument("--send-existing", action="store_true",
                    help="also send replays that were already on disk")
    ap.add_argument("--config", type=Path, default=None,
                    help="where to keep the token and the sent list")
    args = ap.parse_args(argv)

    cfg_dir = args.config or (Path(os.environ.get("APPDATA", Path.home())) / "tyr-uploader")
    cfg_file = cfg_dir / "config.ini"

    token = args.token
    if not token and cfg_file.exists():
        cp = configparser.ConfigParser()
        cp.read(cfg_file, encoding="utf-8")
        token = cp.get("tyr", "token", fallback=None)
    if not token and not args.dry_run:
        print("No token yet. Make one on the Upload page while signed in with Steam,")
        print("then run:  python tyr_uploader.py --token YOUR_TOKEN")
        return 2
    if args.token:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cp = configparser.ConfigParser()
        cp["tyr"] = {"token": args.token}
        with open(cfg_file, "w", encoding="utf-8") as fh:
            cp.write(fh)
        print("[tyr] token saved to %s" % cfg_file)

    replay_dir = args.dir or DEFAULT_REPLAY_DIR
    if not replay_dir.exists():
        print("[tyr] replay folder not found: %s" % replay_dir)
        print("[tyr] pass --dir if the game is installed somewhere else")
        return 2

    state = load_state(cfg_dir)
    if args.send_existing and state["baselined"]:
        # Asked for after the baseline had already been taken, which used to
        # do nothing at all: the flag only ever suppressed the baseline, so
        # from the second run onwards it was silently ignored.
        n = unbaseline(cfg_dir, state)
        print("[tyr] --send-existing: %d replay(s) put back in the queue." % n)
        state = load_state(cfg_dir)
    if not state["baselined"] and not args.send_existing and not args.dry_run:
        n = baseline(replay_dir, cfg_dir, state)
        print("[tyr] first run: %d replay(s) already on disk marked as history." % n)
        print("[tyr] only matches from now on will be sent. --send-existing overrides.")

    if args.once or args.dry_run:
        run_once(replay_dir, cfg_dir, token, args.site, dry_run=args.dry_run)
        return 0

    print("[tyr] watching %s" % replay_dir)
    print("[tyr] leave this running. Ctrl+C to stop.")
    try:
        while True:
            wait = POLL_SECONDS
            try:
                cycle = run_once(replay_dir, cfg_dir, token, args.site, verbose=False)
                if cycle.get("retry_after"):
                    # Say when, not just that. A tool that goes quiet for five
                    # minutes reads as broken.
                    wait = cycle["retry_after"]
                    print("[tyr] rate limited; waiting %s before the next try"
                          % _minutes(wait))
            except Exception as exc:      # one bad cycle must not end the watch
                print("[tyr] cycle failed, will retry: %s" % exc)
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\n[tyr] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

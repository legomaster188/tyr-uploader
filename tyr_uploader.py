"""TYR.pages replay uploader.

Watches the folder Tyr drops replays into and posts new ones to TYR.pages.
That's it - it just reads one folder and sends files.

    python tyr_uploader.py --token YOUR_TOKEN     watch and upload
    python tyr_uploader.py --once                 send what's waiting, exit
    python tyr_uploader.py --dry-run              show what it would send
    python tyr_uploader.py --send-existing        include replays already on disk

Grab a token from the Upload page on the site, signed in with Steam.

Python 3, stdlib only.
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

# LOCALAPPDATA is per user so resolve at runtime
DEFAULT_REPLAY_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Tyr" / "Saved" / "Demos"

MAX_UPLOAD_BYTES = 16 * 1024 * 1024

# server caps at 15 per 5min / 60 per hour, staying well under
SECONDS_BETWEEN_UPLOADS = 25

# replays keep growing while the match runs, so grab one too early and you get
# a truncated file. wait for it to go quiet first.
SETTLE_SECONDS = 20
POLL_SECONDS = 15

STATE_NAME = "uploaded.json"


def state_path(cfg_dir):
    return cfg_dir / STATE_NAME


def load_state(cfg_dir):
    p = state_path(cfg_dir)
    if not p.exists():
        return {"sent": {}, "baselined": False}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data.get("sent"), dict):
            data.setdefault("baselined", False)
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"sent": {}, "baselined": False}


def save_state(cfg_dir, state):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_path(cfg_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path(cfg_dir))


def file_hash(path):
    """SHA1 of the contents, so a renamed or copied replay isn't sent twice."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_settled(path):
    """True once the file's stopped changing and the game has let go of it."""
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0:
        return False
    if time.time() - st.st_mtime < SETTLE_SECONDS:
        return False
    # windows keeps the file locked while recording, so a failed open is a
    # better signal than mtime alone
    try:
        with open(path, "rb"):
            pass
    except OSError:
        return False
    return True


def upload(path, token, site, timeout=180):
    """Send one replay. Returns (ok, message).

    Endpoint wants the raw bytes with the filename in the query string under
    "name" - not a multipart form. It replies with newline delimited JSON, one
    line per check, verdict in the final "done" event. A 200 only means the
    bytes arrived, so you have to read the body to see if it was kept.
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
            verdict, note = None, ""
            for line in resp.read().decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "error":
                    return False, ev.get("message") or ev.get("error") or "rejected"
                if ev.get("type") == "done":
                    report = ev.get("report") or {}
                    verdict = report.get("verdict")
                    note = report.get("summary") or ""
            if verdict is None:
                return False, "no verdict in the reply"
            # UNVERIFIED just means nobody else has uploaded that match yet.
            # normal, and still kept. only REJECTED is a refusal.
            return verdict != "REJECTED", verdict + (": " + note if note else "")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        wait = None
        try:
            parsed = json.loads(raw)
            msg = parsed.get("error") or raw[:200]
            wait = parsed.get("retryAfterSeconds")
        except json.JSONDecodeError:
            msg = raw[:200]
        if e.code == 429 and wait:
            return False, "rate limited, retry in %ss" % wait
        return False, "HTTP %s: %s" % (e.code, msg)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, "connection failed: %s" % e


def candidates(replay_dir, state):
    """New finished replays, oldest first so a backlog goes out in order."""
    if not replay_dir.exists():
        return []
    out = []
    for p in sorted(replay_dir.glob("*.replay"), key=lambda q: q.stat().st_mtime):
        if p.name in state["sent"]:
            continue
        if p.stat().st_size > MAX_UPLOAD_BYTES:
            continue
        if not is_settled(p):
            continue
        out.append(p)
    return out


def baseline(replay_dir, cfg_dir, state):
    """Mark whatever's already on disk as history.

    Installing this shouldn't dump your whole back catalogue on someone who
    didn't ask for it. --send-existing if you actually want that.
    """
    n = 0
    for p in replay_dir.glob("*.replay"):
        if p.name not in state["sent"]:
            state["sent"][p.name] = {"hash": None, "at": int(time.time()),
                                     "result": "already on disk at first run"}
            n += 1
    state["baselined"] = True
    save_state(cfg_dir, state)
    return n


def run_once(replay_dir, cfg_dir, token, site, dry_run=False, verbose=True):
    state = load_state(cfg_dir)
    hashes = {v.get("hash") for v in state["sent"].values()
              if isinstance(v, dict) and v.get("hash")}
    todo = candidates(replay_dir, state)
    if verbose:
        print("[tyr] %d new replay(s) in %s" % (len(todo), replay_dir))
    sent = 0
    for path in todo:
        digest = file_hash(path)
        if digest in hashes:
            state["sent"][path.name] = {"hash": digest, "at": int(time.time()),
                                        "result": "same file under another name"}
            save_state(cfg_dir, state)
            if verbose:
                print("[tyr] %s: already sent under another name" % path.name)
            continue
        if dry_run:
            print("[tyr] would send %s (%d KB)" % (path.name, path.stat().st_size // 1024))
            continue
        ok, msg = upload(path, token, site)
        if verbose:
            print("[tyr] %s: %s %s" % (path.name, "sent" if ok else "FAILED", msg))
        if ok:
            # only record successes - a failure stays off the skip list so the
            # next pass picks it up again
            state["sent"][path.name] = {"hash": digest, "at": int(time.time()),
                                        "result": msg}
            save_state(cfg_dir, state)
            hashes.add(digest)
            sent += 1
        time.sleep(SECONDS_BETWEEN_UPLOADS)
    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--token", help="upload token from the site's Upload page")
    ap.add_argument("--dir", type=Path, default=None, help="replay folder to watch")
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--once", action="store_true", help="send what's waiting, then exit")
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
            try:
                run_once(replay_dir, cfg_dir, token, args.site, verbose=False)
            except Exception as exc:      # one bad cycle shouldn't kill the watch
                print("[tyr] cycle failed, will retry: %s" % exc)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\n[tyr] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

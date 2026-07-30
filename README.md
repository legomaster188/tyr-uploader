# tyr-uploader

Watches the folder Tyr saves replays into and uploads new ones to
[TYR.pages](https://tyrpages.legomaster188.workers.dev). One file, stdlib only.

## What it isn't

Worth saying up front, since anything that touches a game folder gets side-eye:
this isn't a mod. Nothing injects, nothing hooks the game, nothing reads memory,
and there's no overlay. It watches a directory and POSTs `.replay` files. That's
the entire program, and it's short enough to read before you run it.

## Setup

You need Python 3.

1. Get an upload token from the Uploader page on the site while signed in with
   Steam. Tokens are upload-only - they can't read anyone's data or change your
   settings, and you can revoke one whenever you want.

2. Run it:

   ```
   python tyr_uploader.py --token YOUR_TOKEN
   ```

   It saves the token, so after that just:

   ```
   python tyr_uploader.py
   ```

Leave it running while you play. Polls every 15 seconds.

## Options

| Flag | What it does |
| --- | --- |
| `--token TOKEN` | Save your token. First run only. |
| `--dry-run` | List what it would send, send nothing. |
| `--once` | Send what's waiting, then exit. Handy for a scheduled task. |
| `--send-existing` | Also send replays already sitting on disk. |
| `--dir PATH` | Watch a different folder. |

## Waiting for a replay to finish

A replay file keeps growing until the match ends, so if you grab it as soon as
it shows up you get half a file. The uploader waits for two things: no changes
for 20 seconds, and the file opens cleanly (the game holds a lock on it while
recording).

I learned this the annoying way - an earlier version of this skipped the check
and most of what it collected was unusable.

## First run

First time you run it, anything already in the folder gets marked as history and
won't be sent. Installing this shouldn't dump your entire back catalogue on
people. Use `--send-existing` if that's actually what you want.

## Notes

- Replays over 16 MB get skipped, the server won't take them.
- Rate limit is 15 uploads per 5 minutes, 60 per hour. It paces itself under
  that so a backlog trickles out instead of tripping it.
- Failed uploads retry on the next pass. Only successes get recorded.
- A 200 doesn't mean accepted. The server streams back its checks and the
  verdict is on the last line. `REJECTED` is a refusal. `UNVERIFIED` just means
  nobody else has uploaded that match yet, which is fine and still kept.

## Licence

MIT, see `LICENSE`.

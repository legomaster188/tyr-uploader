"""A window over tyr_uploader.py.

Same program: it watches one folder and posts .replay files to TYR.pages. It
does not load into the game, it does not read the game's memory, and it does
not draw anything on screen while you play. This file adds a place to paste
your token, a folder picker and a log, because "run this Python script with a
--token argument" is not something most people are going to do.

    python tyr_uploader_gui.py

Everything that decides anything lives in tyr_uploader.py and is imported
from there. Nothing is reimplemented here, so reading that file tells you
what this one does -- and the command line front end still works exactly as
it did.

Tkinter, which ships with Python. No third-party packages, in either file.
That is deliberate: a program you are asked to trust with a credential should
be short enough to read in one sitting, and it cannot be if it pulls in a
dependency tree.
"""
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

import tyr_uploader as core

APP_NAME = "TYR Uploader"
TOKEN_HELP_URL = core.SITE + "/#/upload"

# Dark, because the site is and because this sits on a second monitor next to
# a game. Nothing here depends on the colours; a system-themed build would
# work identically.
BG = "#12141a"
PANEL = "#181b23"
FG = "#d6dae4"
DIM = "#7f889c"
EDGE = "#272c38"
ACCENT = "#6f8fe0"

# Log line colours, keyed by the level run_once() reports. DECLINED is
# deliberately NOT the error colour: a custom match is a good file the site
# does not collect, and painting it red is how somebody concludes their
# recording is broken and writes in to ask why.
LEVEL_COLOURS = {
    "info": FG,
    "phase": DIM,
    "ok": "#6fd39b",
    "declined": "#f0be5e",
    "error": "#e08a8a",
}


class UploaderWindow:
    def __init__(self, root, cfg_dir=None, site=None):
        self.root = root
        self.site = site or core.SITE
        self.cfg_dir = Path(cfg_dir) if cfg_dir else (
            Path(os.environ.get("APPDATA", Path.home())) / "tyr-uploader")
        self.cfg_file = self.cfg_dir / "config.ini"

        # The worker thread never touches a widget. It posts here and the Tk
        # main loop drains it on a timer -- Tk is not thread safe, and a
        # background thread calling .insert() is the classic way to get a
        # freeze that only happens on someone else's machine.
        self.events = queue.Queue()
        self.worker = None
        self.stopping = threading.Event()
        self.retry_at = None       # monotonic deadline while rate limited

        self._build()
        self._load_settings()
        self.root.after(100, self._drain)
        self.root.after(1000, self._tick)

    # ---- layout ---------------------------------------------------------

    def _build(self):
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=BG)
        r.minsize(720, 520)
        r.geometry("820x620")
        r.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("T.TCheckbutton", background=PANEL, foreground=FG,
                        focuscolor=PANEL)
        style.map("T.TCheckbutton", background=[("active", PANEL)])

        wrap = tk.Frame(r, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)

        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="TYR Uploader", bg=BG, fg=FG,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(head, text="watches one folder, sends new replays",
                 bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(side="left",
                                                           padx=(10, 0), pady=(6, 0))

        # ---- token ----
        tok = self._panel(wrap, "Upload token")
        row = tk.Frame(tok, bg=PANEL)
        row.pack(fill="x")
        self.token_var = tk.StringVar()
        self.token_entry = tk.Entry(
            row, textvariable=self.token_var, show="•", bg="#0e1016",
            fg=FG, insertbackground=FG, relief="flat", font=("Consolas", 10),
            highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        self.token_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Show", variable=self.show_var,
                        style="T.TCheckbutton",
                        command=self._toggle_token).pack(side="left", padx=(8, 0))
        self._button(row, "Paste", self._paste).pack(side="left", padx=(6, 0))

        help_row = tk.Frame(tok, bg=PANEL)
        help_row.pack(fill="x", pady=(7, 0))
        tk.Label(help_row, bg=PANEL, fg=DIM, justify="left",
                 font=("Segoe UI", 9), text=(
                     "Sign in with Steam on the site, open the Upload page, and in the "
                     "Upload token panel\nname the token and press Create token. The "
                     "value is shown once. Copy it and paste it here.")
                 ).pack(side="left")
        link = tk.Label(help_row, text="Open the Upload page", bg=PANEL,
                        fg=ACCENT, cursor="hand2", font=("Segoe UI", 9, "underline"))
        link.pack(side="right", anchor="n")
        link.bind("<Button-1>", lambda _e: webbrowser.open(TOKEN_HELP_URL))

        # ---- folder ----
        fol = self._panel(wrap, "Replay folder")
        row = tk.Frame(fol, bg=PANEL)
        row.pack(fill="x")
        self.dir_var = tk.StringVar(value=str(core.DEFAULT_REPLAY_DIR))
        self.dir_entry = tk.Entry(
            row, textvariable=self.dir_var, bg="#0e1016", fg=FG,
            insertbackground=FG, relief="flat", font=("Consolas", 9),
            highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        self.dir_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._button(row, "Browse", self._browse).pack(side="left", padx=(6, 0))
        self.dir_note = tk.Label(fol, bg=PANEL, fg=DIM, font=("Segoe UI", 9),
                                 anchor="w", justify="left")
        self.dir_note.pack(fill="x", pady=(7, 0))
        self.dir_var.trace_add("write", lambda *_a: self._check_dir())

        # The one setting that can surprise somebody, so it says what it does
        # in full rather than in a three-word label.
        self.send_existing = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            fol, style="T.TCheckbutton", variable=self.send_existing,
            command=self._check_dir,
            text=("Also send the replays already in this folder "
                  "(off by default: only matches from now on)")
        ).pack(anchor="w", pady=(9, 0))

        # ---- controls ----
        ctrl = tk.Frame(wrap, bg=BG)
        ctrl.pack(fill="x", pady=(12, 0))
        self.start_btn = self._button(ctrl, "Start", self._toggle_run, big=True)
        self.start_btn.pack(side="left")
        self.status = tk.Label(ctrl, text="Stopped.", bg=BG, fg=DIM,
                               font=("Segoe UI", 10), anchor="w")
        self.status.pack(side="left", padx=(12, 0))
        self._button(ctrl, "Copy log", self._copy_log).pack(side="right")

        # ---- log ----
        logwrap = tk.Frame(wrap, bg=EDGE, highlightthickness=0)
        logwrap.pack(fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(logwrap, bg="#0e1016", fg=FG, relief="flat",
                           font=("Consolas", 9), wrap="word", padx=10, pady=8,
                           state="disabled", height=12)
        bar = tk.Scrollbar(logwrap, command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        for level, colour in LEVEL_COLOURS.items():
            self.log.tag_configure(level, foreground=colour)

        self._say("info", "%s. Nothing is sent until you press Start." % APP_NAME)
        self._say("info", "This program reads one folder and posts .replay "
                          "files. It does not touch the game.")

    def _panel(self, parent, title):
        outer = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                         highlightbackground=EDGE)
        outer.pack(fill="x", pady=(12, 0))
        inner = tk.Frame(outer, bg=PANEL)
        inner.pack(fill="x", padx=12, pady=10)
        tk.Label(inner, text=title, bg=PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 7))
        return inner

    def _button(self, parent, text, command, big=False):
        return tk.Button(
            parent, text=text, command=command, relief="flat", bd=0,
            bg=ACCENT if big else "#252a36", fg="#0e1016" if big else FG,
            activebackground=ACCENT if big else "#2f3542",
            activeforeground="#0e1016" if big else FG, cursor="hand2",
            font=("Segoe UI", 10, "bold") if big else ("Segoe UI", 9),
            padx=18 if big else 12, pady=6 if big else 4)

    # ---- settings -------------------------------------------------------

    def _load_settings(self):
        import configparser
        if self.cfg_file.exists():
            cp = configparser.ConfigParser()
            try:
                cp.read(self.cfg_file, encoding="utf-8")
                self.token_var.set(cp.get("tyr", "token", fallback="") or "")
                saved = cp.get("tyr", "dir", fallback="") or ""
                if saved:
                    self.dir_var.set(saved)
            except (configparser.Error, OSError):
                pass
        if self.token_var.get():
            self._say("info", "Token loaded from %s" % self.cfg_file)
        self._check_dir()

    def _save_settings(self):
        import configparser
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        cp = configparser.ConfigParser()
        cp["tyr"] = {"token": self.token_var.get().strip(),
                     "dir": self.dir_var.get().strip()}
        with open(self.cfg_file, "w", encoding="utf-8") as fh:
            cp.write(fh)

    # ---- small handlers -------------------------------------------------

    def _toggle_token(self):
        self.token_entry.configure(show="" if self.show_var.get() else "•")

    def _paste(self):
        try:
            self.token_var.set(self.root.clipboard_get().strip())
        except tk.TclError:
            self._say("error", "Nothing on the clipboard to paste.")

    def _browse(self):
        start = self.dir_var.get() or str(core.DEFAULT_REPLAY_DIR)
        picked = filedialog.askdirectory(title="Where Tyr saves replays",
                                         initialdir=start)
        if picked:
            self.dir_var.set(os.path.normpath(picked))

    def _copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))
        self._say("info", "Log copied to the clipboard.")

    def _check_dir(self):
        """Say what is in the chosen folder, and what pressing Start will do
        with it. Guessing is the part people get wrong."""
        d = Path(self.dir_var.get().strip() or ".")
        if not d.exists():
            self.dir_note.configure(
                fg=LEVEL_COLOURS["error"],
                text="That folder does not exist. Tyr's own is "
                     "%LOCALAPPDATA%\\Tyr\\Saved\\Demos.")
            return
        try:
            n = len(list(d.glob("*.replay")))
        except OSError:
            n = 0
        if self.send_existing.get():
            note = ("%d replay(s) here. All of them will be sent, plus new "
                    "matches as they finish." % n)
            colour = LEVEL_COLOURS["declined"]
        else:
            note = ("%d replay(s) here. These are left alone; only matches "
                    "played from now on are sent." % n)
            colour = DIM
        self.dir_note.configure(text=note, fg=colour)

    # ---- running --------------------------------------------------------

    def start_if_ready(self):
        """Begin watching without a click, for --start. Refuses in exactly the
        same way the button does, and says so in the log."""
        if self.worker and self.worker.is_alive():
            return
        self._say("info", "Started automatically (--start).")
        self._toggle_run()

    def _toggle_run(self):
        if self.worker and self.worker.is_alive():
            self.stopping.set()
            self.start_btn.configure(text="Stopping", state="disabled")
            self.status.configure(text="Finishing the current file...")
            return

        token = self.token_var.get().strip()
        replay_dir = Path(self.dir_var.get().strip())
        if not token:
            self._say("error", "Paste your upload token first. It is on the "
                               "site's Upload page, in the Upload token panel.")
            self.token_entry.focus_set()
            return
        if not token.startswith("tyr_"):
            # Checked here rather than at the server, because the answer to
            # "why did it not work" should not cost a round trip and a 401.
            self._say("error", "That does not look like an upload token. They "
                               "start with tyr_ and come from the Upload page.")
            return
        if not replay_dir.exists():
            self._say("error", "Replay folder not found: %s" % replay_dir)
            return

        try:
            self._save_settings()
        except OSError as exc:
            self._say("error", "Could not save settings: %s" % exc)

        self.stopping.clear()
        self.retry_at = None
        self.start_btn.configure(text="Stop", state="normal")
        self.status.configure(text="Watching.", fg=LEVEL_COLOURS["ok"])
        # Read on THIS thread and handed over as a plain bool. A Tk variable
        # is a Tcl interpreter call, and calling one from the worker raises
        # "main thread is not in main loop" -- which killed the whole watch
        # silently, because a dead thread does not report itself.
        self.worker = threading.Thread(
            target=self._run,
            args=(token, replay_dir, bool(self.send_existing.get())),
            daemon=True)
        self.worker.start()

    def _run(self, token, replay_dir, send_existing):
        """The watch loop, on its own thread. Everything it wants to show goes
        through self.events; nothing here touches a widget or a Tk variable."""
        post = lambda level, text: self.events.put(("log", level, text))
        try:
            state = core.load_state(self.cfg_dir)
            if send_existing and state["baselined"]:
                n = core.unbaseline(self.cfg_dir, state)
                post("info", "Queued %d replay(s) that were already on disk." % n)
                state = core.load_state(self.cfg_dir)
            if not state["baselined"] and not send_existing:
                # The default, and the promise the README makes. Clicking
                # Start must not mail somebody's back catalogue at the site.
                n = core.baseline(replay_dir, self.cfg_dir, state)
                post("info", "First run: %d replay(s) already on disk were "
                             "marked as history and will NOT be sent." % n)
                post("info", "Only matches from now on go up. Tick the box "
                             "above if you want the old ones too.")

            post("info", "Watching %s" % replay_dir)
            while not self.stopping.is_set():
                try:
                    cycle = core.run_once(
                        replay_dir, self.cfg_dir, token, self.site,
                        log=post, should_stop=self.stopping.is_set)
                except Exception as exc:
                    # One bad cycle must not end the watch. Same rule as the
                    # command line loop, for the same reason.
                    post("error", "That pass failed, will try again: %s" % exc)
                    cycle = {"sent": 0, "retry_after": None}

                if cycle.get("token_bad"):
                    post("error", "The site rejected the token. Make a new one "
                                  "on the Upload page and paste it in.")
                    break
                wait = cycle.get("retry_after")
                if wait:
                    # The origin allows 15 uploads per 5 minutes and 60 per
                    # hour and tells us how long it wants. Saying WHEN is the
                    # difference between waiting and looking broken.
                    post("declined",
                         "Rate limited by the site. Waiting %s, then carrying "
                         "on where it left off. Nothing is lost."
                         % core._minutes(wait))
                    self.events.put(("retry", wait, None))
                else:
                    wait = core.POLL_SECONDS
                if core._wait(wait, self.stopping.is_set):
                    break
        except Exception as exc:
            # A thread that dies takes the watch with it and says nothing, so
            # the window would sit there reading "Watching." forever. Anything
            # that escapes the loop above gets shown instead.
            post("error", "The uploader stopped with an unexpected error: %r" % exc)
        finally:
            self.events.put(("stopped", None, None))

    # ---- the Tk side of the queue ---------------------------------------

    def _drain(self):
        try:
            while True:
                kind, a, b = self.events.get_nowait()
                if kind == "log":
                    self._say(a, b)
                elif kind == "retry":
                    import time as _t
                    self.retry_at = _t.monotonic() + a
                elif kind == "stopped":
                    self.retry_at = None
                    self.start_btn.configure(text="Start", state="normal")
                    self.status.configure(text="Stopped.", fg=DIM)
                    self._say("info", "Stopped. Nothing is being watched.")
        except queue.Empty:
            pass
        self.root.after(150, self._drain)

    def _tick(self):
        """Count the rate-limit wait down in the status line, so the window is
        visibly doing something during a five minute pause."""
        if self.retry_at is not None:
            import time as _t
            left = self.retry_at - _t.monotonic()
            if left > 0:
                self.status.configure(
                    text="Rate limited. Next try in %s." % core._minutes(left),
                    fg=LEVEL_COLOURS["declined"])
            else:
                self.retry_at = None
                self.status.configure(text="Watching.", fg=LEVEL_COLOURS["ok"])
        self.root.after(1000, self._tick)

    def _say(self, level, text):
        import time as _t
        stamp = _t.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", "%s  %s\n" % (stamp, text),
                        level if level in LEVEL_COLOURS else "info")
        # Only follow the tail when the reader is already at it. Yanking the
        # view back down while somebody is reading a rejection is rude.
        if self.log.yview()[1] > 0.98:
            self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self):
        self.stopping.set()
        self.root.destroy()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg_dir = site = None
    # --start is for a shortcut in the Startup folder: open already watching,
    # using the token and folder saved last time, so the answer to "do I have
    # to remember to run this" is no. --config and --site are here so the
    # window can be pointed at a test server without a rebuild.
    auto = "--start" in argv
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            cfg_dir = argv[i + 1]
        if arg == "--site" and i + 1 < len(argv):
            site = argv[i + 1]

    root = tk.Tk()
    try:
        ico = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "tyr.ico"
        if ico.exists():
            root.iconbitmap(str(ico))
    except tk.TclError:
        pass
    win = UploaderWindow(root, cfg_dir=cfg_dir, site=site)
    if auto:
        # After the window exists, so a refusal (no token, missing folder)
        # is reported into the log rather than thrown at a console nobody
        # is looking at.
        root.after(400, win.start_if_ready)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build TyrUploader.exe -- one file, no installer, no Python needed to run it.

    python build_exe.py                 build dist/TyrUploader.exe
    python build_exe.py --publish       ...and copy it into site/uploader/
    python build_exe.py --publish-only  copy the sources into site/uploader/

Output: dist/TyrUploader.exe, around 10 MB, which is a whole Python plus Tk.

WHY ONE FILE AND NOT AN INSTALLER
---------------------------------
An installer is the wrong shape for this program. It writes nothing but a
config file in %APPDATA%, has no services, no file associations and no uninstall
worth running, so an installer would exist only to move one .exe somewhere and
then need removing later. A single file people drop in a folder is also the
easier thing to TRUST: you can see exactly what you have, you can delete it by
deleting it, and there is no second binary doing unobserved work at install
time. It costs a Start Menu entry, which is a fair trade.

It does not change the SmartScreen story either way -- an unsigned Inno Setup
installer is an unsigned executable exactly like this one is, and gets the same
warning. See README.md.

PyInstaller is a BUILD dependency, not a runtime one. The program itself is
still standard library only, which is the property that lets somebody read it
before running it.
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "tyr_uploader_gui.py"
ICON = HERE / "tyr.ico"
NAME = "TyrUploader"


def ensure_icon():
    """Draw the icon if it is missing.

    Committed to the repo, so this almost never runs and Pillow is not needed
    for a normal build. It is here so a fresh clone that somehow lacks the
    .ico can still build rather than failing on a missing asset.
    """
    if ICON.exists():
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[build] no tyr.ico and Pillow is not installed; "
              "building without an icon")
        return
    size = 256
    img = Image.new("RGBA", (size, size), (18, 20, 26, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, size - 8, size - 8], radius=44,
                        fill=(24, 27, 35, 255), outline=(48, 56, 78, 255), width=5)
    blue = (111, 143, 224, 255)
    d.polygon([(128, 52), (196, 126), (154, 126), (154, 150),
               (102, 150), (102, 126), (60, 126)], fill=blue)
    d.rounded_rectangle([64, 172, 192, 200], radius=10, fill=(96, 106, 132, 255))
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    print("[build] wrote %s" % ICON)


# What site/uploader/ is allowed to contain, and where each file comes from.
# The download page links these by name; nothing else is copied.
#
# site/uploader/ used to hold a HAND-COPY of tyr_uploader.py, which is the
# failure mode AGENTS.md describes for the public repos: the copy drifts from
# the source and the site ends up advertising an old version of the program.
# Copying it from here makes the download a build output rather than something
# somebody has to remember to update.
PUBLISH = ("tyr_uploader.py", "tyr_uploader_gui.py", "README.md", "LICENSE")
SITE_DIR = HERE.parent / "site" / "uploader"


def publish(with_exe):
    if not SITE_DIR.exists():
        print("[build] no %s -- skipping publish" % SITE_DIR)
        return
    for name in PUBLISH:
        src = HERE / name
        if not src.exists():
            print("[build] missing %s, not copied" % name)
            continue
        shutil.copy2(src, SITE_DIR / name)
        print("[build] -> site/uploader/%s" % name)
    if with_exe:
        exe = HERE / "dist" / (NAME + ".exe")
        if exe.exists():
            shutil.copy2(exe, SITE_DIR / exe.name)
            print("[build] -> site/uploader/%s (%.1f MB)"
                  % (exe.name, exe.stat().st_size / (1024 * 1024)))
    print("[build] site/uploader/ is a DEPLOY artifact. Nothing here reaches "
          "anyone until deploy_site runs, which needs the owner's sign-off.")


def main():
    argv = sys.argv[1:]
    if "--publish-only" in argv:
        publish(with_exe=False)
        return 0
    if not ENTRY.exists():
        print("[build] missing %s" % ENTRY)
        return 1
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[build] PyInstaller is not installed. Run:")
        print("           pip install pyinstaller")
        return 1

    ensure_icon()
    for d in (HERE / "build", HERE / "dist"):
        shutil.rmtree(d, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        # No console window behind the GUI. The log is in the window.
        "--noconsole",
        "--name", NAME,
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE / "build"),
        "--noconfirm",
        # tyr_uploader.py is imported by name, and PyInstaller follows that
        # fine, but naming it keeps the build honest if the import ever moves
        # inside a function.
        "--hidden-import", "tyr_uploader",
        "--paths", str(HERE),
        # Everything below is absent from a standard library program and would
        # otherwise be dragged in by whatever happens to be installed on the
        # build machine. Excluding them is what keeps this ~10 MB rather than
        # ~60 MB, and it also means the shipped binary cannot contain a
        # package the source never mentions -- which is the sort of thing
        # somebody checking this program has a right to be able to rule out.
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        "--exclude-module", "pandas",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "PySide6",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "pytest",
        "--exclude-module", "setuptools",
        "--exclude-module", "pip",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "test",
    ]
    if ICON.exists():
        cmd += ["--icon", str(ICON)]
        # So the running window has the icon too, not just the file. onefile
        # unpacks to sys._MEIPASS, which is where the GUI looks for it.
        cmd += ["--add-data", "%s%s." % (ICON, ";" if sys.platform == "win32" else ":")]
    cmd.append(str(ENTRY))

    print("[build] " + " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        print("[build] PyInstaller failed (%s)" % rc)
        return rc

    out = HERE / "dist" / (NAME + (".exe" if sys.platform == "win32" else ""))
    if not out.exists():
        print("[build] expected %s and it is not there" % out)
        return 1
    mb = out.stat().st_size / (1024 * 1024)
    print("\n[build] %s  (%.1f MB)" % (out, mb))
    print("[build] It is UNSIGNED, so Windows will warn about it the first "
          "time somebody runs it.")
    print("[build] README.md says what they will see. Do not tell anyone to "
          "turn protections off.")
    if "--publish" in sys.argv[1:]:
        print()
        publish(with_exe=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build the distributable EQ-Forge-2.0.zip from the current source tree.

    python tools/build_zip.py [-o path\\to\\EQ-Forge-2.0.zip]

Default output: ..\\active\\output\\EQ-Forge-2.0.zip (the usual spot). An existing
zip at that path is renamed <name>_prev-YYYY-MM-DD.zip instead of being overwritten.

What ships: the app, serve.py + run.bat, items.txt.gz, docs, extras (MQ Lua), tools,
and the mychars package. What never ships: mychars.db (his roster + any local data),
eqforge_settings.json (one machine's resolved folder paths — the recipient's install
auto-detects its own, and a stale override would beat the detection),
__pycache__, CLAUDE.md (dev notes), and app/toon-profiles.json — that one is written
out EMPTY, exactly as the first release did, so nobody inherits someone else's toons.
"""
import argparse, datetime, os, shutil, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = "EQ-Forge-2.0"
DEFAULT_OUT = os.path.join(ROOT, "..", "active", "output", "EQ-Forge-2.0.zip")

ROOT_FILES = ["README.md", "GETTING-STARTED.md", "MQ-SETUP.md", "CREDITS.md",
              "LICENSE", "run.bat", "serve.py", "items.txt.gz"]
DIRS = ["app", "docs", "extras", "tools", "mychars"]

SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules",
             # Dev-only, and every fixture in them is built from the real roster
             # (character names, account layout, who owns a Dragon's Hoard). They are
             # not part of the running program, so shipping them only leaks the
             # roster of whoever built the zip.
             "tests"}
SKIP_EXT = {".pyc", ".pyo", ".db", ".log"}
SKIP_FILES = {"CLAUDE.md", "AGENTS.md", "GEMINI.md", ".gitignore", "mychars.db",
              "eqforge_settings.json",
              # Generated from YOUR OWN EQ client's spells_us.txt / dbstr_us.txt. The
              # contents are Daybreak's game text, which is why the repo gitignores
              # them - but this script walks the filesystem, not git, so they have to
              # be named here too or every zip redistributes them. Recipients run
              # tools/build_spells.py against their own install; both loaders are
              # non-fatal, so without them effect labels just degrade to "Combat Proc".
              "spell-effects.json.gz", "focus-families.json"}
BLANKED = {"app/toon-profiles.json": "{}\n"}          # personal → shipped empty


def wanted(rel):
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts):
        return False
    if parts[-1] in SKIP_FILES:
        return False
    return os.path.splitext(rel)[1].lower() not in SKIP_EXT


def collect():
    """[(relative path, absolute path or None-for-blanked)] in a stable order."""
    out = []
    for f in ROOT_FILES:
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            out.append((f, p))
        else:
            print(f"  ! missing {f}")
    for d in DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(n for n in dirnames if n not in SKIP_DIRS)
            for name in sorted(filenames):
                ap = os.path.join(dirpath, name)
                rel = os.path.relpath(ap, ROOT).replace("\\", "/")
                if wanted(rel):
                    out.append((rel, ap))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    ap.add_argument("--slim", action="store_true",
                    help="leave out items.txt.gz (~11MB); serve.py downloads it on "
                         "first run. Result fits Discord's 10MB attachment limit.")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    if args.slim and out == os.path.abspath(DEFAULT_OUT):
        out = os.path.join(os.path.dirname(out), "EQ-Forge-2.0-slim.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    if os.path.exists(out):
        stamp = datetime.date.today().isoformat()
        prev = f"{os.path.splitext(out)[0]}_prev-{stamp}.zip"
        n = 2
        while os.path.exists(prev):
            prev = f"{os.path.splitext(out)[0]}_prev-{stamp}-{n}.zip"
            n += 1
        shutil.move(out, prev)
        print(f"kept the old build as {prev}")

    files = collect()
    if args.slim:
        files = [(rel, ap_) for rel, ap_ in files if rel != "items.txt.gz"]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, ap in files:
            arc = f"{TOP}/{rel}"
            if rel in BLANKED:
                z.writestr(arc, BLANKED[rel])
            else:
                z.write(ap, arc)
    print(f"{out}\n  {len(files)} files · {os.path.getsize(out) / 1048576:.1f} MB")


if __name__ == "__main__":
    main()

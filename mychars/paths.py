"""Where MacroQuest and EverQuest live, resolved without asking the user.

Every MQ install sits somewhere different (redfetch, VanillaMQ, a hand build) and
EverQuest moves around too. Before this module both paths were hardcoded to one
machine and overridable only by an environment variable, so on anyone else's PC
the MQ-facing features (lockout import, gear plans, harvest, roster exports)
silently found nothing and reported it as an empty result.

Resolution order, most explicit first:

  1. saved      eqforge_settings.json next to serve.py  (EQ Forge -> Setup)
  2. env        EQFORGE_MQ_CONFIG / EQFORGE_EQ_DIR
  3. beacon     %LOCALAPPDATA%\\eqforge_paths.json, written by the MQ addon
  4. scan       a bounded look through the usual install locations
  5. default    the historical hardcoded paths, so nothing regresses

The BEACON is the one that makes this work with no configuration at all: the MQ
addon knows mq.configDir and EverQuest.Path for certain, and writes them to a
location a plain Python process can always find. Run `/lua run eqforge` once and
the server knows where everything is.

Stdlib only, like the rest of the app.
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SETTINGS_PATH = os.path.join(ROOT, "eqforge_settings.json")
BEACON_NAME = "eqforge_paths.json"

# Last-resort placeholders, used only when the saved override, the env var, the
# addon's beacon AND the scan have all come up empty. Deliberately generic: a real
# machine's path here would be both a privacy leak and a worse guess than the scan.
DEFAULT_MQ_CONFIG = r"C:\MacroQuest\config"
DEFAULT_EQ_DIR = r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest"

# Proof a candidate directory really is what we think it is. Scanning for a
# *name* finds empty lookalikes; scanning for a marker file does not.
MQ_MARKERS = ("MacroQuest.ini", "MacroQuest.exe")
EQ_MARKERS = ("eqgame.exe", "eqclient.ini")


# ---------------------------------------------------------------------------
# saved overrides
# ---------------------------------------------------------------------------
def load_overrides():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_overrides(mq_config=None, eq_dir=None):
    """Store (or clear, with "") the UI overrides. Returns the saved dict."""
    data = load_overrides()
    for key, value in (("mq_config", mq_config), ("eq_dir", eq_dir)):
        if value is None:
            continue
        value = (value or "").strip()
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


# ---------------------------------------------------------------------------
# beacon
# ---------------------------------------------------------------------------
def beacon_candidates():
    out = []
    for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(var)
        if base:
            out.append(os.path.join(base, BEACON_NAME))
    return out


def read_beacon():
    """The MQ addon's last report: {mq_config, eq_path, server, character, written}."""
    for path in beacon_candidates():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("mq_config"):
            data["_path"] = path
            return data
    return None


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def _has_marker(directory, markers):
    if not directory or not os.path.isdir(directory):
        return False
    try:
        names = {n.lower() for n in os.listdir(directory)}
    except OSError:
        return False
    return any(m.lower() in names for m in markers)


def _drives():
    out = []
    for letter in "CDEFG":
        root = "%s:\\" % letter
        if os.path.isdir(root):
            out.append(root)
    return out


def scan_mq_config():
    """A config dir is valid if it, or its parent, holds a MacroQuest marker."""
    home = os.environ.get("USERPROFILE", "")
    patterns = []
    for base in filter(None, [
        r"C:\Users\Public\redfetch\Downloads",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "redfetch") if os.environ.get("LOCALAPPDATA") else None,
        os.path.join(home, "Downloads") if home else None,
        os.path.join(home, "Documents") if home else None,
        home or None,
    ]):
        patterns.append(os.path.join(base, "config"))
        patterns.append(os.path.join(base, "*", "config"))
    for drive in _drives():
        patterns.append(os.path.join(drive, "MQ*", "config"))
        patterns.append(os.path.join(drive, "MacroQuest*", "config"))
        patterns.append(os.path.join(drive, "Vanilla*", "config"))
        patterns.append(os.path.join(drive, "*", "MacroQuest*", "config"))

    seen = []
    for pattern in patterns:
        try:
            hits = glob.glob(pattern)
        except OSError:
            continue
        for hit in hits:
            if hit in seen or not os.path.isdir(hit):
                continue
            if _has_marker(hit, MQ_MARKERS) or _has_marker(os.path.dirname(hit), MQ_MARKERS):
                seen.append(hit)
    # Newest wins: someone with several MQ builds is almost certainly running the
    # one they touched most recently.
    seen.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return seen


def scan_eq_dir():
    home = os.environ.get("USERPROFILE", "")
    patterns = [DEFAULT_EQ_DIR]
    for drive in _drives():
        patterns.append(os.path.join(drive, "EverQuest"))
        patterns.append(os.path.join(drive, "Games", "EverQuest"))
        patterns.append(os.path.join(drive, "Program Files (x86)", "Sony", "EverQuest"))
        patterns.append(os.path.join(drive, "Program Files (x86)", "Daybreak Game Company",
                                     "Installed Games", "EverQuest"))
        patterns.append(os.path.join(drive, "Users", "Public", "Daybreak Game Company",
                                     "Installed Games", "EverQuest*"))
        patterns.append(os.path.join(drive, "*", "steamapps", "common", "EverQuest"))
        patterns.append(os.path.join(drive, "SteamLibrary", "steamapps", "common", "EverQuest"))
    if home:
        patterns.append(os.path.join(home, "EverQuest"))

    seen = []
    for pattern in patterns:
        try:
            hits = glob.glob(pattern)
        except OSError:
            continue
        for hit in hits:
            if hit not in seen and _has_marker(hit, EQ_MARKERS):
                seen.append(hit)
    seen.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return seen


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------
def _pick(saved, env_name, beacon_value, scanner, default):
    """First source that yields a path wins; the source name travels with it."""
    if saved:
        return {"path": saved, "source": "saved"}
    env = os.environ.get(env_name)
    if env:
        return {"path": env, "source": "env"}
    if beacon_value:
        return {"path": beacon_value, "source": "beacon"}
    hits = scanner()
    if hits:
        return {"path": hits[0], "source": "scan", "alternatives": hits[1:6]}
    return {"path": default, "source": "default"}


def resolve():
    """Everything Setup needs to explain itself: paths, where each came from, and
    whether the thing actually exists on disk."""
    saved = load_overrides()
    beacon = read_beacon()

    mq = _pick(saved.get("mq_config"), "EQFORGE_MQ_CONFIG",
               (beacon or {}).get("mq_config"), scan_mq_config, DEFAULT_MQ_CONFIG)
    eq = _pick(saved.get("eq_dir"), "EQFORGE_EQ_DIR",
               (beacon or {}).get("eq_path"), scan_eq_dir, DEFAULT_EQ_DIR)

    mq["exists"] = os.path.isdir(mq["path"])
    eq["exists"] = os.path.isdir(eq["path"])
    # A directory that exists but holds no marker is the nastiest case: every
    # write "succeeds" into a folder nothing reads. Say so explicitly.
    mq["verified"] = _has_marker(mq["path"], MQ_MARKERS) or \
        _has_marker(os.path.dirname(mq["path"]), MQ_MARKERS)
    eq["verified"] = _has_marker(eq["path"], EQ_MARKERS)

    return {"mq_config": mq, "eq_dir": eq, "beacon": beacon,
            "settings_path": SETTINGS_PATH,
            "env": {"EQFORGE_MQ_CONFIG": os.environ.get("EQFORGE_MQ_CONFIG", ""),
                    "EQFORGE_EQ_DIR": os.environ.get("EQFORGE_EQ_DIR", "")}}


def apply():
    """Resolve, then point the roster API at the result. serve.py calls this at
    startup and after every Setup save, so a path change takes effect without a
    restart (mychars/*.py is imported once; only these two globals move)."""
    r = resolve()
    from . import api as roster_api
    roster_api.MQ_CONFIG_DIR = r["mq_config"]["path"]
    roster_api.EQ_DIR = r["eq_dir"]["path"]
    return r


# ---------------------------------------------------------------------------
# diagnostics  -  what Setup shows you
# ---------------------------------------------------------------------------
def _count(directory, pattern):
    if not directory or not os.path.isdir(directory):
        return 0
    try:
        return len(glob.glob(os.path.join(directory, pattern)))
    except OSError:
        return 0


def _newest_age_h(directory, pattern):
    """Hours since the most recent match, or None. Age is the honest health
    signal for a feed: 'files exist' says nothing about whether it still runs."""
    if not directory or not os.path.isdir(directory):
        return None
    try:
        hits = glob.glob(os.path.join(directory, pattern))
    except OSError:
        return None
    if not hits:
        return None
    import time
    newest = max(os.path.getmtime(h) for h in hits)
    return round((time.time() - newest) / 3600.0, 1)


def mq_lua_dir(mq_config):
    """Standard MQ layout puts lua/ and config/ side by side under the MQ root."""
    if not mq_config:
        return ""
    return os.path.join(os.path.dirname(os.path.normpath(mq_config)), "lua")


ADDON_DEFAULTS = {"camp": True, "login": True, "loginDump": False,
                  "zone": False, "quiet": False, "every": 0}


def read_addon_settings(mq_config):
    """Parse <MQ config>/eqforge_addon.lua.

    Both the Setup page and in-game `/eqf on camp` write this file, so Setup MUST
    read the real values back before showing toggles - rendering defaults would
    make a save silently revert whatever was set in game. The file is a flat
    `return { key = value, }` table written by us at both ends, so a line scan is
    enough; no Lua parser, and an unreadable file falls back to the addon's own
    defaults rather than to nothing.
    """
    out = dict(ADDON_DEFAULTS)
    path = os.path.join(mq_config or "", "eqforge_addon.lua")
    if not os.path.isfile(path):
        return {"found": False, "path": path, "settings": out}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {"found": False, "path": path, "settings": out}

    for line in text.splitlines():
        line = line.split("--", 1)[0].strip().rstrip(",")
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key not in out:
            continue
        if value in ("true", "false"):
            out[key] = (value == "true")
        else:
            try:
                out[key] = int(float(value))
            except ValueError:
                pass
    return {"found": True, "path": path, "settings": out}


def addon_autostarts(mq_config):
    """Is `/lua run eqforge` wired into ingame.cfg?

    MEASURED 2026-08-07: the addon does NOT survive camping to character select, so a
    hand-started copy produces exactly ONE camp export and then silently stops. This
    line is therefore part of a working install, not a nicety - Setup reports it as
    such. ';' comments a line out in an MQ cfg, so a commented example doesn't count.
    """
    path = os.path.join(mq_config or "", "ingame.cfg")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return {"found": False, "path": path, "autostarts": False}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(";") and "eqforge" in stripped.lower():
            return {"found": True, "path": path, "autostarts": True}
    return {"found": True, "path": path, "autostarts": False}


def diagnose():
    """resolve() plus what is actually installed and feeding data, so a broken
    setup names its own missing piece instead of returning an empty list."""
    r = resolve()
    mq_config = r["mq_config"]["path"]
    eq_dir = r["eq_dir"]["path"]
    lua_dir = mq_lua_dir(mq_config)

    r["lua_dir"] = lua_dir
    r["installed"] = {
        # The addon is the one piece we ship and control, so it is checked by file.
        "addon": os.path.isfile(os.path.join(lua_dir, "eqforge", "init.lua")),
        "addon_settings": os.path.isfile(os.path.join(mq_config, "eqforge_addon.lua")),
        # Third-party, not bundled: report presence, never assume it.
        "parcel": os.path.isfile(os.path.join(lua_dir, "parcel", "init.lua")),
        "parcel_sources": os.path.isfile(os.path.join(mq_config, "parcel_sources.lua")),
        "mailgear": os.path.isfile(os.path.join(lua_dir, "mailgear", "init.lua")),
        "autologin_db": os.path.isfile(os.path.join(mq_config, "login.db")),
    }
    r["addon"] = read_addon_settings(mq_config)
    r["autostart"] = addon_autostarts(mq_config)
    r["feeds"] = {
        "dumps": {"count": _count(eq_dir, "*-Inventory.txt"),
                  "newest_age_h": _newest_age_h(eq_dir, "*-Inventory.txt")},
        "roster_exports": {"count": _count(mq_config, "mychars_export_*.csv"),
                           "newest_age_h": _newest_age_h(mq_config, "mychars_export_*.csv")},
        "lockout_exports": {"count": _count(mq_config, "mychars_lockouts*.txt"),
                            "newest_age_h": _newest_age_h(mq_config, "mychars_lockouts*.txt")},
    }
    return r

#!/usr/bin/env python3
"""
EQ Forge 2.0 — local dev server + TLP pricing-API proxy.

Serves the static app (app/, items.txt.gz) AND proxies /api/* to tlp-auctions.com.
The proxy exists because the TLP pricing API only sends CORS headers for wangel's
own origin, so a browser served from localhost (or any forked origin) can't call
it directly. This server makes the call server-side (no CORS) and hands the JSON
back same-origin, so the app's price checks just work locally.

Run:
    python serve.py
then open   http://localhost:8000/app/

Nothing about your inventory or INI ever touches this server — only the item-id
price lookups are forwarded to TLP-Auctions, exactly as the deployed app would.
Standard library only; no pip install.
"""
import os
import sys
import json
import socket
import socketserver
import urllib.parse
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from mychars import api as roster_api          # noqa: E402  (My Characters section)
from mychars import paths as mqpaths           # noqa: E402  (finds MQ + EQ for us)
# roster_api reads MQ AutoLogin's login.db (sanitized: never the accounts table);
# keep its idea of the MQ config dir in lockstep with ours (set below).
UPSTREAM = "https://tlp-auctions.com"          # apex host (valid cert)
PORT = int(os.environ.get("EQFORGE_PORT", "8000"))
# Bind address. Localhost-only BY DEFAULT on purpose: this server writes Lua straight
# into MQ's config folder, arms harvest runs and mutates mychars.db, all with zero
# auth — nothing on the network should be able to reach that unless it's asked for.
# `run.bat --lan` (or EQFORGE_HOST=0.0.0.0) opens it to the home wifi for phone use.
HOST = os.environ.get("EQFORGE_HOST", "127.0.0.1")
CLIENT_TAG = "EQ-Forge-2.0/local"              # identifies our traffic to the API owner

# Where the Gear Planner's plan gets written, and where /outputfile inventory dumps
# land. This is a LOCAL app, so the server writes and reads those folders directly -
# no browser download, no file picker, nothing for the user to move.
#
# BOTH paths are resolved by mychars/paths.py: saved Setup override > env var >
# the MQ addon's beacon > a scan of the usual install locations > the old hardcoded
# default. Nothing needs configuring on a normal install, and Setup explains itself
# when something is off. See mychars/paths.py for the full order.
MQ_CONFIG_DIR = ""
EQ_DIR = ""
DUMP_SUFFIX = "-Inventory.txt"


def apply_paths():
    """(Re)resolve MQ + EQ and push the result everywhere that reads it. Called at
    startup and after a Setup save, so changing a path never needs a restart."""
    global MQ_CONFIG_DIR, EQ_DIR
    resolved = mqpaths.apply()               # also sets roster_api.MQ_CONFIG_DIR/EQ_DIR
    MQ_CONFIG_DIR = resolved["mq_config"]["path"]
    EQ_DIR = resolved["eq_dir"]["path"]
    return resolved


apply_paths()

# The bundled item database. At ~11 MB it is the entire download size of this app, and
# it is the one file that can be re-fetched from its source, so `build_zip.py --slim`
# leaves it out and the server pulls it on first run. That keeps the shipped zip under
# 1 MB — small enough to attach to a Discord message (10 MB cap for non-Nitro).
#
# The live file has FEWER columns than the copy that used to be bundled (no itemhash /
# itemlink), which is fine: forge.js already computes the itemlink itself precisely
# because sodeq dropped that column. It also carries ~5k more items.
ITEMDB_NAME = "items.txt.gz"
ITEMDB_PATH = os.path.join(ROOT, ITEMDB_NAME)
ITEMDB_URL = "https://items.sodeq.org/downloads/items.txt.gz"


def ensure_item_db():
    """Fetch items.txt.gz if it isn't here. One time, ~8 MB.

    Downloads to a .part file and renames on success, so an interrupted download can
    never leave a truncated database in place that then parses to a half-empty item
    list - which would look like a bug in the app, not a failed download.
    """
    if os.path.exists(ITEMDB_PATH):
        return True
    part = ITEMDB_PATH + ".part"
    print("  Item DB: not found, downloading once from %s" % ITEMDB_URL)
    try:
        req = urllib.request.Request(ITEMDB_URL, headers={"User-Agent": CLIENT_TAG})
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        sys.stdout.write("\r           %5.1f%%  (%.1f MB)"
                                         % (100.0 * got / total, got / 1048576.0))
                        sys.stdout.flush()
        print("\r           done - %.1f MB              " % (got / 1048576.0))
        os.replace(part, ITEMDB_PATH)
        return True
    except Exception as e:
        print("\r  Item DB: DOWNLOAD FAILED (%s)" % e)
        print("           Grab %s by hand and drop it next to serve.py." % ITEMDB_URL)
        try:
            os.remove(part)
        except OSError:
            pass
        return False


# The plan is written under BOTH names, same bytes. mailgear.lua prefers the first;
# TrixBox hardcodes the second, so dropping it would break an existing TrixBox setup.
GEARPLAN_NAMES = ["mailgearplan.lua", "trixbox_gearplan.lua"]
# DerpleDude's `parcel` Lua auto-loads config/parcel_sources.lua. We never touch that
# hand-written file; it chain-loads this generated one so the current plan shows up as a
# pickable "source" in the parcel UI (you review the list and hit Send yourself).
PARCELSRC_NAME = "parcel_gearplan.lua"
# Settings for the MQ addon (extras/eqforge). Written by Setup, read by /lua run
# eqforge on load - same file the in-game /eqf on|off writes, so neither side owns it.
ADDON_SETTINGS_NAME = "eqforge_addon.lua"
ADDON_FLAGS = ("camp", "login", "loginDump", "zone", "quiet")


class Handler(SimpleHTTPRequestHandler):
    # Source files must NEVER be served from a stale browser cache. SimpleHTTPRequestHandler
    # sends only Last-Modified, and Chrome then applies a heuristic freshness window -- so
    # after editing forge.js/index.html you keep getting the OLD file on a normal reload and
    # the app looks like the change never shipped (this cost two debugging rounds on
    # 2026-08-05: the server was serving the new file correctly the whole time).
    # "no-cache" = still cache it, but ALWAYS revalidate -> unchanged files still 304
    # cheaply, changed ones are always picked up. Deliberately NOT applied to
    # items.txt.gz / spell-effects.json.gz: those are content-addressed by the app's own
    # IndexedDB revalidation and are far too big to re-check on every asset load.
    NO_CACHE_EXT = (".html", ".js", ".css", ".json")

    def end_headers(self):
        path = self.path.split("?", 1)[0]
        if path.endswith(self.NO_CACHE_EXT) or path.endswith("/"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        SimpleHTTPRequestHandler.end_headers(self)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # Quieter logging: one line per request, no noisy tracebacks on client aborts.
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _proxy(self, method):
        """Forward /api/... to tlp-auctions.com and stream the response back."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        url = UPSTREAM + self.path            # self.path already begins with /api/
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        req.add_header("Accept", "application/json")
        req.add_header("X-Client-App", CLIENT_TAG)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(502, "proxy error: %s" % e)

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_gearplan(self):
        """Write the posted Lua plan under BOTH names (see GEARPLAN_NAMES)."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        path = os.path.join(MQ_CONFIG_DIR, GEARPLAN_NAMES[0])
        try:
            os.makedirs(MQ_CONFIG_DIR, exist_ok=True)
            written = []
            for name in GEARPLAN_NAMES:
                p = os.path.join(MQ_CONFIG_DIR, name)
                with open(p, "wb") as f:
                    f.write(body)
                written.append(p)
            return self._json(200, {"ok": True, "path": path, "paths": written, "bytes": len(body)})
        except Exception as e:
            return self._json(500, {"ok": False, "path": path, "error": str(e)})

    def _write_parcelsource(self):
        """Write the posted parcel-source Lua to <MQ config>/parcel_gearplan.lua."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        path = os.path.join(MQ_CONFIG_DIR, PARCELSRC_NAME)
        try:
            os.makedirs(MQ_CONFIG_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(body)
            return self._json(200, {"ok": True, "path": path, "bytes": len(body)})
        except Exception as e:
            return self._json(500, {"ok": False, "path": path, "error": str(e)})

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _save_setup(self):
        """Persist the Setup overrides and re-resolve immediately.

        An empty string CLEARS an override rather than saving a blank path, so the
        page always has a way back to auto-detection - a saved "" would otherwise
        win the precedence order forever and look like the scan was broken.
        """
        try:
            payload = self._read_json_body()
        except ValueError:
            return self._json(400, {"ok": False, "error": "invalid JSON body"})
        mqpaths.save_overrides(mq_config=payload.get("mq_config"),
                               eq_dir=payload.get("eq_dir"))
        resolved = apply_paths()
        return self._json(200, {"ok": True, "setup": mqpaths.diagnose()})

    def _write_addon_settings(self):
        """Write <MQ config>/eqforge_addon.lua from the Setup toggles.

        Same file and same shape the in-game `/eqf on camp` writes. Only known keys
        are emitted, and booleans are coerced here, so a bad POST cannot produce a
        Lua file that fails to load and silently reverts every setting to default.
        """
        try:
            payload = self._read_json_body()
        except ValueError:
            return self._json(400, {"ok": False, "error": "invalid JSON body"})

        lines = ["-- EQ Forge addon settings. Written by /eqf and by EQ Forge -> Setup.",
                 "-- Edit by hand if you like; unknown keys are ignored.",
                 "return {"]
        for flag in ADDON_FLAGS:
            lines.append("    %-10s = %s," % (flag, "true" if payload.get(flag) else "false"))
        try:
            every = max(0, int(payload.get("every") or 0))
        except (TypeError, ValueError):
            every = 0
        lines.append("    %-10s = %d," % ("every", every))
        lines.append("}")
        body = ("\n".join(lines) + "\n").encode("utf-8")

        path = os.path.join(MQ_CONFIG_DIR, ADDON_SETTINGS_NAME)
        try:
            os.makedirs(MQ_CONFIG_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(body)
            return self._json(200, {"ok": True, "path": path, "bytes": len(body),
                                    "note": "Run /eqf stop then /lua run eqforge "
                                            "(or relog) to pick this up in game."})
        except Exception as e:
            return self._json(500, {"ok": False, "path": path, "error": str(e)})

    def _roster(self, method):
        """My Characters JSON API — all logic lives in mychars/api.py."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            return self._json(400, {"ok": False, "error": "invalid JSON body"})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path[len("/roster"):]
        if parsed.query:                       # GET params (e.g. /gear/best?stat=haste)
            payload.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
        code, obj = roster_api.handle(method, path, payload)
        return self._json(code, obj)

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._proxy("POST")
        if self.path.startswith("/roster/"):
            return self._roster("POST")
        if self.path == "/gearplan":
            return self._write_gearplan()
        if self.path == "/parcelsource":
            return self._write_parcelsource()
        if self.path == "/setup":
            return self._save_setup()
        if self.path == "/addon":
            return self._write_addon_settings()
        self.send_error(405, "POST only supported for /api/*, /roster/*, /gearplan, "
                             "/parcelsource, /setup, /addon")

    def do_PUT(self):
        if self.path.startswith("/roster/"):
            return self._roster("PUT")
        self.send_error(405, "PUT only supported for /roster/*")

    def do_DELETE(self):
        if self.path.startswith("/roster/"):
            return self._roster("DELETE")
        self.send_error(405, "DELETE only supported for /roster/*")

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._proxy("GET")
        if self.path.startswith("/roster/"):
            return self._roster("GET")
        # Where everything resolved to, where each path came from, what is installed
        # in MQ, and how fresh each in-game feed is. This is what Setup renders, and
        # it is the first thing to look at when an MQ-facing feature "returns nothing".
        if self.path == "/setup":
            return self._json(200, {"ok": True, "setup": mqpaths.diagnose()})
        # List the inventory dumps sitting in the EQ folder, newest first, with mtimes so
        # the app can show how fresh each one is.
        if self.path == "/dumps":
            try:
                out = []
                for fn in os.listdir(EQ_DIR):
                    if fn.endswith(DUMP_SUFFIX):
                        fp = os.path.join(EQ_DIR, fn)
                        out.append({"file": fn,
                                    "toon": fn.split("_")[0],
                                    "mtime": int(os.path.getmtime(fp))})
                out.sort(key=lambda d: -d["mtime"])
                return self._json(200, {"ok": True, "dir": EQ_DIR, "dumps": out})
            except Exception as e:
                return self._json(500, {"ok": False, "dir": EQ_DIR, "error": str(e)})
        # Serve one dump's raw text so the app can load it without a file picker.
        if self.path.startswith("/dump/"):
            fn = urllib.parse.unquote(self.path[len("/dump/"):])
            if ("/" in fn or "\\" in fn or ".." in fn or not fn.endswith(DUMP_SUFFIX)):
                return self.send_error(400, "bad dump name")
            fp = os.path.join(EQ_DIR, fn)
            try:
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                return self.send_error(404, "dump not found: %s" % e)
        # Where would the plan be written? (UI shows this so the path is never a mystery.)
        if self.path == "/gearplan":
            return self._json(200, {"ok": True, "path": os.path.join(MQ_CONFIG_DIR, GEARPLAN_NAMES[0]),
                                    "exists": os.path.isfile(os.path.join(MQ_CONFIG_DIR, GEARPLAN_NAMES[0]))})
        # Redirect the bare root to the app for convenience.
        if self.path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/app/")
            self.end_headers()
            return
        return super().do_GET()


class Server(socketserver.ThreadingTCPServer):
    # Threaded so a slow price batch doesn't block the DB download or the UI.
    # allow_reuse_address only off Windows: on Windows SO_REUSEADDR lets a SECOND
    # serve.py silently bind the same port, so restarts STACK zombie servers that
    # keep serving stale code (three were found on :8000, 2026-07-25). Better to
    # fail loudly with "another instance may be running".
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        # SO_EXCLUSIVEADDRUSE = the correct Windows middle ground: a second LIVE
        # server still fails loudly (no zombie stacking), but binding over dead
        # TIME_WAIT sockets from a killed instance works — without it, restarting
        # within ~2 min of a kill hit WinError 10048 on a genuinely free port.
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def lan_ips():
    """EVERY usable IPv4 address on this machine, best guess first.

    Deliberately not a single answer: the 'route to 8.8.8.8' trick picks the
    INTERNET-facing interface, which on some setups is an ISP-managed 100.64/10
    segment the phone can't reach — while the wifi the phone is actually on would
    be a different adapter entirely. Printing one address there just sends you
    chasing the wrong URL, so print them all and let the phone decide.

    APIPA (169.254.x) is filtered out: that address means the adapter is up but
    got no DHCP lease, so nothing can reach it.
    """
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        out.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith(("127.", "169.254.")):
                out.append(ip)
    except OSError:
        pass
    return out


SOURCE_LABEL = {
    "saved": "from Setup",
    "env": "from the environment variable",
    "beacon": "auto-detected from the MQ addon",
    "scan": "auto-detected",
    "default": "FALLBACK GUESS",
}


def print_paths():
    """Say out loud where MQ and EQ resolved to, and warn when it is a guess.

    A wrong path here is the single most common way this app looks broken on
    someone else's machine: every MQ-facing feature returns an empty result
    instead of an error, because an empty folder scans cleanly.
    """
    d = mqpaths.diagnose()
    # EQ is required (dumps live there); MQ is optional, so a missing MacroQuest is
    # reported as a plain fact, never as an error. Shouting "!!" at someone who simply
    # doesn't run MQ is noise that trains people to ignore real warnings.
    eq = d["eq_dir"]
    print("  %s EQ:    %s" % ("  " if eq["verified"] else "!!", eq["path"]))
    print("       (%s)" % SOURCE_LABEL.get(eq["source"], eq["source"]))
    if not eq["exists"]:
        print("       ^ that folder does not exist - inventory dumps can't be read")
    elif not eq["verified"]:
        print("       ^ folder exists but has no EverQuest files in it")

    mq = d["mq_config"]
    if mq["verified"]:
        print("     MQ:    %s" % mq["path"])
        print("       (%s)" % SOURCE_LABEL.get(mq["source"], mq["source"]))
        if not d["installed"]["addon"]:
            print("       Tip: copy extras/eqforge into %s and run /lua run eqforge"
                  % d["lua_dir"])
    else:
        print("     MQ:    not found - fine if you don't run MacroQuest")

    if not eq["verified"]:
        print("  Fix it at:  http://localhost:%d/app/setup.html" % PORT)


if __name__ == "__main__":
    os.chdir(ROOT)
    if "--lan" in sys.argv:
        HOST = "0.0.0.0"
    ensure_item_db()
    try:
        with Server((HOST, PORT), Handler) as httpd:
            print("EQ Forge 2.0 running:")
            print("  App:   http://localhost:%d/app/" % PORT)
            print("  Chars: http://localhost:%d/app/mychars.html" % PORT)
            if HOST != "127.0.0.1":
                ips = lan_ips()
                if ips:
                    print("  LAN:   try these on the phone (it must be on the SAME network"
                          " as one of them):")
                    for ip in ips:
                        print("           http://%s:%d/app/mychars.html" % (ip, PORT))
                else:
                    print("  LAN:   no usable network address found — is any adapter"
                          " actually connected?")
                print("         (open to the whole network, no password — Ctrl+C when done)")
            print("  Proxy: /api/*  ->  %s" % UPSTREAM)
            print_paths()
            print("Press Ctrl+C to stop.")
            # Opened here, after ensure_item_db() and after the socket is bound, so
            # the page is never launched at a server that isn't listening yet.
            if "--open" in sys.argv:
                import webbrowser
                webbrowser.open("http://localhost:%d/app/" % PORT)
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as e:
        print("Could not start on port %d: %s" % (PORT, e))
        print("Another instance may be running, or set EQFORGE_PORT to a free port.")
        sys.exit(1)

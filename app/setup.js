/* Setup page - resolve + explain the two folders everything else depends on.
 *
 * Everything here comes from GET /setup (mychars/paths.py diagnose()). The page
 * deliberately reports SOURCE and VERIFIED separately from EXISTS: a path that
 * exists but holds no MacroQuest/EverQuest files is the failure mode that used to
 * look like success, because writes into an empty folder succeed and reads from
 * it come back empty rather than erroring.
 */
"use strict";

const $ = (id) => document.getElementById(id);

let S = null;                       // last /setup payload

const SOURCE_TEXT = {
  saved: "you set this",
  env: "from the EQFORGE_* environment variable",
  beacon: "detected from the in-game addon",
  scan: "found automatically",
  default: "not found — this is a fallback guess",
};

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function age(hours) {
  if (hours == null) return "never";
  if (hours < 1) return Math.max(1, Math.round(hours * 60)) + "m ago";
  if (hours < 48) return hours.toFixed(1) + "h ago";
  return Math.round(hours / 24) + "d ago";
}

function tag(cls, text) {
  return `<span class="su-tag ${cls}">${esc(text)}</span>`;
}

/* ---------------------------------------------------------------- rendering */

function renderPathNote(entry, noteEl, kind) {
  // MacroQuest is optional, so its "not found" is amber, not red - see renderBanner.
  const optional = kind === "MacroQuest";
  const bits = [];
  if (entry.verified) {
    bits.push(tag("good", "verified"));
  } else if (entry.exists) {
    bits.push(tag("warn", "no " + kind + " files here"));
  } else {
    bits.push(tag(optional ? "warn" : "bad",
                  optional ? "not found (fine if you don't run it)" : "folder not found"));
  }
  bits.push(SOURCE_TEXT[entry.source] || entry.source);
  let html = bits.join(" &nbsp;·&nbsp; ");
  html += `<br><span class="su-path">${esc(entry.path)}</span>`;
  if (entry.alternatives && entry.alternatives.length) {
    html += "<br>Also found: " + entry.alternatives
      .map((p) => `<a href="#" class="su-alt" data-for="${kind}" data-path="${esc(p)}">${esc(p)}</a>`)
      .join("<br>");
  }
  noteEl.innerHTML = html;
}

function renderBanner() {
  const el = $("suBanner");
  const eq = S.eq_dir, mq = S.mq_config;

  // Only the EverQuest folder is REQUIRED - it is where inventory dumps live. Missing
  // MacroQuest is the normal state for anyone not running it, and this page used to
  // report it in red alongside a claim that dump reading would fail, which is simply
  // untrue: dumps come from the EQ folder. Never alarm someone about an optional
  // dependency they deliberately don't have.
  if (!eq.verified) {
    el.className = "su-banner bad";
    el.innerHTML = "<strong>Point EQ Forge at your EverQuest folder.</strong>" +
      "It's where <code>/outputfile inventory</code> writes your dumps, so until it's right, " +
      "“Reload dumps from EQ” has nothing to read. Set it below.";
    return;
  }
  if (!mq.verified) {
    el.className = "su-banner";
    el.innerHTML = "<strong>EverQuest folder found. No MacroQuest detected.</strong>" +
      "That's expected if you don't run it — everything except the automatic exports and " +
      "in-game gear delivery works without it. If you <em>do</em> run MacroQuest, set its " +
      "<code>config</code> folder below.";
    return;
  }
  if (!S.installed.addon) {
    el.className = "su-banner warn";
    el.innerHTML = "<strong>Folders look right. The in-game addon isn't installed yet.</strong>" +
      "EQ Forge works without it — you'd just be running <code>/outputfile inventory</code> " +
      "by hand on each toon. Install it below to automate that.";
    return;
  }
  // The addon does not survive camping to character select (measured), so without the
  // ingame.cfg line you get one camp export per manual start and then silence. That is
  // a half-working install, and silence is exactly what nobody reports as a bug.
  if (S.autostart && !S.autostart.autostarts) {
    el.className = "su-banner warn";
    el.innerHTML = "<strong>Addon installed, but it won't restart itself.</strong>" +
      "It stops when you camp to character select, so you'd get one export per " +
      "<code>/lua run eqforge</code>. Add <code>/timed 50 /lua run eqforge</code> to " +
      "<code>" + esc(S.autostart.path) + "</code> and it starts on every login.";
    return;
  }
  el.className = "su-banner ok";
  el.innerHTML = "<strong>All set.</strong>Folders found, addon installed. " +
    "Camp out on a toon and its roster row, lockouts and inventory land here automatically.";
}

function renderAddon() {
  const installed = S.installed.addon;
  $("suAddonTag").className = "su-tag " + (installed ? "good" : "warn");
  $("suAddonTag").textContent = installed ? "installed" : "not installed";
  $("suAddonInstall").style.display = installed ? "none" : "";
  $("suAddonPath").textContent = (S.lua_dir || "<MacroQuest>\\lua") + "\\eqforge\\init.lua";

  // Show what is REALLY in the settings file. In-game /eqf writes the same file, so
  // rendering our own defaults here would make a save quietly undo a change made
  // in game.
  const a = (S.addon && S.addon.settings) || {};
  $("suCamp").checked = !!a.camp;
  $("suLogin").checked = !!a.login;
  $("suLoginDump").checked = !!a.loginDump;
  $("suZone").checked = !!a.zone;
  $("suQuiet").checked = !!a.quiet;
  $("suEvery").value = a.every || 0;
  if (S.addon && !S.addon.found) {
    msg($("suAddonMsg"), "No settings file yet — these are the addon's defaults.");
  }
}

function renderFeeds() {
  const f = S.feeds;
  const rows = [
    ["Inventory dumps", f.dumps,
     "Every tab that knows what you own. <code>/outputfile inventory</code>, or <code>/eqf dump</code>."],
    ["Roster exports", f.roster_exports,
     "My Characters → Import → “Load in-game exports”. <code>/eqf roster</code>."],
    ["Lockout exports", f.lockout_exports,
     "Keys &amp; Access → “Load lockouts from MQ”. <code>/eqf lockouts</code>."],
  ];
  $("suFeeds").innerHTML = rows.map(([label, data, why]) => {
    const none = !data.count;
    return `<li>
      ${tag(none ? "warn" : "good", none ? "none yet" : data.count + " files")}
      <span class="su-what"><strong>${esc(label)}</strong>
        <span class="su-why">${why}</span></span>
      <span class="su-why">${esc(age(data.newest_age_h))}</span>
    </li>`;
  }).join("");
}

function renderTools() {
  const i = S.installed;
  const rows = [
    ["DerpleDude's <code>parcel</code> script", i.parcel,
     "Sends a planned gear set to another toon in game. Third-party — install it into " +
     "<code>lua\\parcel\\</code> yourself; EQ Forge never bundles it."],
    ["<code>parcel_sources.lua</code>", i.parcel_sources,
     "The chain-load block that makes your gear plan appear as a parcel source. " +
     "Copy <code>extras\\parcel_sources.lua</code> into MacroQuest's <code>config</code> folder."],
    ["MailGear", i.mailgear,
     "Dequip / bank-pull / equip — the half parcel can't do. Copy " +
     "<code>extras\\mailgear.lua</code> to <code>lua\\mailgear\\init.lua</code>."],
    ["MQ AutoLogin database", i.autologin_db,
     "Lets Import read your character list straight out of MacroQuest. Nothing to install — " +
     "it appears once you've used AutoLogin."],
    ["Addon settings file", i.addon_settings,
     "Written the first time you save addon settings, here or with <code>/eqf on camp</code>."],
    ["Addon autostart in <code>ingame.cfg</code>",
     !!(S.autostart && S.autostart.autostarts),
     "<b>Needed for the addon to keep working.</b> It stops when you camp to character " +
     "select, so without <code>/timed 50 /lua run eqforge</code> in that file you get one " +
     "export per manual start."],
  ];
  $("suTools").innerHTML = rows.map(([label, present, why]) => `<li>
      ${tag(present ? "good" : "", present ? "found" : "not found")}
      <span class="su-what"><strong>${label}</strong>
        <span class="su-why">${why}</span></span>
    </li>`).join("");
}

function render() {
  // Only overwrite the boxes with a saved override; a detected path stays as
  // placeholder text so "empty means auto-detect" keeps being true.
  $("suEq").value = S.eq_dir.source === "saved" ? S.eq_dir.path : "";
  $("suMq").value = S.mq_config.source === "saved" ? S.mq_config.path : "";
  $("suEq").placeholder = S.eq_dir.path || "auto-detect";
  $("suMq").placeholder = S.mq_config.path || "auto-detect";
  renderPathNote(S.eq_dir, $("suEqNote"), "EverQuest");
  renderPathNote(S.mq_config, $("suMqNote"), "MacroQuest");
  renderBanner();
  renderAddon();
  renderFeeds();
  renderTools();
}

/* ------------------------------------------------------------------- server */

async function load() {
  try {
    const r = await fetch("/setup");
    const j = await r.json();
    S = j.setup;
    render();
  } catch (e) {
    $("suBanner").className = "su-banner bad";
    $("suBanner").innerHTML = "<strong>Can't reach the EQ Forge server.</strong>" +
      "Open this page through <code>run.bat</code> (http://localhost:8000/app/setup.html), " +
      "not by double-clicking the file.";
  }
}

function msg(el, text, cls) {
  el.textContent = text;
  el.className = "su-msg" + (cls ? " " + cls : "");
}

async function saveFolders() {
  const out = $("suSaveMsg");
  msg(out, "Saving…");
  try {
    const r = await fetch("/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eq_dir: $("suEq").value, mq_config: $("suMq").value }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "save failed");
    S = j.setup;
    render();
    msg(out, "Saved — in effect now, no restart needed.", "good");
  } catch (e) {
    msg(out, String(e.message || e), "bad");
  }
}

async function saveAddon() {
  const out = $("suAddonMsg");
  msg(out, "Writing…");
  try {
    const r = await fetch("/addon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        camp: $("suCamp").checked,
        login: $("suLogin").checked,
        loginDump: $("suLoginDump").checked,
        zone: $("suZone").checked,
        quiet: $("suQuiet").checked,
        every: parseInt($("suEvery").value, 10) || 0,
      }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "write failed");
    msg(out, j.note || "Written.", "good");
    load();
  } catch (e) {
    msg(out, String(e.message || e), "bad");
  }
}

/* --------------------------------------------------------------------- wire */

$("suSave").addEventListener("click", saveFolders);
$("suAddonSave").addEventListener("click", saveAddon);
$("suEqClear").addEventListener("click", () => { $("suEq").value = ""; saveFolders(); });
$("suMqClear").addEventListener("click", () => { $("suMq").value = ""; saveFolders(); });

// A scan alternative is one click, not a retype.
document.addEventListener("click", (e) => {
  const a = e.target.closest(".su-alt");
  if (!a) return;
  e.preventDefault();
  ($(a.dataset.for === "EverQuest" ? "suEq" : "suMq")).value = a.dataset.path;
  saveFolders();
});

load();

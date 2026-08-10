/* My Characters — EQ multibox command center (EQ Forge 2.0 section).
   Vanilla JS, no framework. All data lives server-side in SQLite via /roster/*.
   Hard rule everywhere: only ONE character per EQ account in a live composition.
   Status colors: red = confirmed problem ONLY; yellow = partial/review;
   green = ready; gray = unknown/untested. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

const CAP = 60;                                 // current level cap (Velious era)
const ACCT_COLORS = ["#38bdf8", "#a78bfa", "#22c55e", "#f59e0b", "#f472b6",
                     "#2dd4bf", "#fb923c", "#94a3b8"];
// WoW-style class colors adapted to EQ — instant recognition beats icons
const CLASS_COLORS = {
  "Warrior": "#c79c6e", "Cleric": "#e6dfd0", "Paladin": "#f58cba", "Ranger": "#abd473",
  "Shadow Knight": "#8788ee", "Druid": "#ff7d0a", "Monk": "#00ff98", "Bard": "#e5cc80",
  "Rogue": "#fff569", "Shaman": "#2196f3", "Necromancer": "#33937f", "Wizard": "#69ccf0",
  "Magician": "#f4802b", "Enchanter": "#d982f5", "Beastlord": "#0fd4af", "Berserker": "#c41f3b",
};
const clsColor = (c) => CLASS_COLORS[c] || "#94a3b8";
const clsSpan = (c, txt) => "<span style='color:" + clsColor(c) + "'>" + esc(txt == null ? c : txt) + "</span>";

let S = null;                                   // bootstrap payload
let comp = { id: null, slots: [null, null, null, null, null, null] };
let selectedSlot = 0;
let editCharId = null, editAcctId = null;
let capOverrides = {};                          // dialog working copy
let importPreviewed = false;
let quickFilter = "";                           // summary-card filter: cap|ready|review
let expandedChar = null;                        // char id with open drawer
const expandedAccts = new Set();                // account cards showing "+N more"
let loginSet = {};                              // acctId -> charId (persisted locally)
try { loginSet = JSON.parse(localStorage.getItem("eqmc-loginset") || "{}"); } catch (e) { loginSet = {}; }

// ---------- plumbing ----------
async function api(method, path, body) {
  const r = await fetch("/roster" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    // Carry the body on the Error. Some failures are a REQUEST FOR INPUT rather than
    // a fault — /gearsets/apply answers 409 with the mapping it needs you to confirm —
    // and a bare message string throws that payload away.
    const err = new Error(data.error || (method + " " + path + " failed (" + r.status + ")"));
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", !!isErr);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 3500);
}

async function guard(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
}

async function reload() {
  S = await api("GET", "/bootstrap");
  // gear sets ride along so the comp builder can show each toon's set + fit
  try { gsSets = (await api("GET", "/gearsets")).sets; } catch (e) { gsSets = gsSets || []; }
  ensureLoginSet();
  renderAll();
}

const charById = (id) => S.characters.find((c) => c.id === id);
const acctById = (id) => S.accounts.find((a) => a.id === id);
const capLabel = (k) => (S.meta.capabilities.find((c) => c.key === k) || { label: k }).label;
const claimInfo = (name) => (S.meta.claim_items || []).find((c) => c.name === name);

// Past the hand-picked palette (i.e. a 9th account and beyond) the old `% length`
// wrapped, so account 9 wore account 1's colour. Account colour is the ONLY thing
// telling you which account a toon sits on, and boxers running 12-24 accounts are
// exactly who needs that, so walk the hue circle by the golden angle instead —
// every extra account gets a visually distinct colour, forever.
function paletteColor(i) {
  if (i < 0) return "#475569";
  if (i < ACCT_COLORS.length) return ACCT_COLORS[i];
  return "hsl(" + Math.round((i * 137.508) % 360) + " 68% 62%)";
}

function acctColor(aid) {
  if (aid == null) return "#475569";
  return paletteColor(S.accounts.findIndex((a) => a.id === aid));
}

// Account display mode: "login" shows the alias (login name), "nick" shows the
// nickname (falling back to "Account N"). Flipped by the header toggle.
let acctDisplay = localStorage.getItem("eqmc-acctdisplay") || "login";
let sbGroupByAcct = localStorage.getItem("eqmc-sbgroup") === "acct";
function acctLabel(a) {
  if (!a) return "—";
  if (acctDisplay === "nick")
    return a.nickname || "Account " + (a.account_number || a.launch_order || "?");
  return a.alias;
}
const charAcctLabel = (c) =>
  c.account_id != null ? acctLabel(acctById(c.account_id)) : (c.account_alias || "—");

function fmtRemain(expiresEpoch) {
  if (!expiresEpoch) return "";
  const s = expiresEpoch - Math.floor(Date.now() / 1000);
  if (s <= 0) return "expired";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + "d" + h + "h";
  if (h) return h + "h" + m + "m";
  return m + "m";
}

// ---------- derived status ----------
function charIssues(c) {
  const iss = [];
  for (const u of S.unlocks) {
    if (u.character_id !== c.id) continue;
    if (u.status === "Missing" && (u.priority === "critical" || u.priority === "high"))
      iss.push({ lv: "bad", text: "Missing: " + u.name });
    else if ((u.status === "Unknown" || u.status === "In Progress") && u.priority === "critical")
      iss.push({ lv: "warn", text: "Verify: " + u.name });
  }
  if (c.level == null) iss.push({ lv: "dim", text: "level unknown" });
  return iss;
}

function readiness(c) {
  const iss = charIssues(c);
  if (iss.some((i) => i.lv === "bad")) return { lv: "bad", text: "issue" };
  if ((c.level || 0) >= CAP) {
    if (c.automation_status === "tested" && !iss.some((i) => i.lv === "warn"))
      return { lv: "ready", text: "ready" };
    if (c.automation_status === "partial" || iss.some((i) => i.lv === "warn"))
      return { lv: "warn", text: "almost" };
    return { lv: "dim", text: "unproven" };
  }
  return { lv: "dim", text: c.level ? "leveling" : "—" };
}
const READY_CLS = { ready: "st-ready", warn: "st-warn", bad: "st-bad", dim: "st-dim" };

// ---------- login set ----------
function ensureLoginSet() {
  for (const a of S.accounts) {
    const cur = loginSet[a.id] && charById(loginSet[a.id]);
    if (cur && cur.account_id === a.id) continue;
    const mine = S.characters.filter((c) => c.account_id === a.id && c.status !== "retired");
    const pick =
      mine.find((c) => (c.level || 0) >= CAP && c.automation_status === "tested") ||
      mine.find((c) => (c.level || 0) >= CAP && (c.group_tags || "").includes("core")) ||
      mine.slice().sort((x, y) => (y.level || 0) - (x.level || 0))[0];
    if (pick) loginSet[a.id] = pick.id; else delete loginSet[a.id];
  }
  localStorage.setItem("eqmc-loginset", JSON.stringify(loginSet));
}

function setLoginPick(acctId, charId) {
  loginSet[acctId] = charId;
  localStorage.setItem("eqmc-loginset", JSON.stringify(loginSet));
  renderLoginSet();
  renderAccounts();
  renderChars();
}

const loginSlots = () => S.accounts.map((a) => loginSet[a.id]).filter(Boolean);

// ---------- tabs ----------
document.querySelectorAll(".mtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".mtab").forEach((b) => b.classList.toggle("active", b === btn));
    ["roster", "builder", "gear", "harvest", "sets", "keys", "import", "optimizer"].forEach((name) => {
      $("mtab-" + name).hidden = name !== btn.dataset.mtab;
    });
    if (btn.dataset.mtab === "gear" && !gearLoaded) {
      gearLoaded = true;
      guard(loadGearSummary);
    }
    if (btn.dataset.mtab === "harvest") guard(loadHarvest);
    if (btn.dataset.mtab === "sets" && !gsLoaded) {
      gsLoaded = true;
      guard(loadGearSets);
    }
  });
});
function gotoTab(name) {
  document.querySelector(".mtab[data-mtab=" + name + "]").click();
}

// ---------- render: everything ----------
function renderAll() {
  $("mcCounts").textContent = S.accounts.length + " accounts · " + S.characters.length +
    " characters · " + S.compositions.length + " comps";
  renderSummary();
  renderLoginSet();
  renderAccounts();
  renderCharFilters();
  renderChars();
  renderBuilder();
  renderCompPicker();
  renderKeys();
  renderRecs();
}

// ---------- summary strip ----------
function renderSummary() {
  const chars = S.characters.filter((c) => c.status !== "retired");
  const capped = chars.filter((c) => (c.level || 0) >= CAP);
  const ready = capped.filter((c) => readiness(c).lv === "ready");
  const review = chars.filter((c) => {
    const r = readiness(c);
    return r.lv === "bad" || r.lv === "warn" || ((c.level || 0) >= CAP && c.automation_status === "untested");
  });
  const conflicts = S.recommendations.filter((r) => r.kind === "account_pairing");
  const cards = [
    { k: "", num: S.accounts.length, lbl: "Accounts", act: () => $("acctGrid").scrollIntoView({ behavior: "smooth" }) },
    { k: "", num: chars.length, lbl: "Characters", act: () => { quickFilter = ""; renderChars(); } },
    { k: "cap", num: capped.length, lbl: "Level-Capped" },
    { k: "ready", num: ready.length, lbl: "Raid Ready" },
    { k: "review", num: review.length, lbl: "Needs Review" },
    { k: "", num: conflicts.length, lbl: "Account Conflicts", act: () => gotoTab("optimizer") },
  ];
  const subs = S.accounts.filter((a) => a.sub_expires || a.membership === "FREE");
  if (subs.length) {
    // Rank by the EARLIEST each account can lapse, and show hours once we are
    // inside a day so "0d" never hides "this could go in the next hour".
    const now = Date.now() / 1000;
    const loOf = (a) => a.membership === "FREE" ? -1 : a.sub_expires - now;
    const soonest = Math.min(...subs.map(loOf));
    const due = subs.filter((a) => loOf(a) <= 7 * 86400).length;
    const num = soonest < 0 ? "!"
      : soonest < 48 * 3600 ? Math.ceil(soonest / 3600) + "h"
      : Math.floor(soonest / 86400) + "d";
    cards.push({
      k: "", num: num,
      cls: soonest < 48 * 3600 ? "st-bad" : soonest <= 7 * 86400 ? "st-warn" : "st-ready",
      lbl: due ? "Next krono · " + due + " due" : "Next krono due",
      act: () => $("acctGrid").scrollIntoView({ behavior: "smooth" }),
    });
  }
  const row = $("summaryRow");
  row.innerHTML = "";

  // FIRST RUN. A new install has no roster (the sample one is opt-in), and Roster is
  // the landing tab — so without this the first thing anyone sees is "0 accounts ·
  // 0 characters" and no indication that Import is the front door. Counters on zero
  // are not an empty state.
  if (!S.characters.length && !S.accounts.length) {
    row.innerHTML =
      "<div class='mc-empty'>" +
      "<h3>No characters yet</h3>" +
      "<p>Everything else here — six-box comps, gear sets, the lockout board — is built " +
      "on your roster, so start by getting your characters in.</p>" +
      "<ul>" +
      "<li><b>Running MacroQuest?</b> Import → <b>⚡ Load from MQ AutoLogin</b> pulls your " +
      "whole roster, grouped by account, in one go.</li>" +
      "<li><b>No MacroQuest?</b> Import → paste a CSV " +
      "(<code>name,server,account,class,level</code>), or add them one at a time with " +
      "<b>+ Character</b> below.</li>" +
      "<li>Just want to look around first? Import → <b>Load sample roster</b>.</li>" +
      "</ul>" +
      "<button id='emptyGoImport' class='btn btn-accent' type='button'>Go to Import</button>" +
      "</div>";
    const go = $("emptyGoImport");
    if (go) go.addEventListener("click", () => gotoTab("import"));
    return;
  }

  for (const c of cards) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sumcard" + (c.k && quickFilter === c.k ? " on" : "");
    b.innerHTML = "<div class='num " + (c.cls || "") + "'>" + c.num + "</div><div class='lbl'>" + esc(c.lbl) + "</div>";
    b.addEventListener("click", () => {
      if (c.act) return c.act();
      quickFilter = quickFilter === c.k ? "" : c.k;
      renderSummary();
      renderChars();
    });
    row.appendChild(b);
  }
}

function matchesQuick(c) {
  if (!quickFilter) return true;
  const r = readiness(c);
  if (quickFilter === "cap") return (c.level || 0) >= CAP;
  if (quickFilter === "ready") return r.lv === "ready";
  if (quickFilter === "review")
    return r.lv === "bad" || r.lv === "warn" || ((c.level || 0) >= CAP && c.automation_status === "untested");
  return true;
}

// ---------- membership expiry ----------
// Me.SubscriptionDays is a FLOOR'd day count, so the server stores a window
// [sub_expires, sub_expires_max) rather than a point. Never render a bare
// timestamp from it — it looks exact and can be a full day early.
function subInfo(a) {
  if (a.membership === "FREE") return { state: "none", short: "NO SUB", long: "FREE — no sub" };
  if (!a.sub_expires) return null;
  const now = Date.now() / 1000;
  const lo = a.sub_expires, hi = a.sub_expires_max || a.sub_expires;
  const hrs = (t) => Math.max(0, Math.ceil((t - now) / 3600));
  const days = (t) => Math.floor((t - now) / 86400);
  if (now >= hi) return { state: "lapsed", short: "sub lapsed", long: "lapsed" };
  if (now >= lo)                                  // live, but inside the fuzzy window
    return { state: "critical", short: "sub <" + hrs(hi) + "h", long: "under " + hrs(hi) + "h left",
             note: "Can drop at any moment — apply a krono." };
  if (hi - now <= 48 * 3600)
    return { state: "critical", short: "sub " + hrs(lo) + "h", long: hrs(lo) + "–" + hrs(hi) + "h left",
             note: "Expires today." };
  const [d1, d2] = [days(lo), days(hi)];
  const span = d1 === d2 ? d1 + "d" : d1 + "–" + d2 + "d";
  return { state: d1 <= 7 ? "soon" : "ok", short: "sub " + span, long: span + " left" };
}

// Exact-looking dates are a lie here; show the span the reading actually proves.
function subTitle(a) {
  const lo = a.sub_expires, hi = a.sub_expires_max || a.sub_expires;
  if (!lo) return "Membership from the in-game export";
  const f = (t) => new Date(t * 1000).toLocaleString();
  return "Membership from the in-game export. In-game days-remaining is whole-day only, "
    + "so the true expiry is somewhere between " + f(lo) + " and " + f(hi) + ".";
}

// ---------- current login set ----------
function subWarn(a) {
  const s = subInfo(a);
  if (!s) return "";
  if (s.state === "none" || s.state === "lapsed")
    return " · <span class='st-bad'>" + s.short + "</span>";
  if (s.state === "critical" || s.state === "soon")
    return " · <span class='st-warn'>⏳ " + s.short + "</span>";
  return "";
}

function renderLoginSet() {
  const row = $("loginSetRow");
  row.innerHTML = "";
  for (const a of S.accounts) {
    const mine = S.characters.filter((c) => c.account_id === a.id && c.status !== "retired")
      .sort((x, y) => (y.level || 0) - (x.level || 0));
    const cur = loginSet[a.id];
    const div = document.createElement("div");
    div.className = "ls-slot";
    div.style.borderLeftColor = acctColor(a.id);
    const c = cur && charById(cur);
    div.innerHTML = "<div class='acct'>" + esc(acctLabel(a)) + "</div>" +
      "<select>" + mine.map((m) =>
        "<option value='" + m.id + "'" + (m.id === cur ? " selected" : "") + ">" +
        esc(m.name) + " — " + esc(m.class_name || "?") + " " + (m.level || "?") + "</option>").join("") +
      "</select>" +
      "<div class='cmeta'>" + (c ? "<span class='" + READY_CLS[readiness(c).lv] + "'>" +
        esc(readiness(c).text) + "</span> · " + esc(c.automation_status) + subWarn(a) : "no characters") + "</div>";
    const sel = div.querySelector("select");
    if (sel) sel.addEventListener("change", () => setLoginPick(a.id, parseInt(sel.value, 10)));
    row.appendChild(div);
  }
}

$("lsValidateBtn").addEventListener("click", () => guard(async () => {
  const resp = await api("POST", "/compositions/validate", { slots: loginSlots() });
  $("lsWarnings").innerHTML = resp.warnings.length
    ? resp.warnings.map((w) => "<div class='warn " + esc(w.level) + "'>" + esc(w.message) + "</div>").join("")
    : "<div class='warn ok'>Clean — all roles covered, no conflicts.</div>";
}));
$("lsBuilderBtn").addEventListener("click", () => {
  const slots = loginSlots().slice(0, 6);
  comp = { id: null, slots: slots.concat(Array(6 - slots.length).fill(null)) };
  $("compName").value = "Login Set";
  $("compNotes").value = "";
  $("compReq").value = "";
  selectedSlot = Math.max(0, comp.slots.indexOf(null));
  gotoTab("builder");
  renderSlots(); renderPicker(); refreshWarnings();
});
// Push = write the login set into MQ AutoLogin as a real profile group, so it
// shows up in the sidebar and can be right-click -> Launch All. Two refusals are
// expected and are NOT errors to swallow: the MQ loader being up (it caches the
// profile list, so a write behind it is invisible), and the group already
// existing (replacing it wipes whatever membership is in there now).
// Typing your own group name pins it — loading another comp stops overwriting it.
$("lsGroupName").addEventListener("input", () => { $("lsGroupName").dataset.fromComp = ""; });

$("lsPushBtn").addEventListener("click", () => guard(async () => {
  const push = async (replace) => {
    const r = await fetch("/roster/loginset/push", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slots: loginSlots(), group_name: $("lsGroupName").value.trim() || null,
                             replace: !!replace }),
    });
    return { status: r.status, body: await r.json().catch(() => ({})) };
  };
  let { status, body } = await push(false);
  if (status === 409 && body.exists) {
    const have = (body.current || []).map((c) => c.name).join(", ") || "(empty)";
    if (!confirm("AutoLogin already has a profile group '" + body.group + "':\n\n  " + have +
                 "\n\nReplace its members with your current login set?")) return;
    ({ status, body } = await push(true));
  }
  if (!body.ok) throw new Error(body.error || ("Push failed (" + status + ")"));
  toast("AutoLogin group '" + body.group + "': " + body.added.join(", ") +
        (body.missing && body.missing.length
          ? " · SKIPPED (AutoLogin has never seen them at char select): " +
            body.missing.map((m) => m.name).join(", ")
          : "") +
        " — open MQ and right-click the tray icon → Profiles → " + body.group);
}));
$("lsLaunchBtn").addEventListener("click", () => {
  const list = $("launchList");
  list.innerHTML = "";
  const sorted = S.accounts.slice().sort((a, b) => (a.launch_order || 99) - (b.launch_order || 99));
  for (const a of sorted) {
    const c = loginSet[a.id] && charById(loginSet[a.id]);
    if (!c) continue;
    const div = document.createElement("div");
    div.className = "launch-row";
    div.style.borderLeftColor = acctColor(a.id);
    div.innerHTML = "<span class='ord'>" + (a.launch_order || "?") + ".</span>" +
      "<b>" + esc(c.name) + "</b> <span class='hint'>" + esc(c.class_name || "?") + " " +
      (c.level || "?") + " · " + esc(acctLabel(a)) + "</span>" +
      "<span class='grp'>" + esc(a.autologin_group || "no AL group") + "</span>";
    list.appendChild(div);
  }
  $("launchDlg").showModal();
});

// ---------- accounts ----------
function tierOf(c) {
  if ((c.level || 0) >= CAP) return "core";
  if ((c.level || 0) <= 1) return "util";
  return "alt";
}

function renderAccounts() {
  const grid = $("acctGrid");
  grid.innerHTML = "";
  for (const a of S.accounts) {
    const mine = S.characters.filter((c) => c.account_id === a.id);
    const pick = loginSet[a.id] && charById(loginSet[a.id]);
    const tiers = { core: [], alt: [], util: [] };
    for (const c of mine) if (!pick || c.id !== pick.id) tiers[tierOf(c)].push(c);
    const open = expandedAccts.has(a.id);

    const div = document.createElement("div");
    div.className = "acct-card";
    div.style.borderLeftColor = acctColor(a.id);

    let memberHtml = "";
    if (a.membership) {
      const s = subInfo(a);
      const cls = !s ? "st-ready"
        : s.state === "none" || s.state === "lapsed" || s.state === "critical" ? "st-bad"
        : s.state === "soon" ? "st-warn" : "st-ready";
      const label = !s ? a.membership
        : s.state === "none" ? s.long
        : a.membership + " · " + s.long;
      memberHtml = " <span class='acct-status " + cls + "' title='" + esc(subTitle(a)) +
        (s && s.note ? " " + s.note : "") + "'>" + esc(label) + "</span>";
    }

    const chip = (c) =>
      "<span class='chip toon" + (pick && c.id === pick.id ? " picked" : "") + "' data-cid='" + c.id +
      "' title='Click: make " + esc(c.name) + " the login pick for " + esc(acctLabel(a)) + "'>" +
      esc(c.name) + " · " + clsSpan(c.class_name, c.class_name || "?") + " " + (c.level == null ? "?" : c.level) + "</span>";

    let html =
      "<h3><span style='color:" + acctColor(a.id) + "'>●</span> " + esc(acctLabel(a)) +
      " <span class='acct-status " + esc(a.status) + "'>" + esc(a.status) + "</span>" + memberHtml +
      "<button class='gear' type='button' title='Edit account'>⚙</button></h3>" +
      "<div class='meta'>#" + esc(a.account_number || "?") + " · launch " + (a.launch_order || 0) +
      " · <b>" + esc(a.autologin_group || "no group") + "</b>" +
      (a.notes ? " · " + esc(a.notes) : "") + "</div>";
    if (a.perks.length)
      html += "<div class='chars' style='margin-top:5px'>" + a.perks.map((p) => {
        const ci = claimInfo(p);
        return "<span class='chip claim' title='" + esc(ci ? ci.earned + " — " + ci.use : "account-wide perk") +
          "'>✓ " + esc(p) + "</span>";
      }).join("") + "</div>";
    if (pick) {
      const r = readiness(pick);
      html += "<div class='primary' data-cid='" + pick.id + "' title='Login pick — click to edit'>" +
        "<span class='cname'>" + esc(pick.name) + "</span>" +
        "<span class='cmeta'>" + clsSpan(pick.class_name, pick.class_name || "?") + " " + (pick.level || "?") +
        " · <span class='" + READY_CLS[r.lv] + "'>" + esc(r.text) + "</span></span>" +
        "<span class='star'>★ login</span></div>";
    }
    if (tiers.core.length)
      html += "<div class='tier'><div class='tierlbl'>Core</div><div class='chars'>" +
        tiers.core.map(chip).join("") + "</div></div>";
    const hidden = tiers.alt.length + tiers.util.length;
    if (open) {
      if (tiers.alt.length)
        html += "<div class='tier'><div class='tierlbl'>Alternates</div><div class='chars'>" +
          tiers.alt.map(chip).join("") + "</div></div>";
      if (tiers.util.length)
        html += "<div class='tier'><div class='tierlbl'>Utility / Bank</div><div class='chars'>" +
          tiers.util.map(chip).join("") + "</div></div>";
      html += "<div class='tier'><span class='chip more'>show less</span></div>";
    } else if (hidden) {
      html += "<div class='tier'><span class='chip more'>+" + hidden + " more</span></div>";
    }
    div.innerHTML = html;

    div.querySelector(".gear").addEventListener("click", () => openAcctDlg(a));
    const prim = div.querySelector(".primary");
    if (prim) prim.addEventListener("click", () => openCharDlg(charById(parseInt(prim.dataset.cid, 10))));
    div.querySelectorAll(".chip.toon").forEach((el) =>
      el.addEventListener("click", () => setLoginPick(a.id, parseInt(el.dataset.cid, 10))));
    const more = div.querySelector(".chip.more");
    if (more) more.addEventListener("click", () => {
      if (expandedAccts.has(a.id)) expandedAccts.delete(a.id); else expandedAccts.add(a.id);
      renderAccounts();
    });
    grid.appendChild(div);
  }
}

function renderAcctModeBtn() {
  $("acctModeBtn").textContent = acctDisplay === "login" ? "👁 login names" : "👁 nicknames";
}
$("acctModeBtn").addEventListener("click", () => {
  acctDisplay = acctDisplay === "login" ? "nick" : "login";
  localStorage.setItem("eqmc-acctdisplay", acctDisplay);
  renderAcctModeBtn();
  renderAll();
});

$("loginNamesBtn").addEventListener("click", () => guard(async () => {
  if (!confirm("Rename account aliases to your AutoLogin usernames?\n\nReads ONLY the username column from login.db — passwords are never touched. You can hand-edit any alias afterwards (⚙ on the card).")) return;
  const res = await api("POST", "/autologin/aliases", {});
  const n = Object.keys(res.renamed).length;
  toast(n ? "Renamed " + n + " account(s) to login names." : "Nothing to rename — aliases already match.");
  if (res.skipped_duplicates.length)
    toast("Skipped (duplicate name): " + res.skipped_duplicates.join(", "), true);
  await reload();
}));

$("scanClaimsBtn").addEventListener("click", () => guard(async () => {
  const res = await api("POST", "/perks/scan", {});
  if (res.found === false) throw new Error(res.error || "EQ dump folder not found.");
  const mem = await api("POST", "/membership/load", {});
  const hits = Object.entries(res.applied || {});
  const parts = ["Scanned " + res.dumps_scanned + " dump(s)."];
  parts.push(hits.length
    ? "New claims: " + hits.map(([a, cs]) => a + " (" + cs.join(", ") + ")").join("; ")
    : "No new claims.");
  parts.push(mem.found && mem.rows_matched
    ? "Membership updated on " + mem.rows_matched + " account(s)."
    : "No membership data yet — run /lua run mychars_export on a toon per account.");
  toast(parts.join(" "));
  await reload();
}));

function openAcctDlg(a) {
  editAcctId = a ? a.id : null;
  $("acctDlgTitle").textContent = a ? "Account: " + a.alias : "New account";
  $("aAlias").value = a ? a.alias : "";
  $("aNickname").value = a ? (a.nickname || "") : "";
  $("aNumber").value = a ? a.account_number : "";
  $("aStatus").value = a ? a.status : "active";
  $("aGroup").value = a ? a.autologin_group : "";
  $("aOrder").value = a ? (a.launch_order || 0) : S.accounts.length + 1;
  $("aPerks").value = a ? a.perks.join(", ") : "";
  $("aNotes").value = a ? a.notes : "";
  $("aDeleteBtn").hidden = !a;
  $("acctDlg").showModal();
}

$("addAcctBtn").addEventListener("click", () => openAcctDlg(null));
$("aSaveBtn").addEventListener("click", () => guard(async () => {
  const payload = {
    alias: $("aAlias").value.trim(), nickname: $("aNickname").value.trim(),
    account_number: $("aNumber").value.trim(),
    status: $("aStatus").value, autologin_group: $("aGroup").value.trim(),
    launch_order: parseInt($("aOrder").value, 10) || 0,
    perks: $("aPerks").value.split(",").map((s) => s.trim()).filter(Boolean),
    notes: $("aNotes").value.trim(),
  };
  if (!payload.alias) throw new Error("Alias is required.");
  if (editAcctId) await api("PUT", "/accounts/" + editAcctId, payload);
  else await api("POST", "/accounts", payload);
  $("acctDlg").close();
  await reload();
}));
$("aDeleteBtn").addEventListener("click", () => guard(async () => {
  const a = acctById(editAcctId);
  const n = S.characters.filter((c) => c.account_id === editAcctId).length;
  if (!confirm("Delete " + a.alias + "? Its " + n + " character(s) stay but become unassigned.")) return;
  await api("DELETE", "/accounts/" + editAcctId);
  $("acctDlg").close();
  await reload();
}));

// ---------- characters table ----------
function renderCharFilters() {
  const keep = (sel, opts, label) => {
    const cur = sel.value;
    sel.innerHTML = "<option value=''>" + label + "</option>" +
      opts.map((o) => "<option value='" + esc(o.v) + "'>" + esc(o.t) + "</option>").join("");
    sel.value = cur;
  };
  keep($("fAcct"), S.accounts.map((a) => ({ v: a.id, t: acctLabel(a) })), "All accounts");
  keep($("fClass"), [...new Set(S.characters.map((c) => c.class_name).filter(Boolean))].sort()
    .map((c) => ({ v: c, t: c })), "All classes");
  keep($("fStatus"), ["active", "parked", "planned", "retired"].map((s) => ({ v: s, t: s })), "All statuses");
}
["fAcct", "fClass", "fStatus"].forEach((id) => $(id).addEventListener("change", renderChars));
$("fSearch").addEventListener("input", renderChars);

function closeRowMenus() {
  document.querySelectorAll(".rowmenu").forEach((m) => m.remove());
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".rowmenu") && !e.target.closest(".rowmenu-btn")) closeRowMenus();
});

function drawerHtml(c) {
  const raidName = (rid) => (S.raids.find((r) => r.id === rid) || { name: "?" }).name;
  const caps = Object.entries(c.caps).filter(([, v]) => v).map(([k]) =>
    "<span class='chip cap" + (k in c.overrides ? " ovr" : "") + "' title='" +
    esc(capLabel(k)) + (k in c.overrides ? " (manual override)" : " (class default)") + "'>" +
    esc(capLabel(k)) + "</span>").join("") || "<span class='hint'>none</span>";
  const offCaps = Object.entries(c.overrides).filter(([, v]) => v === false).map(([k]) =>
    "<span class='chip' style='text-decoration:line-through' title='Forced OFF'>" + esc(capLabel(k)) + "</span>").join("");
  const unlocks = S.unlocks.filter((u) => u.character_id === c.id).map((u) =>
    "<span class='chip ul-" + esc(u.status).replace(/ /g, "") + "' title='" + esc(u.priority) + " · " +
    esc(u.category) + (u.verified_date ? " · verified " + esc(u.verified_date) : "") + "'>" +
    esc(u.name) + " — " + esc(u.status) + "</span>").join("") || "<span class='hint'>none tracked</span>";
  const locks = c.lockouts.map((lk) => {
    const rem = fmtRemain(lk.expires_at);
    return "<span class='chip lock'>" + esc(raidName(lk.raid_id)) + (rem ? " · " + rem : "") + "</span>";
  }).join("") || "<span class='hint'>not saved to anything</span>";
  return "<div class='drawer'>" +
    "<div><h5>Capabilities</h5><div class='chips'>" + caps + (offCaps ? offCaps : "") + "</div></div>" +
    "<div><h5>Keys &amp; Access</h5><div class='chips'>" + unlocks + "</div></div>" +
    "<div><h5>Raid Lockouts</h5><div class='chips'>" + locks + "</div></div>" +
    "<div><h5>Details</h5><div class='kv'>" +
    "Server <b>" + esc(c.server) + "</b> · Race <b>" + esc(c.race || "?") + "</b> · " +
    "Epic <b>" + esc(c.epic_status || "—") + "</b> · Gear <b>" + esc(c.gear_tier || "—") + "</b><br>" +
    "Port-bot whitelist <b>" + (c.portbot_whitelist ? "yes" : "no") + "</b> · Status <b>" + esc(c.status) + "</b>" +
    (c.notes ? "<br>Notes: " + esc(c.notes) : "") + "</div>" +
    "<div style='margin-top:6px'><button class='btn btn-ghost btn-sm drawer-edit' type='button'>Edit character</button></div></div>" +
    "</div>";
}

function renderChars() {
  closeRowMenus();
  const fa = $("fAcct").value, fc = $("fClass").value, fs = $("fStatus").value,
    q = $("fSearch").value.trim().toLowerCase();
  const tag = $("quickFilterTag");
  tag.hidden = !quickFilter;
  tag.textContent = { cap: "filter: level-capped", ready: "filter: raid ready", review: "filter: needs review" }[quickFilter] || "";
  const tbody = $("charRows");
  tbody.innerHTML = "";
  const chars = S.characters.slice().sort((a, b) => {
    const ao = (acctById(a.account_id) || {}).launch_order || 99;
    const bo = (acctById(b.account_id) || {}).launch_order || 99;
    return ao - bo || (b.level || 0) - (a.level || 0) || a.name.localeCompare(b.name);
  });
  for (const c of chars) {
    if (fa && c.account_id !== parseInt(fa, 10)) continue;
    if (fc && c.class_name !== fc) continue;
    if (fs && c.status !== fs) continue;
    if (q && !(c.name + " " + (c.group_tags || "") + " " + (c.notes || "")).toLowerCase().includes(q)) continue;
    if (!matchesQuick(c)) continue;

    const r = readiness(c);
    const iss = charIssues(c);
    const isPick = loginSet[c.account_id] === c.id;
    const team = (c.group_tags || "").split(",").map((s) => s.trim()).filter(Boolean)
      .map((t) => "<span class='chip'>" + esc(t) + "</span>").join(" ");
    const issues = iss.map((i) =>
      "<span class='issue " + (i.lv === "bad" ? "st-bad" : i.lv === "warn" ? "st-warn" : "st-dim") + "'>" +
      esc(i.text) + "</span>").join("") || "<span class='st-dim'>—</span>";

    const tr = document.createElement("tr");
    tr.className = "mainrow";
    tr.innerHTML =
      "<td style='border-left-color:" + acctColor(c.account_id) + "'><span class='name'>" + esc(c.name) +
      (isPick ? "<span class='pickstar' title='Current login pick for this account'>★</span>" : "") + "</span></td>" +
      "<td style='color:" + acctColor(c.account_id) + "'>" + esc(charAcctLabel(c)) + "</td>" +
      "<td>" + clsSpan(c.class_name, c.class_name || "?") + " <b>" + (c.level == null ? "?" : c.level) + "</b></td>" +
      "<td>" + esc(c.main_role || "") + "</td>" +
      "<td class='auto-" + esc(c.automation_status) + "'>" + esc(c.automation_status) + "</td>" +
      "<td><span class='" + READY_CLS[r.lv] + "'>" + esc(r.text) + "</span></td>" +
      "<td>" + team + "</td>" +
      "<td>" + issues + "</td>" +
      "<td><button class='rowmenu-btn' type='button' title='Actions'>⋮</button></td>";
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".rowmenu-btn")) return;
      expandedChar = expandedChar === c.id ? null : c.id;
      renderChars();
    });
    tr.querySelector(".rowmenu-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      const existing = document.querySelector(".rowmenu");
      closeRowMenus();
      if (existing) return;
      const menu = document.createElement("div");
      menu.className = "rowmenu";
      menu.innerHTML =
        "<button type='button' data-act='details'>" + (expandedChar === c.id ? "Hide details" : "Details") + "</button>" +
        "<button type='button' data-act='edit'>Edit</button>" +
        "<button type='button' data-act='pick'>Set as login pick</button>" +
        "<button type='button' data-act='delete' class='danger'>Delete</button>";
      document.body.appendChild(menu);
      const rect = e.target.getBoundingClientRect();
      menu.style.left = Math.max(8, rect.right - 170 + window.scrollX) + "px";
      menu.style.top = (rect.bottom + 4 + window.scrollY) + "px";
      menu.addEventListener("click", (ev) => {
        const act = ev.target.dataset && ev.target.dataset.act;
        closeRowMenus();
        if (act === "details") { expandedChar = expandedChar === c.id ? null : c.id; renderChars(); }
        if (act === "edit") openCharDlg(c);
        if (act === "pick") { if (c.account_id != null) setLoginPick(c.account_id, c.id); else toast("No account assigned.", true); }
        if (act === "delete") guard(async () => {
          if (!confirm("Delete " + c.name + "? Unlocks and lockouts go with it.")) return;
          await api("DELETE", "/characters/" + c.id);
          await reload();
        });
      });
    });
    tbody.appendChild(tr);

    if (expandedChar === c.id) {
      const dtr = document.createElement("tr");
      dtr.className = "detailrow";
      dtr.innerHTML = "<td colspan='9' style='border-left-color:" + acctColor(c.account_id) + "'>" +
        drawerHtml(c) + "</td>";
      dtr.querySelector(".drawer-edit").addEventListener("click", () => openCharDlg(c));
      tbody.appendChild(dtr);
    }
  }
}

// ---------- character dialog ----------
function renderCapPills() {
  const cls = $("cClass").value;
  const defaults = new Set((S.meta.class_defaults[cls] || []));
  const grid = $("capsGrid");
  grid.innerHTML = "";
  for (const cap of S.meta.capabilities) {
    const pill = document.createElement("span");
    const ov = capOverrides[cap.key];                 // true | false | undefined
    const on = ov === undefined ? defaults.has(cap.key) : ov;
    pill.className = "cappill " +
      (ov === true ? "ovr-on" : ov === false ? "ovr-off" : on ? "on" : "");
    pill.textContent = cap.label + (ov === undefined ? "" : " *");
    pill.title = ov === undefined
      ? "Class default: " + (on ? "yes" : "no") + " — click to force ON"
      : ov ? "Forced ON — click to force OFF" : "Forced OFF — click to reset to class default";
    pill.addEventListener("click", () => {
      if (ov === undefined) capOverrides[cap.key] = true;
      else if (ov === true) capOverrides[cap.key] = false;
      else delete capOverrides[cap.key];
      renderCapPills();
    });
    grid.appendChild(pill);
  }
}

function openCharDlg(c) {
  editCharId = c ? c.id : null;
  capOverrides = c ? { ...c.overrides } : {};
  $("charDlgTitle").textContent = c ? "Character: " + c.name : "New character";
  $("cName").value = c ? c.name : "";
  $("cServer").value = c ? c.server : "Frostreaver";
  $("cAccount").innerHTML = "<option value=''>— unassigned —</option>" +
    S.accounts.map((a) => "<option value='" + a.id + "'>" + esc(a.alias) + "</option>").join("");
  $("cAccount").value = c && c.account_id ? c.account_id : "";
  $("cClass").innerHTML = "<option value=''></option>" +
    S.meta.classes.map((k) => "<option>" + k + "</option>").join("");
  $("cClass").value = c ? c.class_name : "";
  $("cLevel").value = c && c.level != null ? c.level : "";
  // race dropdown (bit order matches the item DB / RACE_BITS in gear.py)
  const RACES = ["Human", "Barbarian", "Erudite", "Wood Elf", "High Elf", "Dark Elf",
    "Half Elf", "Dwarf", "Troll", "Ogre", "Halfling", "Gnome",
    "Iksar", "Vah Shir", "Froglok", "Drakkin"];
  $("cRace").innerHTML = "<option value=''>— unknown —</option>" +
    RACES.map((r) => "<option value='" + r + "'>" + r + "</option>").join("") +
    (c && c.race && !RACES.includes(c.race)
      ? "<option value='" + esc(c.race) + "'>" + esc(c.race) + " (legacy)</option>" : "");
  $("cRace").value = c ? (c.race || "") : "";
  $("cRaceHint").textContent = "";
  if (c) {                       // guess from race-LOCKED worn gear in the newest dump
    api("GET", "/gear/raceguess?char_id=" + c.id).then((g) => {
      if (!g.evidence || !g.evidence.length) return;
      for (const o of $("cRace").options)
        if (g.possible.includes(o.value)) o.text = o.value + " ✓ fits worn gear";
      $("cRaceHint").textContent = g.possible.length
        ? "worn gear says: " + g.possible.join(" or ") +
          " (e.g. " + g.evidence[0].name + ")"
        : "⚠ worn race-locked gear conflicts — check the dump";
      if (!$("cRace").value && g.possible.length === 1)
        $("cRace").value = g.possible[0];
    }).catch(() => {});
  }
  $("cRole").value = c ? c.main_role : "";
  $("cStatus").value = c ? c.status : "active";
  $("cTags").value = c ? c.group_tags : "";
  $("cGear").value = c ? c.gear_tier : "";
  $("cEpic").value = c ? c.epic_status : "";
  $("cAuto").value = c ? c.automation_status : "untested";
  $("cPortbot").checked = !!(c && c.portbot_whitelist);
  $("cNotes").value = c ? c.notes : "";
  renderCapPills();
  const locks = new Set(c ? c.lockout_raid_ids : []);
  $("lockChecks").innerHTML = S.raids.map((r) =>
    "<label><input type='checkbox' data-raid='" + r.id + "'" + (locks.has(r.id) ? " checked" : "") +
    "> " + esc(r.name) + "</label>").join("") || "<span class='hint'>no raids defined yet</span>";
  $("cDeleteBtn").hidden = !c;
  $("charDlg").showModal();
}

$("cClass").addEventListener("change", renderCapPills);
$("addCharBtn").addEventListener("click", () => openCharDlg(null));
$("cSaveBtn").addEventListener("click", () => guard(async () => {
  const payload = {
    name: $("cName").value.trim(), server: $("cServer").value.trim() || "Frostreaver",
    account_id: $("cAccount").value ? parseInt($("cAccount").value, 10) : null,
    class_name: $("cClass").value, level: $("cLevel").value ? parseInt($("cLevel").value, 10) : null,
    race: $("cRace").value.trim(), main_role: $("cRole").value, status: $("cStatus").value,
    group_tags: $("cTags").value.trim(), gear_tier: $("cGear").value.trim(),
    epic_status: $("cEpic").value, portbot_whitelist: $("cPortbot").checked ? 1 : 0,
    automation_status: $("cAuto").value, notes: $("cNotes").value.trim(),
  };
  if (!payload.name) throw new Error("Name is required.");
  let id = editCharId;
  if (id) await api("PUT", "/characters/" + id, payload);
  else id = (await api("POST", "/characters", payload)).id;
  const overrides = {};
  for (const cap of S.meta.capabilities)
    overrides[cap.key] = cap.key in capOverrides ? capOverrides[cap.key] : null;
  await api("PUT", "/characters/" + id + "/capabilities", { overrides });
  const raidIds = [...$("lockChecks").querySelectorAll("input:checked")]
    .map((el) => parseInt(el.dataset.raid, 10));
  await api("PUT", "/characters/" + id + "/lockouts", { raid_ids: raidIds });
  $("charDlg").close();
  await reload();
}));
$("cDeleteBtn").addEventListener("click", () => guard(async () => {
  const c = charById(editCharId);
  if (!confirm("Delete " + c.name + "? Unlocks and lockouts go with it.")) return;
  await api("DELETE", "/characters/" + editCharId);
  $("charDlg").close();
  await reload();
}));

document.querySelectorAll(".dlg-cancel").forEach((b) =>
  b.addEventListener("click", () => b.closest("dialog").close()));

// ---------- comp builder ----------
function renderBuilder() {
  const sel = $("compSelect");
  const cur = comp.id || "";
  sel.innerHTML = "<option value=''>— new composition —</option>" +
    S.compositions.map((c) => "<option value='" + c.id + "'>" + esc(c.name) + "</option>").join("");
  sel.value = cur;
  renderSlots();
  renderPicker();
  renderReqChips();
  refreshWarnings();
  const names = new Set();
  S.unlocks.forEach((u) => names.add(u.name));
  S.meta.unlock_examples.forEach((u) => names.add(u.name));
  $("reqDatalist").innerHTML = [...names].sort().map((n) => "<option value='" + esc(n) + "'>").join("");
}

function loadComp(id) {
  const c = S.compositions.find((x) => x.id === id);
  if (!c) {
    comp = { id: null, slots: [null, null, null, null, null, null] };
    $("compName").value = ""; $("compNotes").value = ""; $("compReq").value = "";
  } else {
    comp = { id: c.id, slots: [...c.slots] };
    while (comp.slots.length < 6) comp.slots.push(null);
    $("compName").value = c.name; $("compNotes").value = c.notes; $("compReq").value = c.required_unlocks;
  }
  $("autoResult").innerHTML = "";
  // Name the AutoLogin launch group after the comp, not "eqf login set". Once every
  // comp has its own gear sets, the group name is the only thing telling you which
  // six the tray icon is about to launch. AutoLogin stores names lowercase.
  const gn = $("lsGroupName");
  if (gn && (!gn.value.trim() || gn.dataset.fromComp === "1")) {
    gn.value = c ? c.name.toLowerCase() : "";
    gn.dataset.fromComp = c ? "1" : "";
  }
  selectedSlot = comp.slots.indexOf(null) === -1 ? 0 : comp.slots.indexOf(null);
  renderSlots(); renderPicker(); refreshWarnings();
}

// ---------- auto-fill ----------
const REQ_CHIP_DEFS = [["tank", "Tank"], ["healer", "Healer"], ["slow", "Slow"],
  ["haste", "Haste"], ["cc", "CC"], ["rez", "Rez"], ["ports", "Ports/Evac"],
  ["puller", "Puller"], ["mana_regen", "Mana Regen"], ["coth", "CoH"], ["tracking", "Track"]];
const reqSel = new Set();

function renderReqChips() {
  const box = $("reqChips");
  box.innerHTML = "";
  const all = [...REQ_CHIP_DEFS, ...[...reqSel].filter((r) =>
    !REQ_CHIP_DEFS.some(([k]) => k === r)).map((r) => [r, r])];
  for (const [key, label] of all) {
    const chip = document.createElement("span");
    chip.className = "reqchip" + (reqSel.has(key) ? " on" : "");
    chip.textContent = label;
    chip.addEventListener("click", () => {
      if (reqSel.has(key)) reqSel.delete(key); else reqSel.add(key);
      renderReqChips();
    });
    box.appendChild(chip);
  }
  const sel = $("reqClassSel");
  if (!sel.options.length || sel.options.length === 1) {
    sel.innerHTML = "<option value=''>+ class…</option>" +
      S.meta.classes.map((c) => "<option>" + c + "</option>").join("");
  }
}
$("reqClassSel").addEventListener("change", () => {
  if ($("reqClassSel").value) { reqSel.add($("reqClassSel").value); $("reqClassSel").value = ""; renderReqChips(); }
});

$("autoFillBtn").addEventListener("click", () => guard(async () => {
  if (!reqSel.size) throw new Error("Pick at least one requirement chip first.");
  const res = await api("POST", "/compositions/auto",
    { requirements: [...reqSel], required_unlocks: $("compReq").value });
  const out = [];
  if (res.unmet.length)
    out.push("<div class='warn error'>Impossible with current 60s — unmet: " +
      esc(res.unmet.join(", ")) + ". Best attempt filled anyway.</div>");
  out.push(res.assignments.map((a) =>
    "<div class='assign'>" + esc(a.label) + " → " +
    (a.toon ? "<b>" + esc(a.toon) + "</b>" + (a.tier === 1 ? " <span class='tier1'>(fallback — risky)</span>" : "")
            : "<span class='tier0'>nobody</span>") + "</div>").join(""));
  for (const w of res.warnings)
    out.push("<div class='warn warn'>" + esc(w) + "</div>");
  $("autoResult").innerHTML = out.join("");
  if (res.slots.length) {
    const bench = comp.slots.slice(6);
    comp.slots = res.slots.slice(0, 6);
    while (comp.slots.length < 6) comp.slots.push(null);
    comp.slots.push(...bench);
    selectedSlot = 0;
    renderSlots(); renderPicker(); refreshWarnings();
  }
}));

$("benchAddBtn").addEventListener("click", () => {
  comp.slots.push(null);
  selectedSlot = comp.slots.length - 1;
  renderSlots(); renderPicker();
});

$("compSelect").addEventListener("change", () => loadComp(parseInt($("compSelect").value, 10) || null));
$("compNewBtn").addEventListener("click", () => { $("compSelect").value = ""; loadComp(null); });

function renderSlots() {
  const grid = $("slotGrid");
  const bench = $("benchGrid");
  grid.innerHTML = "";
  bench.innerHTML = "";
  comp.slots.forEach((cid, i) => {
    const isBench = i >= 6;
    const c = cid ? charById(cid) : null;
    const div = document.createElement("div");
    div.className = "slot" + (c ? " filled" : "") + (i === selectedSlot ? " selected" : "");
    if (c) div.style.borderLeftColor = acctColor(c.account_id);
    div.innerHTML = "<span class='slotno'>" + (isBench ? "B" + (i - 5) : i + 1) + "</span>" +
      (c ? "<div class='cname'>" + esc(c.name) + "</div>" +
           "<div class='cmeta'>" + clsSpan(c.class_name, c.class_name || "?") + " " + (c.level || "?") +
           " · " + esc(charAcctLabel(c)) + "</div>" +
           slotGearHtml(c) +
           "<button class='clear' type='button' title='" + (isBench ? "Remove bench slot" : "Clear slot") + "'>✕</button>"
         : "<div class='empty'>click a character →</div>");
    div.addEventListener("click", () => { selectedSlot = i; renderSlots(); });
    const sg = div.querySelector(".slot-gear");
    if (sg) sg.addEventListener("click", (e) => {
      e.stopPropagation();
      guard(() => gearJump(parseInt(sg.dataset.cid, 10)));
    });
    const clr = div.querySelector(".clear");
    if (clr) clr.addEventListener("click", (e) => {
      e.stopPropagation();
      if (isBench) { comp.slots.splice(i, 1); selectedSlot = 0; }
      else { comp.slots[i] = null; selectedSlot = i; }
      renderSlots(); renderPicker(); refreshWarnings();
    });
    (isBench ? bench : grid).appendChild(div);
  });
  bench.parentElement && (bench.innerHTML === "" ? bench.setAttribute("data-empty", "1") : bench.removeAttribute("data-empty"));
}

function renderPicker() {
  const list = $("pickerList");
  list.innerHTML = "";
  const usedAccts = new Map();      // account_id -> char id using it (LIVE slots only)
  comp.slots.slice(0, 6).forEach((cid) => {
    const c = cid && charById(cid);
    if (c && c.account_id != null) usedAccts.set(c.account_id, c.id);
  });
  const targetIsBench = selectedSlot >= 6;    // bench is exempt from the account rule
  const chars = S.characters.filter((c) => c.status !== "retired");
  for (const c of chars) {
    const inComp = comp.slots.includes(c.id);
    const blocked = !inComp && !targetIsBench && c.account_id != null && usedAccts.has(c.account_id);
    const div = document.createElement("div");
    div.className = "pick" + (inComp ? " inuse" : "") + (blocked ? " blocked" : "");
    div.style.borderLeftWidth = "3px";
    div.style.borderLeftColor = acctColor(c.account_id);
    const capNames = Object.entries(c.caps).filter(([, v]) => v).map(([k]) => capLabel(k));
    const capsShort = capNames.slice(0, 4).join(", ") + (capNames.length > 4 ? " +" + (capNames.length - 4) : "");
    div.innerHTML =
      "<span class='cname'>" + esc(c.name) + "</span>" +
      "<span class='cmeta' title='" + esc(capNames.join(", ")) + "'>" + clsSpan(c.class_name, c.class_name || "?") + " " +
      (c.level || "?") + " · " + esc(capsShort) + "</span>" +
      "<span class='acct'>" + esc(charAcctLabel(c)) +
      (blocked ? " ⛔ in use by " + esc(charById(usedAccts.get(c.account_id)).name) : "") + "</span>";
    div.title = blocked
      ? "Account conflict: " + charAcctLabel(c) + " already has a character in this comp"
      : inComp ? "Click to remove from the comp" : "Click to add to slot " + (selectedSlot + 1);
    div.addEventListener("click", () => {
      if (blocked) { toast("Only one character per account — " + charAcctLabel(c) + " is taken.", true); return; }
      if (inComp) {
        comp.slots[comp.slots.indexOf(c.id)] = null;
      } else {
        comp.slots[selectedSlot] = c.id;
        const next = comp.slots.indexOf(null);
        if (next !== -1) selectedSlot = next;
      }
      renderSlots(); renderPicker(); refreshWarnings();
    });
    list.appendChild(div);
  }
}

// ---------- comp power panel ----------
let gearCache = null;                            // /gear/summary rows keyed by character_id

async function gearData() {
  if (!gearCache) {
    const d = await api("GET", "/gear/summary");
    gearCache = { byId: {}, meta: d.stats_meta };
    d.rows.forEach((r) => { gearCache.byId[r.character_id] = r; });
  }
  return gearCache;
}

const POWER_COLS = ["ac", "hp", "mana", "regen", "manaregen", "attack"];
const POWER_RESISTS = ["mr", "fr", "cr", "dr", "pr"];
const COVERAGE_ROLES = [["tank", "Tank"], ["healing", "Heal"], ["slow", "Slow"],
  ["haste", "Haste"], ["cc", "CC"], ["resurrection", "Rez"],
  ["ports", "Ports"], ["evac", "Evac"], ["pulling", "Pull"], ["mana_regen", "Crack"]];

function fxCell(list) {
  if (!list || !list.length) return "<span class='fxcount none'>—</span>";
  return "<span class='fxcount' title='" + esc(list.join("\n")) + "'>" + list.length + "</span>";
}

async function renderPower() {
  const panel = $("powerPanel");
  const members = comp.slots.slice(0, 6).filter(Boolean).map(charById).filter(Boolean);
  if (!members.length) { panel.innerHTML = ""; return; }
  let g;
  try { g = await gearData(); }
  catch (e) { panel.innerHTML = "<div class='hint'>" + esc(e.message) + "</div>"; return; }

  // role coverage strip (from capabilities, not gear)
  const cov = COVERAGE_ROLES.map(([cap, label]) => {
    const holders = members.filter((m) => m.caps[cap]);
    const cls = holders.length ? "yes" : "no";
    return "<span class='covchip " + cls + "' title='" +
      esc(holders.map((h) => h.name).join(", ") || "nobody") + "'>" +
      (holders.length ? "✓ " : "") + esc(label) + "</span>";
  }).join("");

  const labels = Object.fromEntries(g.meta.map((m) => [m.key, m.label]));
  let head = "<tr><th>Toon</th>" + POWER_COLS.map((k) => "<th>" + esc(labels[k] || k) + "</th>").join("") +
    "<th>Haste</th>" + POWER_RESISTS.map((k) => "<th>" + esc(labels[k]) + "</th>").join("") +
    "<th title='Focus effects on worn gear — inactive until Luclin, but you own them'>Focus</th>" +
    "<th title='Click effects on worn gear'>Clicky</th>" +
    "<th title='Passive worn effects (damage shields etc.)'>WornFX</th>" +
    "<th title='Weapon procs'>Proc</th><th>Dump</th></tr>";
  const sums = {};
  const rows = members.map((m) => {
    const r = g.byId[m.id];
    if (!r) {
      return "<tr><td style='border-left:3px solid " + acctColor(m.account_id) + "'>" + esc(m.name) +
        "</td><td colspan='" + (POWER_COLS.length + POWER_RESISTS.length + 6) +
        "' class='hint' style='text-align:left'>no inventory dump — /outputfile inventory</td></tr>";
    }
    POWER_COLS.forEach((k) => { sums[k] = (sums[k] || 0) + (r.totals[k] || 0); });
    const age = r.dump_age_h < 24 ? r.dump_age_h + "h" : Math.floor(r.dump_age_h / 24) + "d";
    return "<tr><td style='border-left:3px solid " + acctColor(m.account_id) + "'><b>" + esc(m.name) +
      "</b> " + clsSpan(m.class_name, (m.class_name || "?").slice(0, 3)) + "</td>" +
      POWER_COLS.map((k) => "<td>" + (r.totals[k] || 0) + "</td>").join("") +
      "<td class='hastecell' title='" + esc(r.haste_item || "no worn haste") + "'>" +
      (r.haste ? r.haste + "%" : "—") + "</td>" +
      POWER_RESISTS.map((k) => "<td>" + (r.totals[k] || 0) + "</td>").join("") +
      "<td>" + fxCell(r.focuses) + "</td><td>" + fxCell(r.clickies) + "</td>" +
      "<td>" + fxCell(r.worneffects) + "</td><td>" + fxCell(r.procs) + "</td>" +
      "<td class='" + (r.dump_age_h > 72 ? "dumpold" : "hint") + "'>" + age + "</td></tr>";
  }).join("");
  const totals = "<tr class='totals'><td>Group</td>" +
    POWER_COLS.map((k) => "<td>" + (sums[k] || 0) + "</td>").join("") +
    "<td colspan='" + (POWER_RESISTS.length + 6) + "'></td></tr>";
  panel.innerHTML = "<div class='coverage'>" + cov + "</div>" +
    "<div class='tablewrap'><table>" + head + rows + totals + "</table></div>";
}

let warnTimer = null;
function refreshWarnings() {
  guard(renderPower);
  clearTimeout(warnTimer);
  warnTimer = setTimeout(() => guard(async () => {
    const resp = await api("POST", "/compositions/validate",
      { slots: comp.slots, required_unlocks: $("compReq").value });
    const panel = $("warnPanel");
    const filled = comp.slots.filter(Boolean).length;
    if (!filled) { panel.innerHTML = "<div class='hint'>Empty composition.</div>"; $("gearCheckPanel").innerHTML = ""; return; }
    panel.innerHTML = resp.warnings.length
      ? resp.warnings.map((w) => "<div class='warn " + esc(w.level) + "'>" + esc(w.message) + "</div>").join("")
      : "<div class='warn ok'>Clean — all roles covered, no conflicts.</div>";
    renderGearCheck(await api("POST", "/gearsets/compcheck",
      { slots: comp.slots.slice(0, 6).filter(Boolean) }));
    // Show which set this comp fields per member BEFORE Apply is pressed, so the
    // pickers are visible rather than only appearing after a rejected apply.
    if (comp.id)
      renderCompApply((await api("GET", "/gearsets/compmap/" + comp.id)).mapping, "");
    else
      renderCompApply(null, "");
  }), 150);
}

// Gear coverage for the LIVE six: does every member have an active set, and do
// enough physical copies exist across the whole roster to fill them all at once?
// Copies are fungible — EQ dumps have no per-instance identity, so 8 identical
// mantles are just "owned: 8" and any copy satisfies any set.
function renderGearCheck(gc) {
  const panel = $("gearCheckPanel");
  const out = [];
  for (const o of gc.overlaps) {
    out.push("<div class='warn error'>🎁 Gear overlap: <b>" + esc(o.item) + "</b> — " +
      o.need + " set(s) need it (" + esc(o.sets.join(", ")) + ") but you own " + o.owned +
      (o.owned === 0 ? " (nobody has one)" : "") + ".</div>");
  }
  if (gc.no_set.length)
    out.push("<div class='warn warn'>🎁 No gear set assigned: " + esc(gc.no_set.join(", ")) +
      " — use the 🎁 link on their slot card to snapshot one.</div>");
  for (const o of gc.outside) {
    out.push("<div class='warn warn'>🎁 Tight: <b>" + esc(o.item) + "</b> — this comp needs " +
      o.comp_need + " and <i>" + esc(o.outside_sets.join(", ")) + "</i> (outside this comp) " +
      "want(s) " + o.outside_need + " more, but you own " + o.owned + ". Fine to play — just don't gear both at once.</div>");
  }
  if (!out.length && gc.toons.length)
    out.push("<div class='warn ok'>🎁 Gear: " + gc.toons.length +
      " member(s) have sets · enough copies for everything.</div>");
  panel.innerHTML = out.join("");
}
$("compReq").addEventListener("input", refreshWarnings);

$("compSaveBtn").addEventListener("click", () => guard(async () => {
  const payload = { name: $("compName").value.trim(), notes: $("compNotes").value.trim(),
    required_unlocks: $("compReq").value.trim(), slots: comp.slots };
  const resp = comp.id
    ? await api("PUT", "/compositions/" + comp.id, payload)
    : await api("POST", "/compositions", payload);
  comp.id = resp.id;
  toast("Saved '" + payload.name + "'.");
  await reload();
  $("compSelect").value = comp.id;
}));
// ---------- apply a comp's gear sets ----------
// `active` drives contention, routing and comp_gear_check, so it is only coherent
// when the active group is ONE comp. The user ran two comps' sets active at once and
// single-copy pieces read as permanently contested (2026-08-09). This is a flag
// flip only — deactivating clears no picks and is undone by applying the other comp.
let compMap = null;

function renderCompApply(mapping, note) {
  compMap = mapping;
  const panel = $("compApplyPanel");
  if (!mapping || !mapping.length) { panel.innerHTML = ""; return; }
  const live = mapping.filter((m) => !m.bench);
  const rows = live.map((m) => {
    if (!m.candidates.length)
      return "<div class='warn warn'>🎯 <b>" + esc(m.character) + "</b> — no " +
        esc(m.class_name || "") + " loadout exists. Snapshot one from their slot card.</div>";
    if (m.candidates.length === 1)
      return "<div class='ca-row'><span class='ca-name'>" + esc(m.character) +
        "</span><span class='hint'>" + esc(m.candidates[0].name) +
        (m.candidates[0].used_by && m.candidates[0].used_by.length
           ? " · also in " + esc(m.candidates[0].used_by.join(", ")) : "") +
        (m.candidates[0].moves ? " · moves off " + esc(m.candidates[0].assigned_to) : "") +
        "</span></div>";
    // A loadout is a ROLE, so every set of this toon's class is offered — Rogue Main
    // has to be reachable from Gavriel even while it is pinned to Zyrak. Say out loud
    // when a pick takes the set off someone else, because that is not obvious.
    const opts = m.candidates.map((c) =>
      "<option value='" + c.id + "'" + (c.id === m.gear_set_id ? " selected" : "") + ">" +
      esc(c.name) + " (" + c.pieces + " pieces)" +
      (c.used_here ? " · ALREADY ON " + esc(c.used_here) + " IN THIS COMP" : "") +
      (c.used_by && c.used_by.length ? " · also in " + esc(c.used_by.join(", ")) : "") +
      (c.moves ? " · now on " + esc(c.assigned_to) : "") +
      "</option>").join("");
    return "<div class='ca-row" + (m.stored ? "" : " ca-row-ask") + "'>" +
      "<span class='ca-name'>" + esc(m.character) + "</span>" +
      "<select class='ca-pick' data-cid='" + m.character_id + "'>" + opts + "</select>" +
      (m.stored ? "" : "<span class='hint'>" + m.candidates.length + " " +
        esc(m.class_name || "") + " loadout(s) — pick the one this comp fields</span>") +
      "</div>";
  });
  panel.innerHTML = (note ? "<div class='warn warn'>🎯 " + esc(note) + "</div>" : "") +
    "<div class='ca-box'>" + rows.join("") + "</div>";
}

$("compApplyBtn").addEventListener("click", () => guard(async () => {
  if (!comp.id) { toast("Save this composition first.", true); return; }
  const choices = {};
  for (const el of document.querySelectorAll("#compApplyPanel .ca-pick"))
    choices[el.dataset.cid] = parseInt(el.value, 10);
  let res;
  try {
    res = await api("POST", "/gearsets/apply", { comp_id: comp.id, choices });
  } catch (e) {
    // 409 = a member owns several sets and none is confirmed yet. Show the pickers
    // and stop; nothing was activated. Deliberately NOT auto-resolved: "already
    // active" is not intent, it is how Zyrak ended up fielding Rogue Main.
    if (e.data && e.data.needs_choice) {
      renderCompApply(e.data.mapping, e.data.error);
      toast(e.data.error, true);
      return;
    }
    throw e;
  }
  renderCompApply(res.mapping, "");
  let msg = "Applied '" + res.comp + "' — " + res.active + " set(s) active.";
  if (res.retargeted && res.retargeted.length)
    msg += " Moved: " + res.retargeted.map((t) =>
      t.set + " → " + t.to + (t.from ? " (was " + t.from + ")" : "")).join(", ") + ".";
  if (res.deactivated.length) msg += " Deactivated: " + res.deactivated.join(", ") + ".";
  if (res.no_set.length) msg += " No set for: " + res.no_set.join(", ") + ".";
  toast(msg);
  await reload();
  refreshWarnings();
}));

$("compDeleteBtn").addEventListener("click", () => guard(async () => {
  if (!comp.id) { loadComp(null); return; }
  const name = $("compName").value || "this composition";
  if (!confirm("Delete " + name + "?")) return;
  await api("DELETE", "/compositions/" + comp.id);
  loadComp(null);
  await reload();
}));

// ---------- gear tab ----------
let gearLoaded = false;

// ---------- harvest coverage ----------
// Two independent sources on purpose (see mychars/harvest.py): the dumps say WHAT was
// captured, the run log says WHO was attempted. Only the pair can tell "the run reached
// this toon and wrote nothing" apart from "this toon simply wasn't in the queue".
let hvData = null;
let hvFilter = "";
const hvPicked = new Set();     // character_ids queued for the next armed run

// Toons you never harvest (dead alts, parked mules) — hidden here ONLY, never
// touched on the roster. They drop out of the table, the status counts and every
// bulk picker, so "select all → arm" means the toons he actually cares about.
// Client-side on purpose: it's a per-view preference, not a fact about the toon.
const HV_HIDDEN_KEY = "eqmc-hv-hidden";
let hvHidden = new Set();
try { hvHidden = new Set(JSON.parse(localStorage.getItem(HV_HIDDEN_KEY) || "[]")); }
catch (e) { hvHidden = new Set(); }
let hvShowHidden = false;

function hvSaveHidden() {
  localStorage.setItem(HV_HIDDEN_KEY, JSON.stringify([...hvHidden]));
}

const HV_ORDER = ["missing", "truncated", "failed", "unreadable", "hoard-missed",
                  "stale", "in-progress", "ok"];

function hvAge(h) {
  if (h == null) return "—";
  return h < 48 ? h + "h" : Math.floor(h / 24) + "d";
}

async function loadHarvest() {
  $("hvStatus").textContent = "Scanning dumps…";
  hvData = await api("GET", "/harvest?stale_h=" + encodeURIComponent($("hvStale").value));
  $("hvStatus").textContent = "";
  renderHarvest();
}

function renderHarvest() {
  if (!hvData) return;
  const d = hvData;

  // run banner — a run in progress is mid-write by design, so absence is normal
  const run = d.run || {};
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "—");
  let banner;
  if (run.running) {
    // "armed and not finished" — the queue file existing IS the arming, so this also
    // covers "armed but the client has not been camped yet"
    const live = (run.accounts || []).filter((a) => a.state === "running")
      .map((a) => esc(a.account) + " → " +
        (a.current ? "<b>" + esc(a.current) + "</b>" : "waiting at char select") +
        " <span class='hint'>(" + (a.queue || []).length + " left, " +
        (a.done || []).length + " done" +
        ((a.errors || []).length ? ", " + a.errors.length + " failed" : "") + ")</span>")
      .join(" · ");
    banner = "<b class='hv-live'>● Harvest ARMED</b> — " + live +
             " <span class='hint'>(refresh to follow)</span>";
  } else if (run.updated) {
    banner = "Last harvest run finished " + esc(when(run.updated)) + " · " +
             (run.accounts || []).length + " client(s) reported.";
  } else {
    banner = "<span class='hint'>No harvest run has reported yet — this report is reading " +
             "the dumps on disk only, so a toon can look 'stale' rather than 'failed'.</span>";
  }
  if ((run.errors || []).length) {
    banner += "<br><span class='hv-warn'>Unreadable run files: " +
              run.errors.map(esc).join("; ") + "</span>";
  }
  $("hvRun").innerHTML = banner;

  // Hidden toons are excluded from EVERY number on this page, not just the table —
  // a count that still nags about a mule you told it to ignore is worse than no count.
  const active = d.rows.filter((r) => !hvHidden.has(r.character_id));

  // clickable status chips
  const counts = {};
  for (const r of active) counts[r.status] = (counts[r.status] || 0) + 1;
  const chips = [
    "<button class='hv-chip" + (hvFilter === "" ? " on" : "") + "' data-hv=''>All <b>" +
      active.length + "</b></button>",
  ];
  for (const st of HV_ORDER) {
    if (!counts[st]) continue;
    chips.push("<button class='hv-chip hv-" + st + (hvFilter === st ? " on" : "") +
      "' data-hv='" + st + "' title='" + esc((d.status_help || {})[st] || "") + "'>" +
      st + " <b>" + counts[st] + "</b></button>");
  }
  if (hvHidden.size) {
    chips.push("<span class='hv-hidenote' title='Not counted anywhere on this page'>" +
      "🚫 " + hvHidden.size + " hidden</span>");
  }
  const byAcct = new Map();
  for (const r of active) {
    const k = r.account_alias || "—";
    const a = byAcct.get(k) || { account: k, total: 0, ok: 0 };
    a.total += 1;
    if (r.status === "ok") a.ok += 1;
    byAcct.set(k, a);
  }
  chips.push("<span class='hv-acctroll'>" +
    [...byAcct.values()].sort((a, b) => a.account.localeCompare(b.account)).map((a) =>
      "<span title='" + esc(a.account) + "'>" + esc(a.account) + " " +
      "<b class='" + (a.ok === a.total ? "hv-full" : "hv-part") + "'>" +
      a.ok + "/" + a.total + "</b></span>").join(" · ") + "</span>");
  $("hvSummary").innerHTML = chips.join(" ");
  $("hvSummary").querySelectorAll(".hv-chip").forEach((b) => {
    b.addEventListener("click", () => { hvFilter = b.dataset.hv; renderHarvest(); });
  });

  const rows = (hvShowHidden ? d.rows : active)
    .filter((r) => !hvFilter || r.status === hvFilter);
  const tbody = $("hvRows");
  tbody.innerHTML = "";
  for (const r of rows) {
    const it = r.items || {};
    const off = hvHidden.has(r.character_id);
    const tr = document.createElement("tr");
    if (off) tr.className = "hv-hiddenrow";
    const cell = (v, warn) => "<td class='" + (warn ? "hv-zero" : "") + "'>" + (v || 0) + "</td>";
    tr.innerHTML =
      "<td><input type='checkbox' class='hv-pick' data-cid='" + r.character_id + "'" +
        (off ? " disabled" : "") +
        (hvPicked.has(r.character_id) ? " checked" : "") + "></td>" +
      "<td style='border-left:3px solid " + acctColor(r.account_id) + "'><b>" + esc(r.name) + "</b> " +
        clsSpan(r.class_name, r.class_name || "?") + " " + (r.level || "?") + "</td>" +
      "<td>" + esc(r.account_alias || "—") + "</td>" +
      "<td><span class='hv-pill hv-" + r.status + "'>" + r.status + "</span></td>" +
      cell(it.worn) + cell(it.bags) +
      // 0 bank items is normal (an empty bank still lists its 24 slots); the
      // truncation check lives in the status, never in these counts.
      cell(it.bank) + cell(it.shared) +
      "<td class='" + (r.wants_hoard && !it.hoard ? "hv-zero" : "") + "'>" +
        (it.hoard || 0) + (r.wants_hoard ? " <span title='tagged hoard'>🏦</span>" : "") + "</td>" +
      "<td>" + hvAge(r.dump_age_h) + "</td>" +
      "<td class='hv-note'>" + esc(off ? "hidden — not counted, not armed" : (r.note || "")) + "</td>" +
      "<td><button class='hv-hidebtn' data-hide='" + r.character_id + "' title='" +
        (off ? "Bring this toon back into the report" : "Hide this toon from the report") +
        "'>" + (off ? "↩" : "🚫") + "</button></td>";
    tbody.appendChild(tr);
  }

  tbody.querySelectorAll(".hv-pick").forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = Number(cb.dataset.cid);
      if (cb.checked) hvPicked.add(id); else hvPicked.delete(id);
      hvSyncPicks();
    });
  });
  tbody.querySelectorAll(".hv-hidebtn").forEach((b) => {
    b.addEventListener("click", () => hvSetHidden([Number(b.dataset.hide)],
                                                 !hvHidden.has(Number(b.dataset.hide))));
  });
  hvSyncPicks();

  $("hvOrphans").innerHTML =
    "Reading dumps from <code>" + esc(d.eq_dir || "") + "</code>. " +
    ((d.orphan_dumps || []).length
      ? "<span class='hv-warn'>" + d.orphan_dumps.length + " dump file(s) with no roster row: " +
        d.orphan_dumps.map(esc).join(", ") + " — a rename, or never imported.</span>"
      : "Every dump on disk matches a roster character.");
}

function hvSyncPicks() {
  $("hvPickCount").textContent = hvPicked.size;
  $("hvArmBtn").disabled = hvPicked.size === 0;
  $("hvHiddenCount").textContent = hvHidden.size;
  $("hvShowHidden").classList.toggle("on", hvShowHidden);
  $("hvShowHidden").disabled = hvHidden.size === 0 && !hvShowHidden;
  $("hvHidePicked").disabled = hvPicked.size === 0;
}

// Hiding always drops the pick too — a hidden toon must never ride along into an
// armed run just because it was ticked a moment earlier.
function hvSetHidden(ids, hide) {
  for (const id of ids) {
    if (hide) { hvHidden.add(id); hvPicked.delete(id); }
    else hvHidden.delete(id);
  }
  hvSaveHidden();
  if (!hvHidden.size) hvShowHidden = false;
  renderHarvest();
}

// bulk pickers operate on the rows currently SHOWN, so a status filter scopes them
function hvSelect(pred) {
  if (!hvData) return;
  for (const r of hvData.rows) {
    if (hvHidden.has(r.character_id)) continue;
    if (hvFilter && r.status !== hvFilter) continue;
    if (pred(r)) hvPicked.add(r.character_id);
  }
  renderHarvest();
}

$("hvRefreshBtn").addEventListener("click", () => guard(loadHarvest));
$("hvStale").addEventListener("change", () => guard(loadHarvest));
$("hvPickProblems").addEventListener("click", () => hvSelect((r) => r.status !== "ok"));
$("hvPickHoard").addEventListener("click", () =>
  hvSelect((r) => r.wants_hoard || (r.items && r.items.hoard > 0)));
$("hvPickNone").addEventListener("click", () => { hvPicked.clear(); renderHarvest(); });
$("hvHidePicked").addEventListener("click", () => hvSetHidden([...hvPicked], true));
$("hvShowHidden").addEventListener("click", () => {
  hvShowHidden = !hvShowHidden;
  renderHarvest();
});
$("hvPickAll").addEventListener("change", (e) => {
  if (e.target.checked) hvSelect(() => true);
  else { hvPicked.clear(); renderHarvest(); }
});

$("hvArmBtn").addEventListener("click", () => guard(async () => {
  const d = await api("POST", "/harvest/queue", { char_ids: [...hvPicked] });
  const per = d.queues.map((q) => q.account + " (" + q.count +
    (q.hoard.length ? ", " + q.hoard.length + " hoard" : "") + ")").join(" · ");
  await loadHarvest();          // refresh FIRST — loadHarvest clears #hvStatus
  $("hvStatus").innerHTML =
    "<b class='hv-live'>⚡ Armed " + d.total + " toon(s) across " + d.queues.length +
    " account(s):</b> " + esc(per) +
    "<br>Queue files written to <code>" + esc(d.mq_config_dir) + "</code>. " +
    "Now camp each of those clients to character select — the rotation takes over from there. " +
    "Deleting the queue files (⛔ Disarm) stops it." +
    (d.no_account.length
      ? "<br><span class='hv-warn'>Skipped (no account on the roster, can't /switchchar): " +
        d.no_account.map(esc).join(", ") + "</span>"
      : "");
  toast("Armed " + d.total + " toon(s)");
}));

$("hvDisarmBtn").addEventListener("click", () => guard(async () => {
  const d = await api("POST", "/harvest/disarm", {});
  await loadHarvest();          // refresh FIRST — loadHarvest clears #hvStatus
  $("hvStatus").innerHTML = d.disarmed.length
    ? "⛔ Disarmed: " + d.disarmed.map(esc).join(", ") + ". Any client mid-step stops after this toon."
    : "<span class='hint'>Nothing was armed.</span>";
}));

async function loadGearSummary() {
  $("gearStatus").textContent = "Crunching dumps + item database (first load parses ~100k items)…";
  const d = await api("GET", "/gear/summary");
  $("gearStatus").textContent = "";
  const cols = d.stats_meta;
  $("gearHead").innerHTML = "<tr><th>Toon</th><th>Class</th><th>Dump</th>" +
    cols.map((c) => "<th>" + esc(c.label) + "</th>").join("") +
    "<th>Haste</th><th>Empty</th></tr>";
  const tbody = $("gearRows");
  tbody.innerHTML = "";
  d.rows.sort((a, b) => (b.level || 0) - (a.level || 0) || a.name.localeCompare(b.name));
  for (const r of d.rows) {
    const tr = document.createElement("tr");
    const age = r.dump_age_h < 24 ? r.dump_age_h + "h" : Math.floor(r.dump_age_h / 24) + "d";
    tr.innerHTML =
      "<td style='border-left:3px solid " + acctColor(r.account_id) + "'><b>" + esc(r.name) + "</b></td>" +
      "<td>" + clsSpan(r.class_name, r.class_name || "?") + " " + (r.level || "?") + "</td>" +
      "<td class='" + (r.dump_age_h > 72 ? "dumpold" : "") + "' title='Dump age — refresh in game with /outputfile inventory'>" + age + "</td>" +
      cols.map((c) => "<td>" + (r.totals[c.key] || 0) + "</td>").join("") +
      "<td class='hastecell' title='" + esc(r.haste_item || "no haste item worn") + "'>" +
      (r.haste ? r.haste + "%" : "—") + "</td>" +
      "<td class='hint' title='Worn slot types with nothing in them: " + esc(r.empty_slots.join(", ") || "none") + "'>" +
      r.empty_slots.length + "</td>";
    tbody.appendChild(tr);
  }
  $("gearMissing").textContent = d.no_dump.length
    ? "No inventory dump yet (" + d.no_dump.length + "): " + d.no_dump.slice(0, 12).join(", ") +
      (d.no_dump.length > 12 ? "…" : "") + " — run /outputfile inventory on them."
    : "";
  if (!$("bestStat").options.length) {
    const bs = await api("GET", "/gear/best?stat=haste");
    $("bestStat").innerHTML = bs.stats_meta.map((s) =>
      "<option value='" + s.key + "'" + (s.key === "haste" ? " selected" : "") + ">" +
      esc(s.label) + "</option>").join("");
    $("bestClass").innerHTML = "<option value=''>Any class</option>" +
      S.meta.classes.map((c) => "<option>" + c + "</option>").join("");
    renderBestRows(bs.rows);
  }
}

function renderBestRows(rows) {
  $("bestRows").innerHTML = rows.length ? rows.map((r, i) =>
    "<tr><td>" + (i + 1) + "</td><td><b>" + esc(r.item) + "</b>" +
    (r.fvnodrop ? " <span class='st-warn' title='FV NO DROP — cannot be traded on Frostreaver'>⚠</span>" : "") +
    "</td><td><b>" + r.value + "</b></td><td>" + esc(r.holder) + "</td>" +
    "<td>" + esc(r.where) + "</td><td class='hint'>" + esc(r.usable) + "</td></tr>").join("")
    : "<tr><td colspan='6' class='hint'>Nothing found with that stat.</td></tr>";
}

$("gearRefreshBtn").addEventListener("click", () => guard(async () => {
  gearCache = null;                          // comp power panel re-fetches too
  await loadGearSummary();
}));
$("bestGoBtn").addEventListener("click", () => guard(async () => {
  const q = "/gear/best?stat=" + encodeURIComponent($("bestStat").value) +
    ($("bestClass").value ? "&cls=" + encodeURIComponent($("bestClass").value) : "");
  renderBestRows((await api("GET", q)).rows);
}));

// ---------- gear sets (planner moved here from the Macro Builder) ----------
let gsLoaded = false;
let gsSets = [];
let gsFocusId = null;                 // set row to highlight after a comp-builder jump
// Comp focus: while set, the sets table, the Move Plan and the MQ export are all
// scoped to these six. The PLANNER still routes every active set on the roster —
// only the report is narrowed — or a comp would read clean while taking a piece
// another set is already promised.
let gsComp = null;                    // { name, ids: [char_id], showAll: bool }

// The best set describing a toon: active first, then most recently touched.
function bestSetFor(cid) {
  const mine = gsSets.filter((s) => s.assigned_char_id === cid);
  mine.sort((a, b) => (b.active ? 1 : 0) - (a.active ? 1 : 0) ||
                      (b.updated_at || 0) - (a.updated_at || 0));
  return mine[0] || null;
}

// Small gear-set status line on a comp-builder slot card.
function slotGearHtml(c) {
  const set = bestSetFor(c.id);
  if (!set) {
    return "<div class='slot-gear none' data-cid='" + c.id +
      "' title='No gear set assigned to " + esc(c.name) +
      " — click to snapshot what they are wearing now and open Gear Sets'>🎁 no set — 📸 snapshot</div>";
  }
  const fit = set.fit || {};
  // Count worn against worn_total, not total: virtual slots (the Avatar weapon) live in
  // the bags on purpose, so a complete set would otherwise never read as full.
  const wTot = fit.worn_total != null ? fit.worn_total : fit.total;
  const fitTxt = fit.total == null ? set.items.length + " pc"
    : fit.worn == null ? fit.total + " pc · no dump"
    : fit.worn + "/" + wTot + " worn";
  const full = fit.total != null && fit.worn === wTot;
  return "<div class='slot-gear" + (set.active ? "" : " retired") + (full ? " full" : "") +
    "' data-cid='" + c.id + "' title='Gear set: " + esc(set.name) +
    (set.active ? "" : " (retired)") + " — click to view in Gear Sets'>🎁 " +
    esc(set.name) + " · " + fitTxt + "</div>";
}

// Comp builder -> this toon's gear. No set yet? Snapshot their worn gear first.
async function gearJump(cid) {
  const c = charById(cid);
  if (!bestSetFor(cid)) {
    const res = await api("POST", "/gearsets/snapshot", { char_id: cid });
    toast("No gear set for " + c.name + " — snapshotted their worn gear as '" +
      res.name + "' (" + res.pieces + " pieces).");
  }
  gsLoaded = true;
  await loadGearSets();
  const set = bestSetFor(cid) || gsSets.find((s) => s.source_char_id === cid);
  gsFocusId = set ? set.id : null;
  gotoTab("sets");
  renderGearSets();
  const row = document.querySelector("#gsRows tr.gs-focus");
  if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------- comp gear check (the whole six, not one set at a time) ----------
// "Can I field this comp right now?" Built from the same routed plan the Move Plan
// renders, so the roll-up and the detail can never disagree.

const GS_STATE = {
  ready:   { txt: "✔ ready",     cls: "gs-ok",     tip: "Every piece of their set is on their body" },
  onhand:  { txt: "🎒 to equip",  cls: "gs-free",   tip: "They already hold everything missing — log in and put it on" },
  moves:   { txt: "🚚 inbound",   cls: "gs-swap",   tip: "Pieces still have to be moved to them — see the Move Plan below" },
  blocked: { txt: "⛔ blocked",   cls: "gs-bad",    tip: "At least one piece can't be delivered at all" },
  noset:   { txt: "no set",      cls: "gs-warn",   tip: "No active gear set assigned — nothing to check against" },
  retired: { txt: "set retired", cls: "gs-warn",   tip: "They have a set, it just isn't active — a retired set claims no items and isn't planned" },
};

const compLiveIds = (slots) => (slots || []).slice(0, 6).filter(Boolean);

function renderCompPicker() {
  const sel = $("gsCompPick");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = "<option value=''>— pick a saved comp —</option>" +
    S.compositions.map((c) => "<option value='" + c.id + "'>" + esc(c.name) +
      " (" + compLiveIds(c.slots).length + ")</option>").join("");
  if (cur) sel.value = cur;
}

// Focus the Gear Sets tab on one comp and re-run everything scoped to it.
async function runCompGear(ids, name, compId) {
  ids = ids.filter(Boolean);
  if (!ids.length) throw new Error("That comp has no live members yet.");
  $("gsCompOut").innerHTML = "<div class='hint'>Routing every active set, then reporting on " +
    esc(name) + "…</div>";
  gsComp = { name: name, ids: ids, id: compId || null,
             showAll: gsComp ? gsComp.showAll : false };
  if (!gsLoaded) { gsLoaded = true; await loadGearSets(); }
  // WHICH SETS this comp fields — not "sets pinned to a member". Pinning answers the
  // wrong question in both directions: Trixrez is a member so his PL set showed up in
  // a raid comp, and Rogue Main is pinned to Zyrak so it was hidden from Gavriel, the
  // very toon who needs it. Loadouts are roles; the comp's mapping is the authority.
  gsComp.setIds = null;
  gsComp.unmapped = [];
  if (compId) {
    try {
      const mp = (await api("GET", "/gearsets/compmap/" + compId)).mapping || [];
      const live = mp.filter((m) => !m.bench);
      gsComp.setIds = live.map((m) => m.gear_set_id).filter(Boolean);
      gsComp.unmapped = live.filter((m) => !m.gear_set_id || m.needs_choice)
        .map((m) => m.character);
    } catch (e) { /* unsaved comp: fall back to the pin-based scope below */ }
  }
  const d = await api("POST", "/gearsets/compplan",
    { slots: ids, login: loginSlots(), stale_h: Number($("gsStale").value) });
  gsComp.data = d;
  renderCompReadiness(d);
  applyCompScope();
  renderGearSets();
  renderPlanResult(d.plan);
}

function clearCompFocus() {
  gsComp = null;
  $("gsCompOut").innerHTML = "<div class='hint'>Focus cleared — the sets table and Move Plan are back to the whole roster.</div>";
  applyCompScope();
  renderGearSets();
  $("gsPlan").innerHTML = "<div class='hint'>Hit Plan transfers for the whole-roster plan.</div>";
}

// Everything that changes when a comp is (or isn't) in focus.
function applyCompScope() {
  $("gsCompClearBtn").hidden = !gsComp;
  $("gsPlanScope").innerHTML = gsComp
    ? "scoped to <b>" + esc(gsComp.name) + "</b> — every active set is still routed (nothing gets double-promised), you just see this six's moves"
    : "every ACTIVE set, routed by real cost — your shared bank beats a swap beats a parcel · uses the Current Login Set for trade-now routing";
}

function renderCompReadiness(d) {
  const sum = d.summary || {}, st = sum.states || {};
  const chip = (n, cls, txt, tip) => n
    ? "<span class='gs-chip " + cls + "'" + (tip ? " title='" + esc(tip) + "'" : "") + ">" + n + " " + txt + "</span>" : "";
  const chips = "<div class='gs-sum'>" +
    "<span class='gs-chip'>" + esc(gsComp.name) + " · " + (sum.members || 0) + " live</span>" +
    chip(st.ready, "gs-ok", "ready") +
    chip(st.onhand, "gs-free", "to equip", "They hold the pieces already — just log in and wear them") +
    chip(st.moves, "gs-swap", "waiting on moves") +
    chip(st.blocked, "gs-bad", "blocked") +
    chip((d.members || []).filter((m) => m.reason === "retired").length, "gs-warn",
         "set retired", "They have a set — it's switched off, so it claims nothing and isn't planned") +
    chip((d.members || []).filter((m) => m.reason === "none").length, "gs-warn", "no set") +
    "<span class='gs-chip'>" + (sum.logins || 0) + " login(s) to gear it</span>" +
    (sum.ready ? "<span class='gs-chip gs-ok'>✔ everyone is wearing their set</span>" : "") +
    "</div>";

  const rows = (d.members || []).map((m) => {
    // A member with a RETIRED set isn't the same problem as one with no set —
    // the fix is a tick box, not a snapshot (which would just build a duplicate).
    const b = GS_STATE[m.reason === "retired" ? "retired" : m.state] || GS_STATE.noset;
    const inc = Object.keys(m.incoming || {})
      .map((k) => (m.incoming[k]) + " " + (GS_BADGE[k] ? GS_BADGE[k].txt.replace(/^\S+\s/, "") : k))
      .join(" · ");
    const gaps = (m.gaps || []).length
      ? "<span class='gs-route gs-warn' title='The set has no pick for: " + esc(m.gaps.join(", ")) +
        " — open the editor and fill them, or leave them if that's deliberate'>" +
        m.gaps.length + " slot(s) unset</span>" : "";
    const blocked = (m.blocked || []).map((x) =>
      "<div class='hint'>" + gsBadge(x.status, x.note) + " " + esc(x.item) +
      " <span class='hint'>(" + esc(x.slot) + ")</span></div>").join("");
    const dump = m.dumped
      ? (m.dump_age_h != null && m.dump_age_h > 72
          ? "<span class='st-warn' title='Everything here is judged against a dump this old'>⏳ " +
            Math.floor(m.dump_age_h / 24) + "d</span>" : "")
      : "<span class='st-warn' title='No inventory dump — we cannot see what they are wearing'>no dump</span>";
    // "use for this comp" records the choice on the comp slot; it does NOT flip the
    // set on by itself. A lone activate is what let two comps' sets be live at once.
    const shelved = (m.shelved_sets || []).filter((s) => s.pieces).map((s) =>
      "<div><button class='btn btn-ghost btn-sm gsc-act' data-sid='" + s.id +
      "' data-cid='" + m.char_id + "'" +
      (gsComp && gsComp.id ? "" : " disabled title='Save the comp first'") +
      " title='Field this loadout for " + esc(m.name) + " in this comp. Press " +
      "&quot;Apply this comp&#39;s gear sets&quot; to switch the whole six over.'>" +
      "🎯 use for this comp</button> " +
      esc(s.name) + " <span class='hint'>" + s.pieces + " pc</span></div>").join("");
    const setCell = m.set_id
      ? "<b>" + esc(m.set_name) + "</b> <span class='hint'>" + m.pieces + " pc</span> " + gaps
      : (shelved || "") +
        "<button class='btn btn-ghost btn-sm gsc-snap' data-cid='" + m.char_id +
        "' title='Snapshot what they are wearing right now as their set'>📸 snapshot worn</button>";
    return "<tr class='gsc-" + esc(m.state) + "'>" +
      "<td><b>" + esc(m.name) + "</b> " + clsSpan(m.class_name, (m.class_name || "?").slice(0, 3)) +
      "<div class='hint'>" + esc(m.account_alias || "no account") + " " + dump + "</div></td>" +
      "<td><span class='gs-route " + b.cls + "' title='" + esc(b.tip) + "'>" + b.txt + "</span></td>" +
      "<td>" + setCell + "</td>" +
      "<td>" + (m.set_id ? m.equipped + "/" + m.pieces + " worn" +
        (m.on_hand ? " · <span class='gs-route gs-free'>" + m.on_hand + " on hand</span>" : "") : "—") + "</td>" +
      "<td>" + (inc || "—") + (m.stale_moves
        ? " <span class='st-warn' title='Routed from a dump older than the trust window'>⏳" + m.stale_moves + "</span>" : "") + "</td>" +
      "<td>" + (blocked || "<span class='hint'>—</span>") + "</td>" +
      "<td>" + (m.set_id
        ? "<button class='btn btn-ghost btn-sm gsc-edit' data-sid='" + m.set_id + "'>✎ edit set</button>" : "") + "</td></tr>";
  }).join("");

  // Copy shortages: the same fungible-copy math the builder warns with. Repeated
  // here because "everyone has a set" and "there are enough copies to fill them
  // all at once" are different questions.
  const c = d.check || {};
  const warns = []
    .concat((c.overlaps || []).map((o) =>
      "<div class='warn error'>🎁 <b>" + esc(o.item) + "</b> — " + o.need +
      " set(s) in this comp need it (" + esc(o.sets.join(", ")) + ") but you own " + o.owned +
      (o.owned === 0 ? " (nobody has one)" : "") + ". Someone will go without.</div>"))
    .concat((c.outside || []).map((o) =>
      "<div class='warn warn'>🎁 Tight: <b>" + esc(o.item) + "</b> — this comp needs " +
      o.comp_need + " and <i>" + esc(o.outside_sets.join(", ")) + "</i> (outside this comp) want(s) " +
      o.outside_need + " more, but you own " + o.owned + ". Fine to play — just don't gear both at once.</div>"));

  $("gsCompOut").innerHTML = chips + warns.join("") +
    "<div class='tablewrap'><table class='mc-table gsc-table'><thead><tr>" +
    "<th>Toon</th><th>State</th><th>Set</th><th>On them</th>" +
    "<th title='Pieces still to be moved to them, by route'>Inbound</th>" +
    "<th title='Pieces nobody can deliver — every copy is claimed, No Trade, LORE, or missing'>Blocked</th>" +
    "<th></th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
    "<div class='hint'>Routes and detail are in the Move Plan below — already scoped to this comp.</div>";

  $("gsCompOut").querySelectorAll(".gsc-snap").forEach((btn) => {
    btn.addEventListener("click", () => guard(async () => {
      const cid = parseInt(btn.dataset.cid, 10);
      const res = await api("POST", "/gearsets/snapshot", { char_id: cid });
      toast("Snapshotted '" + res.name + "' — " + res.pieces + " worn piece(s).");
      await loadGearSets();
      await runCompGear(gsComp.ids, gsComp.name, gsComp.id);
    }));
  });
  $("gsCompOut").querySelectorAll(".gsc-act").forEach((btn) => {
    btn.addEventListener("click", () => guard(async () => {
      if (!gsComp || !gsComp.id) throw new Error("Save this composition first.");
      await api("POST", "/gearsets/compchoice", {
        comp_id: gsComp.id, character_id: parseInt(btn.dataset.cid, 10),
        gear_set_id: parseInt(btn.dataset.sid, 10) });
      toast("Set recorded for this comp — press 🎯 Apply this comp's gear sets to switch over.");
      await loadGearSets();
      await runCompGear(gsComp.ids, gsComp.name, gsComp.id);
    }));
  });
  $("gsCompOut").querySelectorAll(".gsc-edit").forEach((btn) => {
    btn.addEventListener("click", () => guard(() => {
      const s = gsSets.find((x) => x.id === parseInt(btn.dataset.sid, 10));
      if (s) openSetEditor(s);
    }));
  });
}

$("gsCompRunBtn").addEventListener("click", () => guard(async () => {
  const id = parseInt($("gsCompPick").value, 10);
  if (!id) throw new Error("Pick a saved comp first — or use 🎁 Check this comp's gear in the Comp Builder.");
  const c = S.compositions.find((x) => x.id === id);
  await runCompGear(compLiveIds(c.slots), c.name, c.id);
}));
$("gsCompClearBtn").addEventListener("click", () => guard(clearCompFocus));
$("gsCompPick").addEventListener("change", () => {
  if ($("gsCompPick").value) guard(() => $("gsCompRunBtn").click());
});

// Comp Builder -> here, carrying the six currently on screen (saved or not).
$("compGearBtn").addEventListener("click", () => guard(async () => {
  const ids = compLiveIds(comp.slots);
  if (!ids.length) throw new Error("Fill some slots first.");
  gotoTab("sets");
  $("gsCompPick").value = comp.id || "";
  await runCompGear(ids, $("compName").value.trim() || "this comp", comp.id);
  $("gsCompCard").scrollIntoView({ behavior: "smooth", block: "start" });
}));

const GS_BADGE = {
  worn:    { txt: "✓ worn",        cls: "gs-ok",    tip: "Already equipped on the target" },
  have:    { txt: "🎒 equip",      cls: "gs-ok",    tip: "Already on the target — just equip it" },
  grab:    { txt: "🏦 shared bank", cls: "gs-free",  tip: "In the target's OWN shared bank — withdraw when you log in. Zero extra logins." },
  swap:    { txt: "🔁 swap",       cls: "gs-swap",  tip: "Holder is on the SAME account — drop it in the shared bank before you swap to the target. No parcel NPC needed." },
  trade:   { txt: "🤝 trade now",  cls: "gs-trade", tip: "Holder is in your Current Login Set — trade it over right now, no extra login." },
  parcel:  { txt: "📦 parcel",     cls: "gs-parcel", tip: "Different account, offline — log the holder once and parcel it" },
  reserved:{ txt: "⚑ reserved",    cls: "gs-warn",  tip: "Every copy is promised to another active set" },
  notrade: { txt: "⛔ no trade",   cls: "gs-bad",   tip: "FV NO DROP — cannot be moved on Frostreaver" },
  lore:    { txt: "LORE",          cls: "gs-bad",   tip: "LORE item — a character can only carry one" },
  covered: { txt: "✓ covered",     cls: "gs-ok",    tip: "Another slot in this set already carries a weapon with this proc — the slot is redundant and can be cleared" },
  // shortfall is NOT missing: you own the item, this set just asked for more copies
  // of it than exist — usually the same piece picked into two slots it legitimately
  // qualifies for (a 2H that also procs Avatar, say). Different fix, different badge.
  shortfall:{ txt: "✗ short",      cls: "gs-bad",   tip: "You own it, but this set claims it in more slots than you have copies" },
  missing: { txt: "✗ missing",     cls: "gs-bad",   tip: "Nobody on the roster has one" },
};
const gsBadge = (st, note) => {
  const b = GS_BADGE[st] || { txt: st, cls: "", tip: "" };
  return "<span class='gs-route " + b.cls + "' title='" + esc(note || b.tip) + "'>" + b.txt + "</span>";
};

function gsCharOptions(set) {
  const chars = S.characters.filter((c) => c.status !== "retired").slice().sort((a, b) => {
    const am = a.class_name === set.class_name ? 0 : 1;
    const bm = b.class_name === set.class_name ? 0 : 1;
    return am - bm || (b.level || 0) - (a.level || 0) || a.name.localeCompare(b.name);
  });
  return chars.map((c) =>
    "<option value='" + c.id + "'" + (c.id === set.assigned_char_id ? " selected" : "") + ">" +
    esc(c.name) + " — " + esc(c.class_name || "?") + " " + (c.level || "?") +
    " · " + esc(charAcctLabel(c)) + "</option>").join("");
}

async function loadGearSets() {
  const d = await api("GET", "/gearsets");
  gsSets = d.sets;
  // snapshot dropdown: every non-retired toon (the server complains if it has no dump)
  const snap = $("gsSnapChar");
  const cur = snap.value;
  snap.innerHTML = S.characters.filter((c) => c.status !== "retired")
    .slice().sort((a, b) => (b.level || 0) - (a.level || 0))
    .map((c) => "<option value='" + c.id + "'>" + esc(c.name) + " — " +
      esc(c.class_name || "?") + " " + (c.level || "?") + "</option>").join("");
  if (cur) snap.value = cur;
  renderGearSets();
  renderSlots();          // comp-builder gear lines show the fresh sets/fit
}

function renderGearSets() {
  const tbody = $("gsRows");
  tbody.innerHTML = "";
  if (!gsSets.length) {
    tbody.innerHTML = "<tr><td colspan='8' class='hint'>No gear sets yet — snapshot a toon's worn gear above, or import your old Macro Builder sets.</td></tr>";
    $("gsStatus").innerHTML = "";
    return;
  }
  // With a comp in focus the table shows only that six's sets — the other sets
  // still exist and are still routed, they're just not what you're looking at.
  // Prefer the comp's own mapping (the loadouts it fields). Only fall back to the
  // old "pinned to a member" rule for a comp that was never saved, which has no
  // mapping to read.
  // In focus the table shows the loadouts this comp actually fields — its mapping,
  // which is the only record of "belongs to this comp". Nothing is hidden by a label;
  // "show every set" is one click away.
  const byMap = gsComp && !gsComp.showAll && gsComp.setIds;
  const mapped = new Set(byMap ? gsComp.setIds : []);
  const focus = gsComp && !gsComp.showAll
    ? new Set(byMap ? gsComp.setIds : gsComp.ids) : null;
  const shown = !focus ? gsSets
    : byMap ? gsSets.filter((s) => mapped.has(s.id))
            : gsSets.filter((s) => focus.has(s.assigned_char_id || s.source_char_id));
  const unmapped = (gsComp && gsComp.unmapped) || [];
  $("gsStatus").innerHTML = focus
    ? "Showing <b>" + shown.length + "</b> of " + gsSets.length + " sets — the loadouts <b>" +
      esc(gsComp.name) + "</b> fields." +
      (unmapped.length ? " <span class='gs-warn'>No loadout chosen yet for " +
        esc(unmapped.join(", ")) + " — pick one in the Comp Builder, or make a set and "
        + "assign it there.</span>" : "") +
      " <a href='#' id='gsShowAll'>show every set</a>"
    : (gsComp ? "Showing every set. <a href='#' id='gsShowFocus'>back to " +
        esc(gsComp.name) + " only</a>" : "");
  const toggle = $("gsShowAll") || $("gsShowFocus");
  if (toggle) toggle.addEventListener("click", (e) => {
    e.preventDefault();
    gsComp.showAll = !gsComp.showAll;
    renderGearSets();
  });
  if (!shown.length) {
    tbody.innerHTML = "<tr><td colspan='8' class='hint'>Nobody in this comp has a gear set yet — snapshot them from the panel above.</td></tr>";
    return;
  }
  for (const s of shown) {
    const fit = s.fit || {};
    const fitCell = fit.total == null ? "—"
      : fit.worn == null
        ? "<span class='st-dim' title='The assigned toon has no inventory dump yet'>no dump</span>"
        : "<b class='" + (fit.worn === (fit.worn_total != null ? fit.worn_total : fit.total)
            ? "st-ready" : "") + "'>" + fit.worn + "</b> worn · " +
          fit.present + "/" + fit.total + " on hand";
    const tr = document.createElement("tr");
    tr.className = (!s.active ? "gs-retired" : "") + (s.id === gsFocusId ? " gs-focus" : "");
    tr.innerHTML =
      "<td><b>" + esc(s.name) + "</b>" +
        (s.notes ? "<div class='hint'>" + esc(s.notes) + "</div>" : "") + "</td>" +
      "<td>" + clsSpan(s.class_name, s.class_name || "—") + "</td>" +
      "<td class='hint'>" + esc(s.source_name || "—") + "</td>" +
      "<td><select class='gs-assign'>" + gsCharOptions(s) + "</select></td>" +
      "<td>" + s.items.length + "</td>" +
      "<td>" + fitCell + "</td>" +
      "<td><input type='checkbox' class='gs-active' " + (s.active ? "checked" : "") +
      " title='Retired sets keep their contents but release every item claim'></td>" +
      "<td><button class='btn btn-ghost btn-sm gs-ren' type='button' title='Edit — rename, reassign, or hand-pick every slot from everything you own'>✎ edit</button>" +
      // Duplicate is for a VARIANT, not for sharing — sharing is what the tag does.
      "<button class='btn btn-ghost btn-sm gs-clone' type='button' title='Duplicate this loadout as a separate set — for a variant that will diverge. To field the SAME loadout in another comp, give both comps the same gear family instead.'>⧉ duplicate</button>" +
      "<button class='raid-del gs-del' type='button' title='Delete set'>✕</button></td>";
    tr.querySelector(".gs-assign").addEventListener("change", (e) => guard(async () => {
      await api("PUT", "/gearsets/" + s.id, { assigned_char_id: parseInt(e.target.value, 10) });
      await loadGearSets();
      $("gsPlan").innerHTML = "<div class='hint'>Assignment changed — hit Plan transfers again.</div>";
    }));
    tr.querySelector(".gs-active").addEventListener("change", (e) => guard(async () => {
      await api("PUT", "/gearsets/" + s.id, { active: e.target.checked });
      await loadGearSets();
    }));
    tr.querySelector(".gs-ren").addEventListener("click", () => guard(() => openSetEditor(s)));
    tr.querySelector(".gs-clone").addEventListener("click", () => guard(async () => {
      const res = await api("POST", "/gearsets/clone", { set_id: s.id });
      toast("Duplicated as '" + res.name + "' — " + res.pieces +
        " piece(s), retired. It is a separate set from now on.");
      await loadGearSets();
    }));
    tr.querySelector(".gs-del").addEventListener("click", () => guard(async () => {
      if (!confirm("Delete gear set '" + s.name + "'? (Items on your toons are untouched — this only deletes the list.)")) return;
      await api("DELETE", "/gearsets/" + s.id);
      await loadGearSets();
    }));
    tbody.appendChild(tr);
  }
}

$("gsSnapBtn").addEventListener("click", () => guard(async () => {
  const cid = parseInt($("gsSnapChar").value, 10);
  if (!cid) throw new Error("Pick a toon to snapshot.");
  const res = await api("POST", "/gearsets/snapshot",
    { char_id: cid, name: $("gsSnapName").value.trim() });
  toast("Saved '" + res.name + "' — " + res.pieces + " worn piece(s)" +
    (res.dump_age_h > 24 ? " (dump is " + Math.floor(res.dump_age_h / 24) + "d old — consider a fresh /outputfile inventory)" : ""));
  $("gsSnapName").value = "";
  await loadGearSets();
}));

// One-time import of the Macro Builder's localStorage gear sets (same origin).
$("gsImportBtn").addEventListener("click", () => guard(async () => {
  let sets, targets, inactive;
  try {
    sets = JSON.parse(localStorage.getItem("eqaf-gear-sets") || "{}");
    targets = JSON.parse(localStorage.getItem("eqaf-gear-targets") || "{}");
    inactive = JSON.parse(localStorage.getItem("eqaf-gear-inactive") || "{}");
  } catch (e) { throw new Error("Could not read the old Gear Planner storage: " + e.message); }
  const names = Object.keys(sets).filter((n) => (sets[n] || []).length);
  if (!names.length) throw new Error("No sets found in the Macro Builder's storage on this browser.");
  const byName = (nm) => S.characters.find((c) => c.name.toLowerCase() === String(nm || "").toLowerCase());
  let done = 0;
  for (const n of names) {
    const tgt = byName(targets[n]);
    await api("POST", "/gearsets", {
      name: n,
      class_name: tgt ? tgt.class_name : "",
      assigned_char_id: tgt ? tgt.id : null,
      active: !inactive[n],
      notes: "imported from Macro Builder",
      items: (sets[n] || []).map((it) => ({ item_id: it.id, item_name: it.name, slot: it.slot || "" })),
    });
    done++;
  }
  toast("Imported " + done + " set(s) from the Macro Builder.");
  await loadGearSets();
}));

// ---------- gear sets: paperdoll set editor ----------
// Every worn slot is a dropdown over EVERYTHING owned across the roster (from
// /gearsets/candidates). Picks live in gsEd.picks keyed "Slot|k" (k = 0/1 for
// paired slots). Unpicked slots simply aren't part of the set.
let gsEd = null;      // { setId, cands: {slot: [items]}, slots: [meta], picks: {} }

function gsEdKey(slot, k) { return slot + "|" + k; }

async function openSetEditor(set) {
  const cls = set ? (set.class_name || "") : "";
  $("gsEdTitle").textContent = set ? "Edit set" : "New custom set";
  $("gsEdName").value = set ? set.name : "";
  $("gsEdClass").innerHTML = "<option value=''>any class</option>" +
    S.meta.classes.map((c) => "<option" + (c === cls ? " selected" : "") + ">" + c + "</option>").join("");
  $("gsEdAssign").innerHTML = "<option value=''>— assign to… —</option>" +
    S.characters.filter((c) => c.status !== "retired")
      .slice().sort((a, b) => (b.level || 0) - (a.level || 0))
      .map((c) => "<option value='" + c.id + "'" +
        (set && c.id === set.assigned_char_id ? " selected" : "") + ">" +
        esc(c.name) + " — " + esc(c.class_name || "?") + " " + (c.level || "?") + "</option>").join("");
  gsEd = { setId: set ? set.id : null, cands: {}, slots: [], picks: {}, sel: null, selSlot: null };
  if (set) {
    const seen = {};
    for (const it of set.items) {
      const k = it.slot_index != null ? it.slot_index : (seen[it.slot] || 0);
      seen[it.slot] = k + 1;
      gsEd.picks[gsEdKey(it.slot, k)] = { item_id: it.item_id, item_name: it.item_name };
    }
  }
  $("gsEditorCard").hidden = false;
  $("gsEdRows").innerHTML = "<tr><td colspan='2' class='hint'>Loading everything you own…</td></tr>";
  $("gsEditorCard").scrollIntoView({ behavior: "smooth", block: "start" });
  await gsEdFetch();
}

async function gsEdFetch() {
  const d = await api("POST", "/gearsets/candidates", {
    class_name: $("gsEdClass").value || null,
    exclude_set_id: gsEd.setId,
    target_char_id: $("gsEdAssign").value ? parseInt($("gsEdAssign").value, 10) : null,
  });
  gsEd.slots = d.slots;
  gsEd.cands = {};
  // "" = no target assigned; race name = filtering; null = target has no race on file
  gsEd.raceFilter = d.race_filter;
  // Name of the toon this set is assigned to — the editor needs it to say whose
  // "wearing now" it is showing.
  gsEd.targetName = $("gsEdAssign").selectedIndex > 0
    ? $("gsEdAssign").options[$("gsEdAssign").selectedIndex].text.split(" — ")[0] : "";
  for (const s of d.slots) gsEd.cands[s.slot] = s.items;
  renderSetEditor();
}

function gsEdWhere(c) {
  if (!c) return "—";
  // Copies are FUNGIBLE (EQ dumps carry no per-copy identity): show every holder
  // so one arbitrary name never reads like "the" copy. "Rakthor worn · Belwyn worn"
  // means two interchangeable copies exist.
  const holders = c.holders || [];
  const holderTxt = holders.slice(0, 3).map((h) =>
    (h.banker ? "🏦 " : "") + esc(h.holder) + " <span class='gs-loc gs-loc-" + esc(h.bucket) + "'>" + esc(h.bucket) +
    (h.count > 1 ? " ×" + h.count : "") + "</span>").join(" · ");
  const fullList = holders.map((h) => (h.banker ? "[banker] " : "") + h.holder + " " + h.bucket +
    (h.count > 1 ? " ×" + h.count : "") + " (" + h.loc + ")").join("\n");
  let html = "";
  if (c.target_has) {
    const tname = $("gsEdAssign").selectedIndex > 0
      ? $("gsEdAssign").options[$("gsEdAssign").selectedIndex].text.split(" — ")[0] : "target";
    const how = c.target_has === "worn" ? "already worn"
      : c.target_has === "held" ? "in their bags/bank" : "in their shared bank";
    html += "<span class='gs-route gs-have' title='The assigned toon already has a copy (" + esc(how) +
      ") — the planner will use THAT, not the sources listed here'>✔ " + esc(tname) + " — " + esc(how) + "</span> ";
  }
  html += "<span title='" + esc(fullList) + "'>" + (holderTxt || esc(c.holder)) +
    (holders.length > 3 ? " <span class='hint'>+" + (holders.length - 3) + " more</span>" : "") + "</span>";
  if (c.owned > 1) html += " <span class='hint'>· own " + c.owned + "</span>";
  if (c.fvnodrop) html += " <span class='st-bad' title='FV NO DROP — cannot be moved between toons'>⛔ No Trade</span>";
  if (c.lore) html += " <span class='hint' title='LORE — one per character'>LORE</span>";
  const wb = c.worn_by || [];
  if (wb.length) {
    html += " <span class='gs-route gs-worn' title='Copies currently WORN by these toons — grabbing one undresses them. " +
      (c.idle || 0) + " idle cop" + (c.idle === 1 ? "y" : "ies") + " (bags/bank/banker) elsewhere.'>⚔ " +
      esc(wb.map((w) => w.holder + (w.level ? " " + w.level : "")).join(", ")) + "</span>";
  }
  if (c.reserved_by.length) {
    html += c.free <= 0
      ? " <span class='gs-warn gs-route' title='Every copy is claimed by: " + esc(c.reserved_by.join(", ")) + "'>⚑ reserved</span>"
      : " <span class='hint' title='Some copies are promised elsewhere — " + Math.max(0, c.free) +
        " still free'>· also in " + esc(c.reserved_by.join(", ")) + "</span>";
  }
  return html;
}

// Compact stat labels, mirroring gear.py STAT_FIELDS order.
const GS_STATS = [["ac", "AC"], ["hp", "HP"], ["mana", "Mana"], ["attack", "ATK"],
  ["regen", "Regen"], ["manaregen", "MRegen"],
  ["astr", "STR"], ["asta", "STA"], ["aagi", "AGI"], ["adex", "DEX"],
  ["awis", "WIS"], ["aint", "INT"], ["acha", "CHA"],
  ["mr", "SvM"], ["fr", "SvF"], ["cr", "SvC"], ["dr", "SvD"], ["pr", "SvP"]];

// "AC 9 · HP 45 · SvC 10" — only the stats the item actually carries.
function gsStatLine(c) {
  if (!c) return "";
  const st = c.stats || {};
  const parts = GS_STATS.filter(([k]) => st[k]).map(([k, l]) => l + " " + st[k]);
  if (c.haste) parts.push("Haste " + c.haste + "%");
  return parts.join(" · ");
}

// Named click/proc/focus/worn effects as badges — the whole reason gear.py's effect
// columns got fixed; a cleric's "Focus: Improved Healing I" is the deciding factor.
function gsEffectsHtml(c) {
  if (!c || !(c.effects || []).length) return "";
  return c.effects.map((e) =>
    "<span class='gs-fx gs-fx-" + esc(e.kind.toLowerCase()) + "' title='" +
    esc(e.kind) + " effect'>" + esc(e.kind) + ": " + esc(e.name) + "</span>").join(" ");
}

// Signed stat difference vs the slot's current pick, so a swap's real cost is visible
// (the old dropdown could only show absolutes — you had to do this arithmetic yourself).
function gsDeltaHtml(c, cur) {
  if (!cur || !c || cur.item_id === c.item_id) return "";
  const a = c.stats || {}, b = cur.stats || {};
  const out = [];
  for (const [k, l] of GS_STATS) {
    const d = (a[k] || 0) - (b[k] || 0);
    if (d) out.push("<span class='" + (d > 0 ? "gs-up" : "gs-down") + "'>" +
      (d > 0 ? "+" : "") + d + " " + l + "</span>");
  }
  const dh = (c.haste || 0) - (cur.haste || 0);
  if (dh) out.push("<span class='" + (dh > 0 ? "gs-up" : "gs-down") + "'>" +
    (dh > 0 ? "+" : "") + dh + " Haste</span>");
  return out.length ? "<div class='gs-delta'>" + out.join(" ") + "</div>"
                    : "<div class='gs-delta hint'>same stats</div>";
}

// The candidate record behind a pick, when it's still in the current candidate list.
function gsPickCand(slot, pick) {
  if (!pick) return null;
  return (gsEd.cands[slot] || []).find((x) => x.item_id === pick.item_id) || null;
}

// Live set totals. Haste is the MAX single worn item, never summed (same truth as
// gear.py worn_stats) — summing it would badly overstate a set.
function gsRenderTotals() {
  const tot = {}; let haste = 0, unknown = 0, stowed = 0;
  for (const sm of gsEd.slots) {
    // Virtual slots (Avatar / 2-Hander) are gear the toon CARRIES, never wears, so their
    // stats must not land in any total — this bar, Comp Power, or the group roll-up.
    // Counted and named instead of silently dropped, so the bar can't look wrong.
    if (sm.extra) {
      for (let k = 0; k < (sm.paired ? 2 : 1); k++) {
        if (gsEd.picks[gsEdKey(sm.slot, k)]) stowed++;
      }
      continue;
    }
    for (let k = 0; k < (sm.paired ? 2 : 1); k++) {
      const pick = gsEd.picks[gsEdKey(sm.slot, k)];
      if (!pick) continue;
      const c = gsPickCand(sm.slot, pick);
      if (!c) { unknown++; continue; }   // pick outside the current filter — no stats
      for (const [key] of GS_STATS) if ((c.stats || {})[key]) tot[key] = (tot[key] || 0) + c.stats[key];
      if ((c.haste || 0) > haste) haste = c.haste;
    }
  }
  const cells = GS_STATS.filter(([k]) => tot[k])
    .map(([k, l]) => "<span class='gs-tot'><b>" + tot[k] + "</b> " + l + "</span>");
  if (haste) cells.push("<span class='gs-tot'><b>" + haste + "%</b> Haste</span>");
  const notes = (unknown ? "<span class='hint'>· " + unknown +
      " pick(s) outside the current filter not counted</span>" : "") +
    (stowed ? "<span class='hint'>· " + stowed + " carried in bags (Avatar / 2-Hander /" +
      " Mount / WW Clicky) — reserved, not counted in any stats</span>" : "");
  $("gsEdTotals").innerHTML = cells.length ? cells.join("") + notes
    : (stowed ? notes : "<span class='hint'>No pieces picked yet.</span>");
}

function gsEdCount() {
  let txt = Object.keys(gsEd.picks).length + " piece(s) picked";
  // Where the SET's pieces physically are, from the target's newest dump. This is
  // the line that answers "did I strip this guy?" without opening the Move Plan —
  // and it never touches the set itself, which is the whole point: the set is the
  // goal, this is the current reality, and reality is re-read from the dump on
  // every open (there is nothing to "save" to keep it current).
  if (gsEd.targetName) {
    let eq = 0, bag = 0, away = 0;
    for (const [key, p] of Object.entries(gsEd.picks)) {
      const c = gsPickCand(key.slice(0, key.lastIndexOf("|")), p);
      if (c && c.target_has === "worn") eq++;
      else if (c && c.target_has) bag++;
      else away++;
    }
    const bits = [eq + " equipped"];
    if (bag) bits.push(bag + " in their bags");
    if (away) bits.push(away + " elsewhere");
    txt += " · " + gsEd.targetName + ": " + bits.join(" · ");
  }
  if (gsEd.raceFilter) txt += " · showing " + gsEd.raceFilter + "-wearable gear only";
  else if (gsEd.raceFilter === null)
    txt += " · ⚠ assigned toon has no race on file — race-locked gear is NOT filtered";
  $("gsEdCount").textContent = txt;
}

// ----- left pane: one row per worn slot, showing the current pick in full -----
function renderSetEditor() {
  const tbody = $("gsEdRows");
  tbody.innerHTML = "";
  let firstKey = null;
  for (const sm of gsEd.slots) {
    const copies = sm.paired ? 2 : 1;
    // What the target is WEARING in this slot right now, live from their newest
    // dump — deliberately kept distinct from the picks. The set is what you want
    // them to wear; this is what they have on. Conflating the two is what made a
    // stripped toon look fully equipped (2026-08-06: a toon had shipped 9 pieces
    // to other toons and the editor still showed a full loadout with ✔s).
    const pickedIds = [];
    for (let k = 0; k < copies; k++) {
      const p = gsEd.picks[gsEdKey(sm.slot, k)];
      if (p) pickedIds.push(p.item_id);
    }
    // Worn pieces that AREN'T one of this slot's picks — i.e. what they're wearing
    // *instead*. Consumed one per row so a paired slot can't show the same earring twice.
    const wornInstead = (gsEd.cands[sm.slot] || [])
      .filter((x) => x.target_has === "worn" && !pickedIds.includes(x.item_id));
    for (let k = 0; k < copies; k++) {
      const key = gsEdKey(sm.slot, k);
      const cur = gsEd.picks[key];
      const c = gsPickCand(sm.slot, cur);
      // Default the panel to the first slot that actually HAS candidates — Charm is
      // slot 0 and is empty for almost every set, so opening on it looks broken.
      if (!firstKey && (gsEd.cands[sm.slot] || []).length) firstKey = key;
      const steal = c && c.free <= 0 && c.reserved_by.length;
      const tr = document.createElement("tr");
      tr.className = "gs-slotrow" + (gsEd.sel === key ? " gs-slotrow-sel" : "") +
        (steal ? " gs-slotrow-steal" : "");
      tr.dataset.key = key;
      tr.dataset.slot = sm.slot;
      const nAvail = (gsEd.cands[sm.slot] || []).length;
      // WHERE the picked piece is right now, from the target's own dump. Split out
      // of the old single ✔, which fired for worn/bags/shared alike and so read as
      // "equipped" when the piece was actually sitting in a bag — or on nobody.
      const has = c ? c.target_has : "";
      const badge =
        has === "worn"   ? " <span class='gs-have-dot' title='Equipped on " +
                             esc(gsEd.targetName || "the target") + " right now'>✔ equipped</span>" :
        has === "shared" ? " <span class='gs-have-bag' title='In their shared bank — not equipped'>in shared bank</span>" :
        has             ? " <span class='gs-have-bag' title='In their own bags/bank — not equipped, just log in and wear it'>in their bags</span>" : "";
      let pickHtml;
      if (!cur) {
        pickHtml = "<span class='hint'>— empty —</span>" +
          (nAvail ? " <span class='gs-navail'>" + nAvail + " available</span>" : "");
      } else if (!c) {
        // survives a class/race filter change — never silently dropped
        pickHtml = "<b>" + esc(cur.item_name) + "</b> <span class='hint'>(kept — outside current filter)</span>";
      } else {
        pickHtml = "<b>" + esc(c.name) + "</b>" + badge +
          "<div class='gs-statline'>" + esc(gsStatLine(c)) + "</div>" +
          (gsEffectsHtml(c) ? "<div class='gs-fxline'>" + gsEffectsHtml(c) + "</div>" : "");
      }
      // "Wearing now" line: only when it tells you something — the pick isn't on
      // them, or they're wearing something in a slot the set doesn't cover. Silent
      // when the pick IS equipped (the ✔ already says so) and when an unpicked slot
      // is empty anyway.
      if (gsEd.targetName && has !== "worn") {
        const w = wornInstead.shift();
        if (cur || w) {
          pickHtml += "<div class='gs-nowline'>wearing now: " +
            (w ? "<b>" + esc(w.name) + "</b>" : "<i>nothing</i>") + "</div>";
        }
      }
      tr.innerHTML = "<td class='gs-slotname'>" + esc(sm.slot) + (copies > 1 ? " " + (k + 1) : "") + "</td>" +
        "<td class='gs-slotpick'>" + pickHtml + "</td>";
      tr.addEventListener("click", () => gsSelectSlot(key, sm.slot));
      tbody.appendChild(tr);
    }
  }
  // ORPHANED PICKS. This table draws one row per known slot, so a pick whose slot is
  // blank or unrecognised was invisible here while STILL claiming its item — the user
  // changed Sleeper Monk 2's Neck and the comp check kept reporting the old Yelinak's
  // Talisman, because a second slotless row went on claiming it (2026-08-09, from the
  // Macro Builder import). Never hide a claim: list it with a way to drop it.
  const known = new Set();
  for (const sm of gsEd.slots)
    for (let k = 0; k < (sm.paired ? 2 : 1); k++) known.add(gsEdKey(sm.slot, k));
  const orphans = Object.keys(gsEd.picks).filter((k) => !known.has(k));
  for (const key of orphans) {
    const p = gsEd.picks[key];
    const tr = document.createElement("tr");
    tr.className = "gs-slotrow gs-slotrow-steal";
    tr.innerHTML = "<td class='gs-slotname'>no slot</td>" +
      "<td class='gs-slotpick'><b>" + esc(p.item_name) + "</b>" +
      "<div class='hint'>This pick has no slot, so it never showed in a row above — " +
      "but it still claims the item. Drop it unless you know why it is here." +
      " <a href='#' class='gs-orphan-del'>remove</a></div></td>";
    tr.querySelector(".gs-orphan-del").addEventListener("click", (e) => {
      e.preventDefault();
      delete gsEd.picks[key];
      renderSetEditor();
    });
    tbody.appendChild(tr);
  }
  if (!gsEd.sel && firstKey) gsEd.sel = firstKey;
  gsEdCount();
  gsRenderTotals();
  if (gsEd.sel) {
    const row = tbody.querySelector("tr[data-key='" + gsEd.sel.replace(/'/g, "\\'") + "']");
    if (row) row.classList.add("gs-slotrow-sel");
    renderCandidates();
  }
}

function gsSelectSlot(key, slot) {
  gsEd.sel = key;
  gsEd.selSlot = slot;
  document.querySelectorAll("#gsEdRows tr").forEach((r) =>
    r.classList.toggle("gs-slotrow-sel", r.dataset.key === key));
  renderCandidates();
}

// ----- right pane: every owned candidate for the selected slot -----
function renderCandidates() {
  const box = $("gsCandList");
  const key = gsEd.sel;
  if (!key) { box.innerHTML = "<p class='hint gs-cand-empty'>Select a slot.</p>"; return; }
  const slot = key.slice(0, key.lastIndexOf("|"));
  const cur = gsEd.picks[key];
  const curC = gsPickCand(slot, cur);
  const slotMeta = gsEd.slots.find((s) => s.slot === slot) || {};
  $("gsCandTitle").textContent = slot + (slotMeta.paired
    ? " " + (parseInt(key.slice(key.lastIndexOf("|") + 1), 10) + 1) : "");

  let list = (gsEd.cands[slot] || []).slice();
  if ($("gsCandFree").checked) list = list.filter((c) => c.free > 0 || (cur && cur.item_id === c.item_id));
  const sort = $("gsCandSort").value;
  const by = { ac: "ac", hp: "hp", mana: "mana" }[sort];
  if (sort === "name") list.sort((a, b) => a.name.localeCompare(b.name));
  else if (by) list.sort((a, b) => (b[by] || 0) - (a[by] || 0) || b.score - a.score);
  else list.sort((a, b) => b.score - a.score || b.ac - a.ac);

  const rows = ["<div class='gs-cand gs-cand-none" + (!cur ? " gs-cand-sel" : "") +
    "' data-iid='0'><div class='gs-cand-name'>— empty —</div>" +
    "<div class='hint'>leave this slot out of the set</div></div>"];
  for (const c of list) {
    const sel = cur && cur.item_id === c.item_id;
    const taken = c.free <= 0 && c.reserved_by.length;
    rows.push(
      "<div class='gs-cand" + (sel ? " gs-cand-sel" : "") + (taken && !sel ? " gs-cand-steal" : "") +
        "' data-iid='" + c.item_id + "'>" +
        "<div class='gs-cand-top'>" +
          "<span class='gs-cand-name'>" + (c.target_has ? "✔ " : "") + esc(c.name) + "</span>" +
          (c.score ? "<span class='gs-cand-score' title='class-weighted score'>⚡" + c.score + "</span>" : "") +
          (c.owned > 1 ? "<span class='hint'>" + Math.max(0, c.free) + " free of " + c.owned + "</span>" : "") +
          (taken && !sel ? "<span class='gs-warn' title='Also picked by " +
            esc(c.reserved_by.join(", ")) + ". Picking it here changes nothing over there — " +
            "both sets keep it, and the move plan says which toon can actually get a copy.'>" +
            "⚑ also in " + esc(c.reserved_by.join(", ")) + "</span>" : "") +
        "</div>" +
        "<div class='gs-statline'>" + esc(gsStatLine(c)) + "</div>" +
        (gsEffectsHtml(c) ? "<div class='gs-fxline'>" + gsEffectsHtml(c) + "</div>" : "") +
        gsDeltaHtml(c, curC) +
        "<div class='gs-cand-where'>" + gsEdWhere(c) + "</div>" +
      "</div>");
  }
  // An account-bound list is short ON PURPOSE — it only ever shows copies held on
  // the assigned toon's own account, because a mount or claim clicky can't cross
  // accounts. Saying so up front stops a 1-item list reading as a missing-data bug.
  const boundChar = $("gsEdAssign").value
    ? charById(parseInt($("gsEdAssign").value, 10)) : null;
  const boundAcct = boundChar && boundChar.account_id != null
    ? acctLabel(acctById(boundChar.account_id)) : null;
  const bound = slotMeta.account_bound
    ? "<p class='hint gs-cand-empty'>Account-bound: " + (boundAcct
        ? "only copies on <b>" + esc(boundAcct) + "</b> are listed"
        : "<b>assign this set to a toon</b> to see its account's copies") +
      " — a mount or claim clicky can never be traded or parcelled in from another account.</p>"
    : "";
  // rows always carries the "— empty —" option, so keep it even when nothing
  // matched: clearing a pick has to stay possible.
  box.innerHTML = bound + rows.join("") + (list.length ? ""
    : "<p class='hint gs-cand-empty'>" + (slotMeta.account_bound
        ? "Nothing on this account has one yet."
        : "Nothing you own fits this slot for this class.") + "</p>");
  box.querySelectorAll(".gs-cand").forEach((el) => el.addEventListener("click", () => {
    const iid = parseInt(el.dataset.iid, 10);
    if (!iid) delete gsEd.picks[key];
    else {
      const cand = (gsEd.cands[slot] || []).find((x) => x.item_id === iid);
      gsEd.picks[key] = { item_id: iid, item_name: cand ? cand.name : "" };
    }
    renderSetEditor();          // refresh left rows + totals, keeps the slot selected
  }));
}

$("gsCandSort").addEventListener("change", () => { if (gsEd) renderCandidates(); });
$("gsCandFree").addEventListener("change", () => { if (gsEd) renderCandidates(); });

$("gsNewBtn").addEventListener("click", () => guard(() => openSetEditor(null)));
$("gsEdCancelBtn").addEventListener("click", () => { $("gsEditorCard").hidden = true; gsEd = null; });
$("gsEdClass").addEventListener("change", () => guard(gsEdFetch));
$("gsEdAssign").addEventListener("change", () => guard(gsEdFetch));

$("gsEdClearBtn").addEventListener("click", () => {
  if (!gsEd) return;
  gsEd.picks = {};
  renderSetEditor();
  toast("All slots emptied — hit Save to free this set's claims (the set itself stays).");
});

// ⭐ Fill every EMPTY armor/jewelry slot with the top-scored free candidate.
// Weapons/range/ammo stay manual (scoring can't judge weapon ratios/procs well).
// Avatar joins the weapons for the same reason: the slot's candidate list is now
// exactly the weapons that proc Avatar (server-side, proc 2434), but WHICH of them a
// toon can actually swing is a weapon-skill call the stat score does not model — a
// monk wants the Brawl Stick, a warrior the Warsword, and "best AC/HP" cannot tell
// them apart.
// Mount / WW Clicky skip for a different reason: they have no stats at all, so
// "best" is meaningless for them — which mount or which port clicky is entirely
// your call, and auto-picking one would just claim a copy at random.
const GS_AUTOFILL_SKIP = new Set(["Charm", "Primary", "Secondary", "Range", "Ammo", "Power",
                                  "Avatar", "2-Hander", "Mount", "WW Clicky"]);
$("gsEdBestBtn").addEventListener("click", () => {
  if (!gsEd) return;
  const target = $("gsEdAssign").value ? charById(parseInt($("gsEdAssign").value, 10)) : null;
  const used = {};
  for (const p of Object.values(gsEd.picks)) used[p.item_id] = (used[p.item_id] || 0) + 1;
  let filled = 0;
  for (const sm of gsEd.slots) {
    if (GS_AUTOFILL_SKIP.has(sm.slot)) continue;
    const copies = sm.paired ? 2 : 1;
    for (let k = 0; k < copies; k++) {
      const key = gsEdKey(sm.slot, k);
      if (gsEd.picks[key]) continue;
      const cand = (gsEd.cands[sm.slot] || []).find((c) => {
        const u = used[c.item_id] || 0;
        if (c.free - u <= 0) return false;                 // no free copy left
        if (c.lore && u > 0) return false;                 // LORE: one per character
        if (c.fvnodrop && !(target && (c.holders || []).some((h) => h.holder === target.name)))
          return false;                                    // No-Trade gear can't move to the target
        return true;
      });
      if (!cand) continue;
      gsEd.picks[key] = { item_id: cand.item_id, item_name: cand.name };
      used[cand.item_id] = (used[cand.item_id] || 0) + 1;
      filled++;
    }
  }
  renderSetEditor();
  toast(filled
    ? "Filled " + filled + " empty slot(s) with your best available pieces — weapons are yours to pick."
    : "Nothing to fill — every slot is picked or has no free candidate.");
});

$("gsEdSaveBtn").addEventListener("click", () => guard(async () => {
  const name = $("gsEdName").value.trim();
  if (!name) throw new Error("Give the set a name.");
  const items = [];
  for (const [key, p] of Object.entries(gsEd.picks)) {
    const [slot, k] = key.split("|");
    items.push({ item_id: p.item_id, item_name: p.item_name, slot, slot_index: parseInt(k, 10) });
  }
  // No steal prompt. A set is what you want this toon to wear, so a piece may live
  // in as many sets as you like and saving here never edits another set. Shortfalls
  // show up in the move plan as "reserved", where they can be acted on.
  if (!items.length &&
      !confirm("Save '" + name + "' EMPTY? The set is kept but every slot clears and all its item claims are freed."))
    return;
  const payload = {
    name, items,
    class_name: $("gsEdClass").value || "",
    assigned_char_id: $("gsEdAssign").value ? parseInt($("gsEdAssign").value, 10) : null,
  };
  let res;
  if (gsEd.setId) res = await api("PUT", "/gearsets/" + gsEd.setId, payload);
  else { res = await api("POST", "/gearsets", payload); gsEd.setId = res.id; }
  let msg = "Saved '" + name + "' — " + items.length + " piece(s).";
  if (res.contested && res.contested.length)
    msg += " Shared with other sets: " + res.contested.map((c) =>
      c.item + " (" + c.want + " sets want it, you own " + c.owned + ")").join("; ") +
      ". Every set keeps its pick — see the move plan for who gets a copy.";
  toast(msg);
  gsFocusId = gsEd.setId;
  await loadGearSets();
}));

// ---------- gear sets: the move plan ----------
function gsLocHtml(r) {
  const badge = r.bucket ? "<span class='gs-loc gs-loc-" + esc(r.bucket) + "'>" + esc(r.bucket) + "</span>" : "";
  const detail = (r.bucket === "bags" || r.bucket === "bank" || r.bucket === "hoard") ? " " + esc(r.from_loc || "") : "";
  return badge + detail;
}

function renderPlanResult(d) {
  const box = $("gsPlan");
  const sum = d.summary || {};
  const bs = sum.by_status || {};
  // Sets whose same-account hand-offs won't all fit in the shared bank at once
  const tight = (d.plans || []).filter((p) => p.shared_bank && p.shared_bank.overflow);
  const chips =
    "<div class='gs-sum'>" +
    "<span class='gs-chip'>" + (sum.pieces || 0) + " pieces</span>" +
    "<span class='gs-chip gs-ok'>" + (sum.satisfied || 0) + " already there</span>" +
    (bs.grab ? "<span class='gs-chip gs-free'>" + bs.grab + " in shared bank</span>" : "") +
    (bs.swap ? "<span class='gs-chip gs-swap'>" + bs.swap + " same-account swap</span>" : "") +
    (bs.trade ? "<span class='gs-chip gs-trade'>" + bs.trade + " trade now</span>" : "") +
    (bs.parcel ? "<span class='gs-chip gs-parcel'>" + bs.parcel + " parcel</span>" : "") +
    (sum.blocked ? "<span class='gs-chip gs-bad'>" + sum.blocked + " blocked</span>" : "") +
    (tight.length
      ? "<span class='gs-chip gs-bad' title='More same-account hand-offs than free shared-bank slots — those sets need more than one bank round'>🧳 " +
        tight.length + " set(s) exceed shared-bank space</span>"
      : "") +
    "<span class='gs-chip'>" + (d.workorder || []).length + " login(s) needed</span>" +
    // How much of this plan rests on inventory nobody has looked at lately. Counted
    // over move rows only — a stale "already worn" row promises nothing.
    (sum.stale_moves
      ? "<span class='gs-chip gs-stale' title='These moves were routed from dumps older than " +
        Math.floor((sum.stale_h || 168) / 24) + " days — the item may already be gone'>⏳ " +
        sum.stale_moves + " from stale dumps</span>"
      : "") +
    "</div>" +
    (sum.stale_moves
      ? "<div class='gs-orderhint gs-warntext'>⏳ " + sum.stale_moves + " move(s) rest on a dump " +
        "older than " + Math.floor((sum.stale_h || 168) / 24) + " days" +
        (sum.oldest_source_h ? " (oldest: " + Math.floor(sum.oldest_source_h / 24) + "d)" : "") +
        " — re-dump those holders before you trust the routing.</div>"
      : "");

  let work = "";
  if ((d.workorder || []).length) {
    work = "<h4 class='gs-h'>Work order — biggest first</h4><div class='gs-workgrid'>" +
      d.workorder.map((h) => {
        const swaps = h.routes.swap || 0;
        const items = h.items.map((it) =>
          "<div class='gs-workitem'>" + gsBadge(it.route) + " <b>" + esc(it.item) + "</b>" +
          " <span class='hint'>→ " + esc(it.to) + (it.manual ? " · MANUAL (" + esc(it.bucket) + ")" : "") +
          (it.loc && it.bucket !== "worn" ? " · " + esc(it.loc) : "") + "</span></div>").join("");
        return "<div class='gs-holder'>" +
          "<div class='gs-holderhead'>Log in <b>" + esc(h.holder) + "</b>" +
          (h.banker ? " <span class='gs-route gs-free' title='Banker/mule — parked in town, easy grab'>🏦 banker</span>" : "") +
          // how old the picture of this holder's bags is, before you go log them in
          (h.stale ? " <span class='st-warn' title='This holder&apos;s inventory dump is old — what it says they have may be out of date'>⏳ " +
                     Math.floor(h.dump_age_h / 24) + "d old</span>" : "") +
          " <span class='hint'>" + esc(h.account_alias || "no account") + "</span>" +
          "<span class='grow'></span><span class='gs-count'>" + h.total + "</span></div>" +
          (swaps ? "<div class='gs-orderhint'>🔁 do this one BEFORE logging the target — it hands off through the shared bank</div>" : "") +
          (h.manual ? "<div class='gs-orderhint gs-warntext'>⚠ " + h.manual + " manual pull(s) — hoard/persona can't be auto-lifted</div>" : "") +
          items + "</div>";
      }).join("") + "</div>";
  }

  const setBlocks = (d.plans || []).map((p) => {
    if (!p.target) return "<div class='warn error'>" + esc(p.name) + ": " + esc(p.error || "no target") + "</div>";
    const stale = !p.target.dumped
      ? " <span class='st-warn'>(no dump for " + esc(p.target.name) + " — can't tell what they already have)</span>"
      : (p.target.dump_age_h > 72 ? " <span class='st-warn'>(dump " + Math.floor(p.target.dump_age_h / 24) + "d old)</span>" : "");
    const rows = p.rows.map((r) =>
      "<tr class='" + (r.stale_source ? "gs-rowstale" : "") + "'>" +
      "<td class='hint'>" + esc(r.slot) + (r.slot_index ? " 2" : "") + "</td>" +
      "<td><b>" + esc(r.item_name) + "</b>" +
      (r.attune_risk ? " <span class='st-warn' title='Attunable and currently worn by the holder — may be stuck'>⚠ attuned?</span>" : "") +
      (r.stale_source ? " <span class='st-warn' title='Routed from a dump " +
        Math.floor(r.source_age_h / 24) + " days old'>⏳</span>" : "") + "</td>" +
      "<td>" + gsBadge(r.status, r.note) + "</td>" +
      "<td>" + (r.holder ? (r.holder_banker ? "🏦 " : "") + esc(r.holder) + " <span class='hint'>" + esc(r.account_alias || "") + "</span> " + gsLocHtml(r)
                        : (r.note ? "<span class='hint'>" + esc(r.note) + "</span>" : "—")) + "</td></tr>").join("");
    // Does the shared-bank leg fit? Each same-account hand-off parks one item in
    // that account's 8-slot shared bank between logins, so free slots caps how many
    // can move per round. Over it, the run stalls mid-way holding gear.
    const sb = p.shared_bank;
    let sbLine = "";
    if (sb && sb.needed) {
      const detail = sb.free_direct + " empty slot(s) + " + sb.free_bag + " free bag slot(s)" +
        " · seen on " + esc(sb.observed_from) +
        (sb.age_h ? " " + sb.age_h + "h ago" : " just now");
      sbLine = sb.overflow
        ? "<div class='gs-orderhint gs-warntext'>🧳 <b>" + sb.needed + " hand-offs but only " +
          sb.free_total + " free shared-bank slot(s)</b> on " + esc(p.target.account_alias) +
          " — this needs <b>" + sb.rounds + " rounds</b>, or free up space first. " +
          "<span class='hint'>(" + detail + ")</span></div>"
        : "<div class='gs-orderhint'>🧳 " + sb.needed + " hand-off(s) through " +
          esc(p.target.account_alias) + "'s shared bank · " + sb.free_total +
          " free slot(s) — fits in one round. <span class='hint'>(" + detail + ")</span></div>";
    }
    return "<details class='gs-set' open><summary><b>" + esc(p.name) + "</b> → " +
      esc(p.target.name) + " <span class='hint'>" + esc(p.target.class_name || "") + " · " +
      esc(p.target.account_alias || "") + "</span>" + stale + "</summary>" + sbLine +
      "<table class='mc-table gs-plantable'><thead><tr><th>Slot</th><th>Item</th><th>Route</th><th>From</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table></details>";
  }).join("");

  box.innerHTML = (d.plans || []).length
    ? chips + work + "<h4 class='gs-h'>Per-set detail</h4>" + setBlocks
    : "<div class='hint'>No active sets with items — snapshot one first.</div>";
}

$("gsPlanBtn").addEventListener("click", () => guard(async () => {
  $("gsPlan").innerHTML = "<div class='hint'>Routing every active set…</div>";
  const d = await api("POST", "/gearsets/plan",
    { login: loginSlots(), stale_h: Number($("gsStale").value),
      focus: gsComp ? gsComp.ids : null });
  renderPlanResult(d);
}));
// tightening the trust window re-plans, so the flags update without a second click
$("gsStale").addEventListener("change", () => {
  if ($("gsPlan").querySelector(".gs-sum")) $("gsPlanBtn").click();
});

$("gsExportBtn").addEventListener("click", () => guard(async () => {
  // Focused: only this comp's moves go to MQ — same scoping as the Move Plan on
  // screen, so what you export is what you just read.
  const d = await api("POST", "/gearsets/export",
    { login: loginSlots(), stale_h: Number($("gsStale").value),
      focus: gsComp ? gsComp.ids : null });
  // Reuse the proven serve.py writers (same files the old Gear Planner wrote).
  const r1 = await fetch("/gearplan", { method: "POST", headers: { "Content-Type": "text/plain" }, body: d.plan_lua });
  const j1 = await r1.json().catch(() => ({}));
  if (!j1.ok) throw new Error("Could not write the plan file: " + (j1.error || r1.status));
  let parcelOk = false;
  try {
    const r2 = await fetch("/parcelsource", { method: "POST", headers: { "Content-Type": "text/plain" }, body: d.parcel_lua });
    parcelOk = r2.ok && (await r2.json()).ok === true;
  } catch (e) { /* parcel source is optional */ }
  toast("Sent " + d.plans.length + " plan(s): " +
    d.plans.map((p) => p.name + " → " + p.target + " (" + p.count + ")").join(", ") +
    " → " + j1.path + (parcelOk ? " (+ parcel source)" : ""));
}));

// ---------- lockout board ----------
const LOCK_FRESH_DAYS = 3;   // only trust lockout data imported within this window

function renderLockBoard() {
  const board = $("lockBoard");
  const now = Math.floor(Date.now() / 1000);
  const active = S.characters.filter((c) => c.status !== "retired");
  const isFresh = (lk) => lk.imported_at == null ||        // manual checks always show
    (now - lk.imported_at) <= LOCK_FRESH_DAYS * 86400;

  const byRaid = new Map();                                // raid_id -> [{c, lk}]
  let staleCount = 0;
  for (const c of active) {
    for (const lk of c.lockouts) {
      if (!isFresh(lk)) { staleCount++; continue; }
      if (!byRaid.has(lk.raid_id)) byRaid.set(lk.raid_id, []);
      byRaid.get(lk.raid_id).push({ c, lk });
    }
  }
  if (!byRaid.size) {
    board.innerHTML = "<div class='hint'>No current lockouts" +
      (staleCount ? " (" + staleCount + " hidden as stale — re-run /lua run mychars_lockouts and reload)" : "") +
      " — everyone is free.</div>";
    return;
  }
  board.innerHTML = "";
  for (const raid of S.raids) {
    const entries = byRaid.get(raid.id);
    if (!entries) continue;
    entries.sort((x, y) => (x.lk.expires_at || 0) - (y.lk.expires_at || 0));
    const rows = entries.map(({ c, lk }) =>
      "<div class='lb-row'><span class='dot' style='color:" + acctColor(c.account_id) + "'>●</span>" +
      "<b>" + esc(c.name) + "</b>" +
      "<span class='lb-saved'>" + fmtRemain(lk.expires_at) + "</span>" +
      "<span class='who' title='" + esc(c.class_name || "") + "'>" + esc(charAcctLabel(c)) + "</span>" +
      "</div>").join("");
    const div = document.createElement("div");
    div.className = "lb-raid";
    div.innerHTML = "<h4>" + esc(raid.name) +
      "<span class='free lb-saved'>" + entries.length + " saved</span></h4>" + rows;
    board.appendChild(div);
  }
  if (staleCount) {
    const note = document.createElement("div");
    note.className = "hint";
    note.textContent = staleCount + " lockout(s) hidden — export older than " + LOCK_FRESH_DAYS +
      " days. Re-run /lua run mychars_lockouts on those toons, then Load lockouts from MQ.";
    board.appendChild(note);
  }
}

// ---------- split availability (free pool by class, per active target) ----------
function renderSplitBoard() {
  const board = $("splitBoard");
  if (!board) return;
  const now = Math.floor(Date.now() / 1000);
  const minLvl = parseInt(($("sbMinLvl") && $("sbMinLvl").value) || "59", 10) || 0;
  const active = S.characters.filter((c) =>
    c.status !== "retired" && (c.level || 0) >= minLvl);   // split-viable bodies only
  const isFresh = (lk) => lk.imported_at == null ||
    (now - lk.imported_at) <= LOCK_FRESH_DAYS * 86400;

  // raid_id -> Set(character_id) of toons currently LOCKED (fresh + unexpired; NULL-expiry = locked)
  const lockedByRaid = new Map();
  for (const c of active) {
    for (const lk of c.lockouts) {
      if (!isFresh(lk)) continue;
      if (lk.expires_at && lk.expires_at <= now) continue;   // expired -> free again
      if (!lockedByRaid.has(lk.raid_id)) lockedByRaid.set(lk.raid_id, new Set());
      lockedByRaid.get(lk.raid_id).add(c.id);
    }
  }
  if (!lockedByRaid.size) {
    board.innerHTML = "<div class='hint'>No active lockouts — every toon is free for every target.</div>";
    return;
  }
  board.innerHTML = "";
  for (const raid of S.raids) {
    const locked = lockedByRaid.get(raid.id);
    if (!locked) continue;
    const free = active.filter((c) => !locked.has(c.id));
    const chips = sbGroupByAcct ? sbAcctChips(free) : sbClassChips(free);
    // one toon per account can log in at once → distinct free accounts is the true fieldable count
    const freeAccts = new Set(free.map((c) => c.account_id != null ? "a" + c.account_id : "n" + c.name)).size;
    const div = document.createElement("div");
    div.className = "lb-raid";
    div.innerHTML = "<h4>" + esc(raid.name) +
      "<span class='free lb-saved' title='" + freeAccts + " account(s) have a free toon = most bodies you can field at once · " +
      free.length + " free toons of " + active.length + " total'>" +
      freeAccts + " acct / " + free.length + " toon</span></h4>" +
      "<div class='sb-chips'>" + (chips || "<span class='hint'>no toons free</span>") + "</div>";
    board.appendChild(div);
  }
}

// FREE toons grouped by CLASS → one chip per class (the scarcest class caps your splits)
function sbClassChips(free) {
  const byClass = new Map();
  for (const c of free) {
    const cls = c.class_name || "?";
    if (!byClass.has(cls)) byClass.set(cls, []);
    byClass.get(cls).push(c);
  }
  return [...byClass.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([cls, toons]) =>
      "<span class='sb-chip' style='border-color:" + clsColor(cls) + "' title='" +
      esc(toons.map((t) => t.name).join(", ")) + "'>" +
      "<span class='sb-cls' style='color:" + clsColor(cls) + "'>" + esc(cls) + "</span>" +
      "<span class='sb-n'>" + toons.length + "</span></span>").join("");
}

// FREE toons grouped by ACCOUNT → one chip per account (only one of its toons can be live at once)
function sbAcctChips(free) {
  const byAcct = new Map();                                  // account_id -> [toon]
  for (const c of free) {
    const key = c.account_id != null ? c.account_id : "n:" + c.name;
    if (!byAcct.has(key)) byAcct.set(key, []);
    byAcct.get(key).push(c);
  }
  return [...byAcct.entries()]
    .sort((a, b) => b[1].length - a[1].length ||
      charAcctLabel(a[1][0]).localeCompare(charAcctLabel(b[1][0])))
    .map(([key, toons]) => {
      const aid = typeof key === "number" ? key : null;
      const col = aid != null ? acctColor(aid) : "var(--muted)";
      const label = charAcctLabel(toons[0]);
      const inner = toons
        .sort((x, y) => (x.class_name || "").localeCompare(y.class_name || ""))
        .map((t) => "<span class='sb-cls' style='color:" + clsColor(t.class_name || "?") +
          "' title='" + esc(t.name) + "'>" + esc(t.class_name || "?") + "</span>").join(" ");
      return "<span class='sb-chip sb-acctchip' style='border-color:" + col + "' title='" +
        esc(label) + " — pick ONE: " + esc(toons.map((t) => t.name + " (" + (t.class_name || "?") + ")").join(", ")) + "'>" +
        "<span class='sb-acct' style='color:" + col + "'>" + esc(label) + "</span>" +
        inner + "</span>";
    }).join("");
}

// ---------- keys & access ----------
function renderKeys() {
  renderLockBoard();
  renderSplitBoard();
  const chars = S.characters.filter((c) => c.status !== "retired");
  $("ulChar").innerHTML = chars.map((c) => "<option value='" + c.id + "'>" + esc(c.name) + "</option>").join("");
  $("ulStatus").innerHTML = S.meta.unlock_statuses.map((s) =>
    "<option" + (s === "Complete" ? " selected" : "") + ">" + s + "</option>").join("");
  $("ulExamples").innerHTML = S.meta.unlock_examples.map((u) =>
    "<option value='" + esc(u.name) + "'>").join("");

  const tbody = $("unlockRows");
  tbody.innerHTML = "";
  for (const u of S.unlocks) {
    const c = charById(u.character_id);
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + esc(c ? c.name : "?") + "</td>" +
      "<td><b>" + esc(u.name) + "</b></td>" +
      "<td>" + esc(u.expansion) + "</td>" +
      "<td>" + esc(u.category) + "</td>" +
      "<td>" + esc(u.scope) + "</td>" +
      "<td class='pri-" + esc(u.priority) + "'>" + esc(u.priority) + "</td>" +
      "<td><select class='ul-" + esc(u.status).replace(/ /g, "") + "'>" +
        S.meta.unlock_statuses.map((s) => "<option" + (s === u.status ? " selected" : "") + ">" + s + "</option>").join("") +
      "</select></td>" +
      "<td>" + esc(u.verified_date) + " <button class='btn btn-ghost btn-sm vtoday' type='button' title='Mark verified today'>✓</button></td>" +
      "<td>" + esc(u.notes) + "</td>" +
      "<td><button class='raid-del uldel' type='button'>✕</button></td>";
    tr.querySelector("select").addEventListener("change", (e) => guard(async () => {
      await api("PUT", "/unlocks/" + u.id, { status: e.target.value });
      await reload();
    }));
    tr.querySelector(".vtoday").addEventListener("click", () => guard(async () => {
      await api("PUT", "/unlocks/" + u.id, { verified_date: new Date().toISOString().slice(0, 10) });
      await reload();
    }));
    tr.querySelector(".uldel").addEventListener("click", () => guard(async () => {
      await api("DELETE", "/unlocks/" + u.id);
      await reload();
    }));
    tbody.appendChild(tr);
  }

  // lockout grid
  $("lockHead").innerHTML = "<tr><th>Character</th>" + S.raids.map((r) =>
    "<th>" + esc(r.name) + " <button class='raid-del' data-raid='" + r.id + "' type='button' title='Remove raid'>✕</button></th>").join("") + "</tr>";
  $("lockHead").querySelectorAll(".raid-del").forEach((b) => b.addEventListener("click", () => guard(async () => {
    if (!confirm("Remove this raid and all its lockouts?")) return;
    await api("DELETE", "/raids/" + b.dataset.raid);
    await reload();
  })));
  const grid = $("lockGrid");
  grid.innerHTML = "";
  for (const c of chars) {
    const expiry = {};
    c.lockouts.forEach((lk) => { expiry[lk.raid_id] = lk.expires_at; });
    const tr = document.createElement("tr");
    tr.innerHTML = "<td>" + esc(c.name) + "</td>" + S.raids.map((r) => {
      const rem = fmtRemain(expiry[r.id]);
      return "<td><input type='checkbox' data-raid='" + r.id + "'" +
        (c.lockout_raid_ids.includes(r.id) ? " checked" : "") + ">" +
        (rem ? "<div class='remain'>" + rem + "</div>" : "") + "</td>";
    }).join("");
    tr.querySelectorAll("input").forEach((cb) => cb.addEventListener("change", () => guard(async () => {
      const ids = [...tr.querySelectorAll("input:checked")].map((el) => parseInt(el.dataset.raid, 10));
      await api("PUT", "/characters/" + c.id + "/lockouts", { raid_ids: ids });
      await reload();
    })));
    grid.appendChild(tr);
  }
}

$("ulName").addEventListener("change", () => {
  const ex = S.meta.unlock_examples.find((u) => u.name === $("ulName").value);
  if (ex) {
    $("ulExpansion").value = ex.expansion;
    $("ulCategory").value = ex.category;
    $("ulPriority").value = ex.priority;
  }
});
$("ulAddBtn").addEventListener("click", () => guard(async () => {
  const name = $("ulName").value.trim();
  if (!name) throw new Error("Unlock needs a name.");
  await api("POST", "/unlocks", {
    character_id: parseInt($("ulChar").value, 10), name,
    expansion: $("ulExpansion").value.trim(), category: $("ulCategory").value,
    scope: $("ulScope").value, priority: $("ulPriority").value,
    status: $("ulStatus").value, verified_date: "", notes: "",
  });
  $("ulName").value = ""; $("ulExpansion").value = "";
  await reload();
}));
$("lockLoadBtn").addEventListener("click", () => guard(async () => {
  const res = await api("POST", "/lockouts/load", {});
  if (!res.found) {
    throw new Error("No lockout export yet — run  /lua run mychars_lockouts  on each toon first (looked at " + res.path + ").");
  }
  let msg = "Lockouts: " + res.applied + " applied from " + res.rows_in_file + " row(s)";
  if (res.raids_created.length) msg += " · new raids: " + res.raids_created.join(", ");
  if (res.skipped_expired) msg += " · " + res.skipped_expired + " already expired";
  toast(msg);
  if (res.unmatched.length)
    toast("Not in roster (import them first): " + res.unmatched.join(", "), true);
  await reload();
}));
{
  const saved = localStorage.getItem("eqmc-splitminlvl");
  if (saved !== null && $("sbMinLvl")) $("sbMinLvl").value = saved;
  if ($("sbMinLvl")) $("sbMinLvl").addEventListener("input", () => {
    localStorage.setItem("eqmc-splitminlvl", $("sbMinLvl").value);
    renderSplitBoard();
  });
  const syncSbGroupBtn = () => {
    const b = $("sbGroupBtn");
    if (b) b.textContent = sbGroupByAcct ? "👥 by class" : "👥 by account";
    const h = $("sbHint");
    if (h) h.textContent = sbGroupByAcct
      ? "per active target: FREE toons grouped by ACCOUNT · only ONE toon per account can be live, so 'accts free' = the most bodies you can field"
      : "per active target: FREE toons by class (not locked) · hover a chip for names · the scarcest class you need caps your splits";
  };
  if ($("sbGroupBtn")) $("sbGroupBtn").addEventListener("click", () => {
    sbGroupByAcct = !sbGroupByAcct;
    localStorage.setItem("eqmc-sbgroup", sbGroupByAcct ? "acct" : "class");
    syncSbGroupBtn();
    renderSplitBoard();
  });
  syncSbGroupBtn();
}
$("raidAddBtn").addEventListener("click", () => guard(async () => {
  const name = $("raidName").value.trim();
  if (!name) return;
  await api("POST", "/raids", { name });
  $("raidName").value = "";
  await reload();
}));

// ---------- import ----------
$("impFile").addEventListener("change", async () => {
  const f = $("impFile").files[0];
  if (f) $("impText").value = await f.text();
  importPreviewed = false;
  $("impCommitBtn").disabled = true;
});
$("impText").addEventListener("input", () => { importPreviewed = false; $("impCommitBtn").disabled = true; });

function rowLine(r) {
  return esc(r.name) + " · " + esc(r.server) + (r.class_name ? " · " + esc(r.class_name) : "") +
    (r.level != null ? " " + r.level : "") + (r.account ? " · " + esc(r.account) : "");
}

$("impPreviewBtn").addEventListener("click", () => guard(async () => {
  const plan = await api("POST", "/import/preview", { text: $("impText").value });
  const out = [];
  if (plan.stripped_fields.length)
    out.push("<div class='warn error'><span class='imp-strip'>Stripped credential fields (never imported):</span> " +
      esc(plan.stripped_fields.join(", ")) + "</div>");
  if (plan.ignored_fields.length)
    out.push("<div class='hint'>Ignored columns: " + esc(plan.ignored_fields.join(", ")) + "</div>");
  if (plan.missing_accounts.length)
    // "create them first" used to be the whole instruction, which on a fresh install
    // meant leaving Import, hand-adding six accounts, and pasting again — and if you
    // didn't, every character imported with NO account, quietly breaking the
    // one-per-account rule, the Lockout Board and Gear Sets routing. One button now
    // does it, and re-previews so you see the result before committing.
    out.push("<div class='warn warn'>Unknown accounts (characters would import " +
      "<b>unassigned</b>): " + esc(plan.missing_accounts.join(", ")) +
      " <button id='impMakeAcctsBtn' class='btn btn-sm' type='button' " +
      "data-aliases='" + esc(JSON.stringify(plan.missing_accounts)) + "'>➕ Create " +
      plan.missing_accounts.length + " account" +
      (plan.missing_accounts.length === 1 ? "" : "s") + "</button></div>");
  out.push("<div class='imp-block'><h4>New (" + plan.create.length + ")</h4>" +
    (plan.create.map((e) => "<div>+ " + rowLine(e.row) +
      (e.missing_account ? " <span class='imp-strip'>(account " + esc(e.missing_account) + " unknown)</span>" : "") +
      "</div>").join("") || "<span class='hint'>none</span>") + "</div>");
  out.push("<div class='imp-block'><h4>Updates (" + plan.update.length + ")</h4>" +
    (plan.update.map((e) => "<div>~ " + esc(e.row.name) + ": " +
      Object.entries(e.diff).map(([f, d]) =>
        f + " <span class='diff-old'>" + esc(d.old == null ? "—" : d.old) + "</span><span class='diff-new'>" +
        esc(d.new) + "</span>").join(", ") + "</div>").join("") || "<span class='hint'>none</span>") + "</div>");
  out.push("<div class='imp-block'><h4>Duplicates / unchanged (" + plan.duplicate.length + ")</h4>" +
    (plan.duplicate.map((e) => "<div>= " + rowLine(e.row) + "</div>").join("") || "<span class='hint'>none</span>") + "</div>");
  $("impResult").innerHTML = out.join("");
  importPreviewed = true;
  $("impCommitBtn").disabled = !(plan.create.length || plan.update.length);

  // Wired here rather than at load: the button only exists once a preview has
  // rendered one, and innerHTML above replaces any previous instance.
  const mk = $("impMakeAcctsBtn");
  if (mk) mk.addEventListener("click", () => guard(async () => {
    const aliases = JSON.parse(mk.dataset.aliases);
    let order = S.accounts.length;
    for (const alias of aliases)
      await api("POST", "/accounts", { alias, perks: [], launch_order: ++order,
                                       notes: "created from an import" });
    await reload();
    toast("Created " + aliases.length + " account(s) — re-previewing.");
    $("impPreviewBtn").click();
  }));
}));

$("impCommitBtn").addEventListener("click", () => guard(async () => {
  if (!importPreviewed) throw new Error("Preview first.");
  const res = await api("POST", "/import/commit", { text: $("impText").value });
  toast("Imported: " + res.created + " new, " + res.updated + " updated, " +
    res.skipped_duplicates + " duplicates skipped.");
  $("impCommitBtn").disabled = true;
  await reload();
}));

// ---------- import: the sample roster ----------
// Opt-in only. Auto-seeding this on first run meant a fresh install opened showing
// characters that were not yours; POST /seed without force is a no-op unless the DB is
// empty, so a real roster can never be polluted by a stray click.
$("impSeedBtn").addEventListener("click", () => guard(async () => {
  if (S.characters.length || S.accounts.length)
    throw new Error("The sample roster only loads into an empty roster — you already have " +
      S.characters.length + " character(s). Delete them first if you really want it.");
  const res = await api("POST", "/seed", {});
  if (!res.seeded) throw new Error("Nothing was loaded.");
  await reload();
  toast("Sample roster loaded — every name is a placeholder, delete them when you're done.");
}));

// ---------- import: in-game export CSVs (live levels) ----------
// Reads every mychars_export_<Name>.csv server-side and drops the rows into the
// normal Preview → Import pipeline (so diffs are reviewed, blanks never clobber).
$("impExportsBtn").addEventListener("click", () => guard(async () => {
  const res = await api("GET", "/exports");
  if (!res.found || !res.rows.length)
    throw new Error("No mychars_export_*.csv found at " + (res.path || "the MQ config dir") +
      " — in game, hit Export All (or /lua run mychars_export per toon), then retry.");
  $("impText").value = JSON.stringify(res.rows, null, 1);
  toast("Loaded " + res.rows.length + " toon(s) from " + res.files.length +
    " in-game export file(s) — previewing…");
  $("impPreviewBtn").click();
}));

// ---------- import: MQ AutoLogin (login.db) ----------
let alData = null;                              // last /autologin payload

$("impAutoBtn").addEventListener("click", () => guard(async () => {
  alData = await api("GET", "/autologin");
  if (!alData.found) {
    throw new Error("login.db not found at " + (alData.db_path || "the MQ config dir") +
      " — set EQFORGE_MQ_CONFIG if MQ lives elsewhere.");
  }
  if (!alData.rows.length) throw new Error("login.db has no visible characters yet.");
  const panel = $("alMapPanel");
  // Suggest the roster account that already holds each MQAcct's toons (match by
  // name+server). No overlap → default to SKIP, never silently "create": a
  // create-by-default once duplicated every account and re-assigned the whole
  // roster onto the copies (2026-07-30 incident).
  // FIRST RUN: with no accounts yet there is nothing to vote against, so every row
  // would suggest nothing and default to "skip" — the user clicks Apply and imports
  // NOTHING, which is how this panel used to dead-end on a fresh install. The
  // 2026-07-30 "never default to create" rule exists to stop DUPLICATING an existing
  // roster; with an empty roster there is nothing to duplicate, so create is both
  // safe and the only useful default.
  const emptyRoster = S.accounts.length === 0;
  const acctAlias = {};
  for (const a of S.accounts) acctAlias[a.id] = a.alias;
  const charAcct = new Map();
  for (const c of S.characters) {
    if (c.account_id != null)
      charAcct.set(c.name.toLowerCase() + "|" + (c.server || "").toLowerCase(), acctAlias[c.account_id]);
  }
  const suggestFor = (key) => {
    const votes = {};
    for (const r of alData.rows) {
      if (r.account !== key) continue;
      const alias = charAcct.get(r.name.toLowerCase() + "|" + (r.server || "").toLowerCase());
      if (alias) votes[alias] = (votes[alias] || 0) + 1;
    }
    let best = null;
    for (const [alias, n] of Object.entries(votes)) if (!best || n > best.n) best = { alias, n };
    return best;                                   // {alias, n} or null
  };
  // Toons on servers the roster doesn't track (Firiona/Vaniki alts on the same EQ
  // accounts) are skipped automatically — say so up front. Empty roster = track everything.
  const rosterServers = new Set(S.characters.map((c) => (c.server || "").toLowerCase()).filter(Boolean));
  const foreign = rosterServers.size
    ? alData.rows.filter((r) => !rosterServers.has((r.server || "").toLowerCase()))
    : [];
  const foreignNote = foreign.length
    ? "<div class='hint'>" + foreign.length + " toon(s) on other servers (" +
      esc([...new Set(foreign.map((r) => r.server))].sort().join(", ")) +
      ") are skipped automatically — this roster tracks " +
      esc([...new Set(S.characters.map((c) => c.server))].sort().join(", ")) + ".</div>"
    : "";
  panel.innerHTML =
    "<h4>Found " + alData.rows.length + " characters on " + alData.accounts.length +
    " EQ accounts. Map each to a roster account:</h4>" +
    (emptyRoster ? "<div class='hint'>Your roster is empty, so each EQ account will be " +
      "created as-is. Rename them later on the Roster tab (or use 🏷 Use login names).</div>" : "") +
    foreignNote +
    alData.accounts.map((a) => {
      const sug = suggestFor(a.key);
      const aliasOpts = S.accounts.map((x) =>
        "<option value='" + esc(x.alias) + "'" + (sug && sug.alias === x.alias ? " selected" : "") + ">" +
        esc(x.alias) + "</option>").join("");
      return "<div class='almap-row' data-key='" + esc(a.key) + "'>" +
      "<span class='key'>" + esc(a.key) + "</span>" +
      "<span class='meta'>" + a.char_count + " toon" + (a.char_count === 1 ? "" : "s") +
      (a.groups.length ? " · groups: " + esc(a.groups.join(", ")) : "") +
      (sug ? " · <b>matches " + esc(sug.alias) + "</b> (" + sug.n + " toon" + (sug.n === 1 ? "" : "s") + ")"
           : emptyRoster ? " · <b>will be created</b>"
           : " · no roster match") + "</span>" +
      "<select>" +
      "<option value='__skip__'" + (sug || emptyRoster ? "" : " selected") + ">skip these toons</option>" +
      aliasOpts +
      "<option value='__create__'" + (emptyRoster ? " selected" : "") + ">➕ create '" +
      esc(a.key) + "'</option>" +
      "</select></div>";
    }).join("") +
    "<div class='almap-actions'><button id='alApplyBtn' class='btn btn-sm' type='button'>Apply mapping → Preview</button>" +
    "<button id='alCancelBtn' class='btn btn-ghost btn-sm' type='button'>Cancel</button>" +
    "<span class='hint'>account NAMES/passwords are never read — only the numeric grouping</span></div>";
  panel.hidden = false;
  $("alApplyBtn").addEventListener("click", () => guard(applyAutologinMapping));
  $("alCancelBtn").addEventListener("click", () => { panel.hidden = true; });
}));

async function applyAutologinMapping() {
  const mapping = {};
  for (const row of $("alMapPanel").querySelectorAll(".almap-row"))
    mapping[row.dataset.key] = row.querySelector("select").value;
  let nextLaunch = S.accounts.length;                // advance per create (all-got-the-same-number bug)
  for (const a of alData.accounts) {                 // create requested accounts first
    if (mapping[a.key] === "__create__") {
      await api("POST", "/accounts", {
        alias: a.key, autologin_group: a.groups.join("/"),
        notes: "from MQ AutoLogin login.db", perks: [],
        launch_order: ++nextLaunch,
      });
      mapping[a.key] = a.key;
    }
  }
  const rosterServers = new Set(S.characters.map((c) => (c.server || "").toLowerCase()).filter(Boolean));
  const rows = alData.rows
    .filter((r) => mapping[r.account] !== "__skip__")
    .filter((r) => !rosterServers.size || rosterServers.has((r.server || "").toLowerCase()))
    .map((r) => ({ ...r, account: mapping[r.account] }));
  if (!rows.length) throw new Error("Everything was skipped — nothing to import.");
  $("impText").value = JSON.stringify(rows, null, 1);
  $("alMapPanel").hidden = true;
  await reload();
  $("impPreviewBtn").click();
}

// ---------- optimizer ----------
const REC_LABELS = { missing_role: "Role gap", account_pairing: "Account pairing",
  next_account: "Next character", missing_unlock: "Unlock gap", attention: "Attention",
  membership: "Membership" };
function renderRecs() {
  $("recList").innerHTML = S.recommendations.length
    ? S.recommendations.map((r) =>
        "<div class='rec " + esc(r.kind) + "'><b>" + esc(REC_LABELS[r.kind] || r.kind) + "</b>" +
        esc(r.message) + "</div>").join("")
    : "<div class='warn ok'>No recommendations — roster looks solid.</div>";
}
$("recRefreshBtn").addEventListener("click", () => guard(reload));

// ---------- boot ----------
guard(async () => {
  renderAcctModeBtn();
  await reload();
  loadComp(null);
  // deep link from the Macro Builder's old Gear Planner tab
  if (location.hash === "#sets") gotoTab("sets");
});

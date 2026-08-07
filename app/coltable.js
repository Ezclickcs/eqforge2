/* coltable.js — drag-to-resize columns for the .grid tables (inventory, sell list,
   vendor, sold). Vanilla, no deps, no build step — same rules as the rest of the app.

   Why: the sell list's Price cell holds an <input>. With the browser's auto table
   layout a long item name ("Blood Soaked Plasmatic Priest Robe") squeezes that column
   until 5-digit prices clip — you type 25000 and read 2500. Grab a header's right edge
   and every column is pinned at its current pixel width (table-layout: fixed); from
   then on a drag only moves the column you grabbed and the table grows into the
   wrapper's horizontal scroll instead of stealing width from its neighbours.

   Widths persist per table id in localStorage ("eqaf-colw-<tableId>").
   Double-click any grip = drop the saved widths, back to auto layout. */
(function () {
  const MIN_W = 44;                                   // px — never let a column vanish
  const storeKey = (id) => `eqaf-colw-${id}`;
  const colKey = (th, i) => th.dataset.col || `c${i}`;  // data-col where it exists, else position

  function loadWidths(id) {
    try {
      const o = JSON.parse(localStorage.getItem(storeKey(id)) || "{}");
      return o && typeof o === "object" ? o : {};
    } catch { return {}; }
  }
  function saveWidths(id, widths) {
    try {
      if (Object.keys(widths).length) localStorage.setItem(storeKey(id), JSON.stringify(widths));
      else localStorage.removeItem(storeKey(id));
    } catch { /* private mode / quota — resizing still works for this session */ }
  }

  function headers(table) {
    const row = table.tHead && table.tHead.rows[0];
    return row ? [...row.cells] : [];
  }

  // One <col> per header, created once. Widths live on the colgroup, not on the cells,
  // so a tbody re-render (refreshAuction wipes innerHTML every keystroke) can't drop them.
  function cols(table) {
    let cg = table.querySelector(":scope > colgroup");
    if (!cg) {
      cg = document.createElement("colgroup");
      headers(table).forEach(() => cg.appendChild(document.createElement("col")));
      table.insertBefore(cg, table.tHead || table.firstChild);
    }
    return [...cg.children];
  }

  function apply(table, widths) {
    const cs = cols(table), ths = headers(table);
    let total = 0, sized = 0;
    cs.forEach((col, i) => {
      const w = ths[i] ? widths[colKey(ths[i], i)] : 0;
      if (w) { col.style.width = w + "px"; total += w; sized++; }
      else col.style.width = "";
    });
    table.classList.toggle("col-sized", sized > 0);
    // Every column pinned → the table is exactly as wide as its columns and the
    // .table-wrap scrolls sideways. Partial → let the auto layout keep filling the pane.
    table.style.width = sized && sized === cs.length ? total + "px" : "";
  }

  // Freeze what's on screen right now, so the first drag doesn't reshuffle everything.
  function pinCurrent(table, widths) {
    headers(table).forEach((th, i) => {
      const k = colKey(th, i);
      if (!widths[k]) widths[k] = Math.max(MIN_W, Math.round(th.getBoundingClientRect().width));
    });
  }

  function attach(table) {
    if (!table.id || table.dataset.colResize) return;
    table.dataset.colResize = "1";
    const widths = loadWidths(table.id);
    apply(table, widths);

    headers(table).forEach((th, i) => {
      const grip = document.createElement("span");
      grip.className = "col-grip";
      grip.title = "Drag to resize this column · double-click to reset all";
      th.appendChild(grip);

      // Headers double as sort buttons — a grab must never sort.
      grip.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); });
      grip.addEventListener("dblclick", (e) => {
        e.stopPropagation(); e.preventDefault();
        for (const k of Object.keys(widths)) delete widths[k];
        apply(table, widths);
        saveWidths(table.id, widths);
      });
      grip.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.stopPropagation(); e.preventDefault();
        pinCurrent(table, widths);
        apply(table, widths);
        const k = colKey(th, i), startX = e.clientX, startW = widths[k];
        document.body.classList.add("col-resizing");
        const move = (ev) => {
          widths[k] = Math.max(MIN_W, Math.round(startW + (ev.clientX - startX)));
          apply(table, widths);
        };
        const up = () => {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          document.body.classList.remove("col-resizing");
          saveWidths(table.id, widths);
        };
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  }

  function reset(table) {
    if (!table || !table.id) return;
    saveWidths(table.id, {});
    apply(table, {});
  }

  function initAll(root) {
    (root || document).querySelectorAll("table.grid").forEach(attach);
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => initAll());
    else initAll();
    window.ColTable = { attach, initAll, reset };
  }
})();

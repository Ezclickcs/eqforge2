# TLP-Auctions API — reference for EQ Forge 2.0

Full spec: [`tlp-auctions-api-v3.0.json`](tlp-auctions-api-v3.0.json) (saved 2026-07-03 from
<https://tlp-auctions.com/swagger/index.html>). This file is the practical cheat sheet.

## Ground rules
- **Base URL:** `https://tlp-auctions.com/api` — the **apex** host. Do **not** use an `api.` subdomain
  (cert SAN mismatch). `araduneauctions.net` is the same service (older domain).
- **CORS:** the API only sends CORS headers for origin `https://wangel.github.io`. A browser on any
  **other** origin (localhost, your own deploy) is **blocked**. `serve.py` proxies `/api` locally;
  a public deploy needs a Cloudflare Worker proxy. Server-side/curl is unaffected.
- **License:** PolyForm **Noncommercial** — personal/community use only, never sell it or build a paid
  product on it. (App is also AGPL from the EQAF fork.)
- **Server:** this app uses `serverName=Frostreaver` (the only TLP with live data). Others: Teek,
  Yelinak, Mischief, Thornblade, Oakwynd.
- **Rate limits:** `429` + `Retry-After`. The single-search `/api/sales` is **60/min per IP** — prefer
  the bulk endpoints and don't poll per item.

## Endpoints this app uses
| Endpoint | Purpose | Notes |
|---|---|---|
| `POST /api/prices/bulk` | **Price check** (PC All) | Krono-normalized, MAD-filtered **median** over full history. ≤10 ids/request, cached **1h**/item. Body `{serverName, itemIds}` → `{kronoRate, items:[{itemId,item,medianPlatPrice,sampleSize,hasData}]}`. |
| `POST /api/sales/bulk` | Recent postings for the WHOLE price check (review flags, supply/demand tags, **recent-market pricing**) | Up to **200 ids/request**, `perItemLimit` ≤20 (app sends `REVIEW_FETCH=20`), cached ~5min. Body `{serverName, itemIds, perItemLimit}` → `{items:[{itemId, item, sales:[SalesLogDto]}]}` — same sale shape as `GET /sales`. |
| `GET /api/sales?searchTerm=&exactMatch=true&serverName=&pageSize=` | Recent Postings modal (on-demand, one item) | WTS+WTB mixed, newest first. `transactionType`: `true`=WTB, `false`=WTS. Price = `platPrice` or `kronoPrice`. **60/min/IP.** |
| `GET /api/krono-prices/{server}/windows` | Live krono→plat rate (header Sync) | 1/2/3/7-day windows; app takes the freshest with data. |

## Endpoints NOT used yet — worth knowing (queued work)
| Endpoint | Why it matters |
|---|---|
| `GET /api/items/catalog?serverName=` | Every item-with-sales + median in **one call** (~4k, cached 1h). Could pre-fill an at-a-glance median column in the inventory list, and replace the 20 `prices/bulk` batches in PC All. |
| `GET /api/items/search?q=&serverName=` | Resolve item name → id (dedups to the id that has sales). |
| `GET /api/items/{itemId}/history/{server}` | Full price-history points → a per-item price chart. |
| `GET /api/prices/pricecheck?serverName=&searchTerm=` | Avg buy/sell + 8 most-recent per side. |

## Key response fields
- **Money:** every sale is in plat OR krono. Effective plat = `platPrice + kronoPrice * kronoRate`.
- `hasData=false` (prices/bulk) → no sales on the server → the app marks the row "— no sales".
- `sampleSize` = number of sales behind the median (high = saturated/liquid market).

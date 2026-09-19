# Wiring a console screen to the control plane

The console was built against an in-memory dataset. Every screen is being moved
onto the live API. This is the shared contract for doing that, so twenty-three
screens end up written the same way.

## What already exists

| Piece | Where | What it gives you |
|---|---|---|
| `API` | `js/api.js` | One method per endpoint: `API.<domain>.<verb>()`. It handles the base URL, credentials, timeouts, error translation, CSV downloads and SSE. **Never construct a URL or a fetch anywhere else.** |
| `Store` | `js/store.js` | `Store.session`, `Store.mutate()`, `Store.on/emit`, `Store.badges`, `Store.refreshBadges({maxAge})`. There is no shared list cache: each table owns its rows through `source`, and a screen that must react to another screen's mutation subscribes to the event that mutation emits. |
| `C.dataTable` | `js/components.js` | Pass `source` and the table fetches from the server: search, sort, filters and paging become query parameters, and it renders its own loading, error-with-retry and empty states |
| `AUTH` / shell | `js/login.js`, `js/app.js` | Sign-in gate, session boot, workspace switch, sidebar badges. Screens can assume `Store.session.user` exists. |

## The five rules

**1. No screen reads `DB`.** `js/data.js` is the old fixture and is deleted at the
end of this migration. A reference to `DB.anything` is a bug, not a fallback.

**2. Tables go through `source`, not `rows`.**

```js
const table = C.dataTable({
  columns, rowId: 'id', itemName: 'agents',
  filters: [
    // `param` is the query parameter the server expects; `key` stays the UI key
    { key: 'source', label: 'Source', param: 'source', options: SOURCES },
  ],
  source: (params) => API.agents.list(params),
  exportSource: (params) => API.agents.export(params),
  autoSelectFirst: true,          // replaces a bare table.selectFirst() call
  onSelect: (row) => inspector(row),
});
```

`autoSelectFirst` matters: in server mode the rows arrive after construction, so
an immediate `table.selectFirst()` would run against an empty table.

**3. KPI rows come from `/summary`, and they render twice.** Paint the card
shells immediately with the `card-loading` class, then fill them when the summary
resolves. A KPI must never show a number the server did not send — no zeros
standing in for "not loaded yet".

```js
const host = C.elem(`<div>${C.kpiRow(SHELLS)}</div>`);   // labels, no values
mount.appendChild(host);
API.agents.summary()
  .then(s => { host.innerHTML = C.kpiRow(cards(s)); })
  .catch(err => { host.innerHTML = screenError(err, () => reload()); });
```

**4. Mutations go through `Store.mutate` and then refresh.** The optimistic toast
comes *after* the server says yes, never before.

```js
await Store.mutate(() => API.policies.activate(row.id), { event: 'policies:changed' });
C.toast('success', 'Policy activated', `${row.name} is now enforcing.`);
table.refresh();
```

On failure show the server's message — it is written for a person:
`C.toast('error', 'Could not activate', err.message)`.

**5. Every async path has three states.** Loading, error (with a retry that
re-runs the same call), and empty. `C.dataTable` does this for tables; for
everything else (charts, feeds, detail panels) use the same shapes:
`.card-loading`, `.screen-error`, `.empty-state`.

## Streaming (Live Runs)

```js
const stream = API.runs.stream({
  params: currentFilters,
  onMessage: (run) => table.prependRow(run),
  onError: () => setLiveState('reconnecting'),
});
SCREENS['live-runs'].cleanup = () => stream.close();   // the router calls this
```

Register `cleanup` on the screen for anything that outlives a render: streams,
intervals, timers, observers. The router already calls it on navigation.

## Permissions

`Store.session.can('operator')` gates a control. A user without the role should
see the control **disabled with a title explaining why**, not a button that
fails when pressed. The server enforces the same rule; the UI is only being
honest about it in advance.

## What "no fabricated data" means here

If the server does not return a field, the cell shows `—`. If a call fails, the
screen says so. Nothing is simulated, no timers invent rows, no random values
fill a chart. The one moving thing in the product is the run stream, and that
carries real ingested runs.

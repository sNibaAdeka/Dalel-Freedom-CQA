# Frontend/backend integration — Дәлел

## Runtime

The integrated app contains `list`, `card`, `audio`, `weights`, `review`, `board`, and `editor`. Each module exports `mount(CQA)`. Plain HTML/CSS/JS ES modules, no build step; HTTP is required. All network access goes through `CQA.api` and cross-module updates through `CQA.bus`.

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000/?mode=live`. The default demo remains available via the explicit mode switch. An unavailable server shows an error; it never falls back to mock writes. Server-side `mock` is labelled as a test evaluator. Demo mutations are in memory; live mutations persist in SQLite.

Host configuration before bootstrap:

```js
window.CQA_CONFIG = {
  mock: false,
  apiBase: '/api',
  modules: ['list', 'card', 'audio', 'weights', 'review', 'board', 'editor'],
  parentOrigin: null
};
```

The host may embed the page as a future Dynamics web resource. For iframe messaging, set `parentOrigin` to the exact origin. Messages validate source window, origin and `source:'dalel-host'`. Commands: `open` and `setRole`. Outgoing `dalel-cqa` events carry IDs/totals, not transcripts. Production Dynamics authentication is not implemented.

## API and state

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest?group_id=...` | Upload records/envelope; without body fetch configured dataset |
| `GET /api/conversations` | Queue, group/language/channel/status filters |
| `GET /api/conversations/{id}?version=N` | Version-specific card with source evals, scores and decisions |
| `GET/POST /api/checklists` | Immutable checklist versions |
| `GET /api/checklists/{a}/diff/{b}` | Changed criteria, re-evaluation plan |
| `POST /api/runs` | `{version,conv_ids,mode,profile,live,force,preview,activate}` |
| `GET /api/runs/{id}` | Progress and immutable result snapshot |
| `GET /api/runs/{id}/export?format=json` | Snapshot export; `csv` also supported |
| `POST /api/appeals` | Criterion, comment, messages, optional pinned `checklist_version` |
| `GET /api/appeals/{id}/dossier` | Validated quotes, arguments for appeal / original evaluation |
| `POST /api/decisions` | Confirm/change, mandatory reason, separate decision record |
| `GET /api/audio/{id}` | Refreshable recording URL and playback offsets |
| `GET /api/health`, `GET/POST /api/config`, `GET /api/metrics` | Provider settings and measured usage |

`AppState` connects `RunManager(Database, ChecklistStore)` to the evaluator's `only_criteria/no_cache` callback. Review API uses `ReviewService`; API adapters serialize Pydantic objects. Dossiers share the core dossier table.

Run submission is one POST. The editor sends `{version,activate:true,conv_ids}`; it does not activate first. `run:started` attaches the board to the existing run. Polling pause stops observation, not execution. Empty explicit selections must not expand to all dialogs. Errors/invalid answers prevent activation; live forces fresh answers. Weight-only edits reuse criterion answers. Wording/type/scale/N/A changes invalidate corresponding criterion hashes; prompt-policy changes invalidate affected prompt versions.

Appeals pin the checklist version and original evaluation snapshot. Subsequent evaluations do not rewrite the appeal's source assessment. A decision on old wording does not override a changed criterion. A changed transcript under an existing ID is rejected to avoid stale evaluations, evidence and decisions.

## UI contract

Detail: `{id,channel,language,group_id,messages,meta,audio_url,evals,score,checklist,appeals,decisions,audit}`. Message IDs are stable; evidence offsets are UTF-16 positions. Any transcript role may support an exact quote; bot/system context is visibly distinguished from operator action. A matched-text badge is not a correctness guarantee.

The deterministic engine is authoritative: team example 85.38 (C1 yes, C2 N/A, C3 N/A, C4=15, C5 yes, C6=7, C7 yes), C6=7 → 10.5 points at weight15; C8=no means no violation. Keep two decimal places; never round 99.99 to 100. N/A removes weight from the denominator. A zero-weight critical flag has no meaningless 0/0 fraction.

Events: `conversations:refresh`, `checklists:updated`, `checklist:saved`, `checklist:activated`, `run:started`, `run:progress`, `run:finished`, `run:completed`, `conv:open`, `conv:loaded`, `criterion:focus`, `audio:seek`, `appeal:start`, `score:updated`, `role:changed`.

Keyboard shortcuts use physical key codes and respect typing inputs. Projector mode hides secondary settings, enlarges content and collapses cost details. Evidence threads follow the selected criterion and message across scrolling. Audio preserves transcript access on missing/expired recordings; it does not invent a playable source.

## Acceptance and boundaries

```bash
python -m pytest tests -q
node --experimental-default-type=module --test tests/frontend_*.test.mjs
git diff --check
```

API tests cover ingest → evaluate → card → appeal → decision → version edit → apply. Additional regressions cover live refresh, selection scope, partial failures, exact formula, profile separation, immutable export and appeal snapshots. The test suite must not skip scenarios merely because runs/review modules are absent.

Browser acceptance uses synthetic data with a real HTTP API and an explicitly identified test evaluator. Check 375/768/1024/1280×720/1440/1920, projector on/off, focus, overflow, actual form submissions, card scroll, evidence thread and console. Private screenshots/reports remain in ignored output folders. A browser pass on fake evaluations is not a real-model accuracy benchmark.

Use `scripts/live_smoke.py` for a separate live-provider check on RU/KZ/EN + call, then DEV. It writes actual provider traces/usage locally and stops at preflight failures. Human blind annotations are still required for quality measurement; Silver agreement is not accuracy.

## Publication

Never stage `.env`, `data/raw`, `data/processed`, `results`, `reports`, `eval/gold`, `web/mocks` or team strategy files. Static files under `web/mocks` are denied by default. `CQA_SERVE_DEMO_FIXTURES=1` is only for audited synthetic fixtures. Role selection is a demonstration control, not authorization. Keep the API bound to localhost until production authentication is added.

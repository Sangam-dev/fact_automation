# FACT Adaptation Roadmap

The project is now an Excel-free desktop-automation foundation. This document
records what still carries an Excel/list heritage, what is missing for FACT
ERP.NG, and the order the missing pieces are expected to arrive in. None of
the items below are implemented yet.

## 1. Remaining Excel (spreadsheet-shape) coupling

These are concepts, not workbook code — no workbook file is read or written.

| Location | What survives | Why it matters for FACT |
| --- | --- | --- |
| `execution/verification/search.py` (`find_appended_row`, `scroll_window`) | "a new record lands below the fold, scroll down to find it, refuse if the whole app scrolls" | Valid for a list view, but only single-scroll-container. Multi-pane FACT grids need a declared scroll target. |
| `ConfirmationConfig.verifier = "row"` | name and docstrings speak of a "row" / record list | Rename to a record/list verifier; behaviour is reusable as-is. |
| `core/state/store.py` (`RunState.pending`, `completed`, `RunRow`, `stopped_row`) | row-number shaped checkpoint and resume state | Replace with transaction ids and a state transition log; keep atomic writes and save-intent protection. |
| `SaveIntent.record_id` | generic id, but currently used with invoice-like values | Fine as a transaction key; needs a defined id source in FACT. |
| `execution/actions/engine.py::verify_save` | compares footprint values on screen before/after save | Reusable evidence rule; FACT may prefer its own confirmation banner or API check. |
| Wizard wording (`fields`, `sample record`) | "sample record" is a one-off proof, not persisted | Fine; ensure FACT values are supplied by the transaction, never stored. |

There are no remaining workbook paths, sheet names, column indexes, pandas,
openpyxl or `automation.excel` imports.

## 2. Missing FACT capabilities

- Transaction model: id, type, payload, status, owner, retry count.
- FACT-specific navigation: login/session handling, module switching, search.
- Multi-window / multi-dialog flows (open record, confirm dialogs, popups).
- Business validation of values before typing (required fields, formats).
- Post-save FACT verification (record number, status transition, list refresh).
- Durable queue for transactions and per-transaction artifacts.
- Webhook intake (transaction enqueue + idempotency).
- Audit trail that ties screenshots and AI calls to one transaction.

## 3. Required multi-window support

The current runner attaches to a single window (`core.config.AppConfig` has one
matcher) and re-inspects that window after every click.

Needed:

1. A window role registry (main, dialog, popup, report) instead of one matcher.
2. Step targets that name a role, so a click can move execution to a new
   window and later return.
3. Reinspection per role, with the active role tracked in run state.
4. A rule for unexpected windows: wait/retry, dismiss, or stop for a human.
5. Evidence capture per window, not just the main one.

`desktop.windows.window` and `desktop.uia.uia` are already window-agnostic and
can serve any number of windows; the coupling is in config and the runner.

## 4. Required state-transition engine

Current state (`core.state.store`) is a checkpoint: pending/completed records,
a stopped flag, a save intent. It is enough to resume a linear run, not to
represent FACT transactions.

Needed:

- An explicit transition table per transaction type (e.g. `new → filling →
  saving → verifying → posted`, plus `needs_review`, `failed`, `abandoned`).
- Every transition persisted atomically with the evidence that justified it.
- Resume that validates the window/layout fingerprint and re-derives the
  state from the transition log rather than trusting a counter.
- Idempotency: a transaction past `saving` must never be saved twice; this
  rule already exists at run level and must move to the transaction level.
- Unknown events (unexpected dialog, validation error, session expiry) recorded
  as first-class transitions with a human-review outcome.

## 5. Required workflow graph

Today a workflow is a flat ordered list of `click`/`fill` steps
(`WorkflowConfig.steps`) for one screen. FACT needs branching.

Needed:

- A graph: nodes (screens/actions) and edges guarded by observed conditions
  (control present, text matched, record found).
- Sub-workflows reusable across transaction types (login, open module, search).
- Declarative loops bounded by an explicit counter (list pages, line items).
- Per-node retry policy and per-node evidence requirements.
- A validator that rejects unreachable nodes and missing failure edges.

The executor's per-step guarantees (reinspect after click, UIA-first, read-back,
pause checks) should be reused as the node execution primitive rather than
rewritten.

## 6. Unknown-event registration

Unknown events currently surface as `problems[]`/`warnings[]` on a plan result,
or as a stop. FACT needs them registered, not just printed.

Needed:

- An event schema: kind, window role, screenshot, controls snapshot, AI call id,
  transaction id, step index, severity.
- Registration on every non-happy path (control missing, read-back mismatch,
  save unconfirmed, unexpected window, app scroll unsupported).
- A review queue with actions (retry, correct plan, abandon, mark handled) that
  feeds corrections back into the workflow graph.
- Deduplication so the same unknown event on the same transaction is recorded
  once, with an occurrence count.

## 7. Suggested order

1. Transaction model + state-transition log (keep the current store's atomic
   write and save-intent guards).
2. Multi-window role registry and runner support.
3. Workflow graph + validator, reusing the existing executor per node.
4. Unknown-event schema and review queue.
5. FACT business workflows (login, module navigation, record entry).
6. Webhook intake and durable queue once the transaction engine is stable.

Do not start the webhook or queue before steps 1–4 exist; otherwise retries and
idempotency will be defined twice, in two places that disagree.

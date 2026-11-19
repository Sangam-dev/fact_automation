# Refactoring Report

The refactor worked on the files already present in `fact_automation`; the
source project was not re-copied and was not modified.

## 1. Relocated modules

The `automation/` package was split into logical top-level packages and all
imports rewritten to absolute ones.

| New location | From | Notes |
| --- | --- | --- |
| `ai/providers/gemini.py` | `automation/ai/client.py` | Gemini client, retries, call budget/log |
| `ai/planners/grounding.py` | `automation/ai/grounding.py` | generic field mapping |
| `ai/planners/step_planner.py` | `automation/ai/step_planner.py` | declared field keys, no headers |
| `ai/validators/verification.py`, `models.py` | `automation/ai/verification.py`, `automation/models/verification.py` | split response schemas from checks |
| `ai/prompts/loader.py` | `automation/ai/prompts.py` | template loading/rendering |
| `wizard/wizard.py`, `wizard/chat_ui.py` | `automation/agent/wizard.py`, `chat_ui.py` | Excel-free workflow builder |
| `desktop/windows/window.py`, `points.py`, `topmost.py`, `coordinates.py` | `automation/desktop/*` | unchanged behaviour |
| `desktop/uia/uia.py` | `automation/desktop/uia.py` | unchanged behaviour |
| `desktop/screenshots/capture.py`, `images.py` | `automation/desktop/capture.py`, `automation/utils/images.py` | relocated utility |
| `desktop/input/input.py`, `scroll.py` | `automation/desktop/input.py`, `scroll.py` | unchanged behaviour |
| `desktop/types.py` | `automation/models/screen.py` | shared window/UIA types |
| `execution/actions/models.py`, `engine.py` | `automation/models/actions.py`, `automation/agent/form_steps.py` | step schemas split from engine |
| `execution/workflows/runner.py` | `automation/agent/row_runner.py` | single-record runner + `save_and_verify` |
| `execution/verification/controls.py`, `evidence.py`, `confirmation.py`, `search.py` | `automation/agent/form_steps.py`, `row_runner.py` helpers | read-back, save evidence, list search |
| `core/config.py` | `automation/config.py` | Excel-free models |
| `core/state/store.py`, `run_lock.py` | `automation/agent/state_store.py`, `run_lock.py` | generic run state |
| `core/safety/*` | `automation/safety/*` | unchanged behaviour |
| `core/logging/setup.py` | `automation/logging_setup.py` | log path fixed for new depth |
| `core/runtime/errors.py`, `pause_guard.py` | `automation/agent/row_runner.py` errors, `pause_guard.py` | shared runtime |
| `cli.py` | `automation/cli.py` | commands rebuilt for the new pipeline |
| `main.py` | `main.py` | unchanged entrypoint shape |

## 2. Retained without behaviour change

- UIA inspection/hierarchy, window attach/lookup/topmost, coordinate mapping.
- Screenshot capture, scaling, cropping, annotation, evidence saving.
- Mouse/keyboard/clipboard/wheel input, including foreground checks.
- Ordered execution: reinspect after every click, UIA-first with coordinate
  fallback, fill read-back with one delayed retry, optional-step skipping.
- Save handling: save-button resolution, screenshot before/after, local
  ("row") verification, AI ("text") verification, "none" mode, list scrolling
  to find an appended record, refusal when the app scrolls as a whole.
- Pause guard (including re-clicking the field after a pause), emergency stop,
  input lock, overlay, watchdog, hotkeys, run locks.
- State persistence with atomic writes, checkpoint bookkeeping, save-intent
  recovery checks.
- AI provider retries, call budget, AI call log, prompt rendering, redaction.

## 3. Removed

| Removed | Reason |
| --- | --- |
| `automation/excel/reader.py`, `writer.py` | workbook I/O; values now enter per run |
| `automation/workspace.py` | workbook paths/sheet discovery |
| `automation/agent/orchestrator.py` | workbook queue driver |
| `automation/agent/pending_status.py` | per-row pending/status bookkeeping |
| `automation/agent/setup_flow.py` | sheet/column discovery, wrote `agent.json` |
| `automation/agent/cleanup.py` | workbook-specific cleanup |
| `automation/ai/mapping.py`, `automation/models/mapping.py` | column mapping vs workbook headers |
| `automation/models/state.py` | workbook-row state |
| `automation/utils/json_utils.py`, `retry.py` | only used by removed row code |
| `ExcelConfig`, `FormConfig` | replaced by `WorkflowConfig` + `FieldConfig` |
| `config/examples/agent*.json` | Excel configs; replaced by `workflow.example.json` |
| `config/prompts/mapping.md` | no code path uses it |
| pandas/openpyxl dependencies | no workbook code remains |
| 12 test modules for deleted code | tested removed behaviour only |

`FillStep.column` → `key`, `PlannedFill.column` → `key`, `StepPlan.columns` →
`keys`, `FieldConfig.column` → `name`, `AgentConfig.form` → `workflow`,
`MappingEntry` → `FieldSpec`, `GroundingResult.column_mapping` →
`field_mapping`, `footprint_columns` → `footprint_values`, `SaveIntent.invoice_id`
→ `record_id`.

## 4. Modified behaviour (deliberate, Excel-removal)

- **Config**: `AgentConfig` no longer accepts an `excel` block; it accepts
  `fields` (name/field/type) and an optional `workflow`. Unknown keys remain
  forbidden.
- **Business data**: no values in config. `run_workflow` takes
  `values: dict[str, str]`; the CLI reads `--values file.json`.
- **Wizard**: asks for one window, the field names, a sample record (memory
  only), then plans steps with AI; writes `config/generated/workflow.json`.
  Field names are separated from sample values, so workflow design and
  business data never mix.
- **Runner**: `run_workflow` performs one record (attach, plan, save, verify,
  screenshot) and returns an `ExecutionOutcome`; no row loop, writer callbacks
  or status column.
- **Save verification**: `verify_save` is a small pure-ish function taking the
  pre-save inspection and values; `save_and_verify` wraps it for the runner.
- **Grounding/planner**: prompts and validation speak of "data fields"/"keys";
  the grounding result exposes `field_mapping`/`unmapped_keys`.
- **State**: `RunState` keeps config path, window identity, layout fingerprint,
  pending/completed records, stopped flag and save intent — no workbook paths,
  mapping approval or per-row invoice id.
- **Logging**: logger renamed `fact`, default directory corrected to
  `<project>/data/logs`.

## 5. Verification

Executed in this environment (Windows, venv Python 3.12.2):

| Check | Result |
| --- | --- |
| `python -m pytest tests` (unit + integration) | **282 passed** |
| Import sweep of every module under all five packages + `cli` + `main` | 53 modules, 0 failures |
| `python main.py --check` | all checks OK (deps, `.env`, key, config, data dirs) |
| `python main.py --help` / `--version` | OK (2.0.0) |
| Grep for `automation.*`, `exel_automaiton`, `openpyxl`, `pandas`, `.xlsx` in code/config | no hits outside docs |
| Live desktop behaviour (real FACT ERP.NG window) | **not tested** — needs a live app |
| AI planning against a real window | **not tested** live; client/planner covered by offline tests only |

Passing tests are offline unit tests plus the desktop integration fixtures that
skip when no form window is available. Import success alone was not counted as
functional verification.

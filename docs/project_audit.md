# Project Audit

Scope: the destination project `fact_automation` as it stood **before** this
refactor (the first, broad extraction from `exel_automaiton`) and what it was
classified as. The original source project was only read, never modified.

## 1. Original source project (`exel_automaiton`)

A single-package Excel desktop agent. The package `automation/` was the whole
application; approximate contents:

| Area | Modules |
| --- | --- |
| Entry points | `main.py`, `automation/cli.py` |
| Wizard | `automation/agent/wizard.py`, `setup_flow.py`, `calibrate.py` |
| Execution | `automation/agent/form_steps.py`, `row_runner.py`, `orchestrator.py`, `pending_status.py`, `cleanup.py` |
| Excel coupling | `automation/excel/reader.py`, `writer.py`, `automation/workspace.py` |
| AI | `automation/ai/client.py` (Gemini), `grounding.py`, `mapping.py`, `step_planner.py`, `verification.py`, `prompts.py` |
| Desktop | `automation/desktop/*`: window, capture, images, UIA, input, points, scroll, topmost |
| Safety | `automation/safety/*`: controller, hotkeys, input_lock, overlay, watchdog |
| State | `automation/agent/state_store.py`, `run_lock.py`, `pause_guard.py` |
| Models/utils | `automation/models/*`, `automation/utils/*` |
| Config/prompts/tests | `config/`, `tests/`, `docs/` |

The Excel application's shape: read rows from a workbook, map columns to form
fields, fill one row at a time, save, verify the row appeared, write a status
column back to the workbook, resume from a saved row index.

## 2. Destination project before this refactor

The destination contained the same `automation/` package tree, mechanically
copied and import-rewritten. Functionally it was still an Excel application:
its entry points demanded a workbook, `AgentConfig` required an `excel` block,
the wizard asked for input/output workbooks and sheet names, the runner looped
over workbook rows, and `SaveIntent` tracked an invoice/row id.

## 3. Module classification

### A. Reusable automation infrastructure (retained)

- `agent/wizard.py`, `agent/chat_ui.py` — interactive GUI workflow builder.
- `ai/client.py`, `ai/grounding.py`, `ai/step_planner.py`, `ai/verification.py`,
  `ai/prompts.py` — AI providers, GUI grounding, one-instruction planning,
  AI validation, prompt templates.
- `desktop/window.py`, `points.py`, `topmost.py` — window detection/management,
  coordinate mapping.
- `desktop/capture.py`, `images.py` — screenshot capture and processing.
- `desktop/uia.py` — UIA inspection and hierarchy/metadata extraction.
- `desktop/input.py`, `scroll.py` — mouse, keyboard, clipboard, wheel control.
- `agent/form_steps.py` — ordered multistep engine: resolution, reinspection,
  click/fill, read-back, save click, screenshot evidence, pause/stop checks.
- `agent/row_runner.py` — the single-record run/save flow and retry rules
  (record loop stripped later).
- `agent/state_store.py`, `agent/run_lock.py`, `agent/pause_guard.py` — state
  persistence, atomic writes, checkpoints, run locks, pause/resume.
- `safety/*` — e-stop controller, hotkeys, input lock, overlay, watchdog.
- `logging_setup.py` — rotating logs and secret redaction.
- `models/actions.py`, `models/verification.py`, `utils/images.py`,
  `utils/json_utils.py`, `utils/retry.py` — shared schemas/utilities.
- `config/prompts/*` — prompt templates (runtime requirement).

### B. Excel-specific (removed)

- `automation/excel/` — `reader.py`, `writer.py` (workbook I/O, row status
  writes, workbook reporting).
- `automation/workspace.py` — workbook paths/sheet discovery.
- `automation/agent/row_runner.py` batch/row loop (`run_form_rows`,
  pending-row queueing) — the per-record part was kept and renamed.
- `automation/agent/orchestrator.py` — drives the workbook queue end to end.
- `automation/agent/pending_status.py` — pending/status bookkeeping per row.
- `automation/agent/setup_flow.py` — discovers workbook sheets/columns and
  writes `agent.json`.
- `automation/agent/cleanup.py` — workbook-specific run cleanup.
- `automation/ai/mapping.py`, `automation/models/mapping.py` — column mapping
  against workbook headers.
- `automation/models/state.py` — workbook-row state model.
- `automation/utils/json_utils.py`, `automation/utils/retry.py` — only used by
  the removed row/orchestrator code (re-verified before deletion).
- `ExcelConfig`, `FormConfig`, `AgentConfig.excel`, `FillStep.column`,
  `MappingEntry`, `GroundingResult.column_mapping`, `SaveIntent.invoice_id`,
  `RunState.{mapping,input,output}_path`, `pending_status` writers.
- `config/examples/agent*.json`, prompts that described workbook headers,
  `config/prompts/mapping.md`.

### C. Mixed modules (split, not deleted)

| Module | Excel part | Reusable part kept |
| --- | --- | --- |
| `agent/form_steps.py` | row-typed `run_form_row(s)`, column value typing, status writer | `execute_form_steps`, `verify_save`, `clear_open_form`, `form_is_open`, `save_screenshot` |
| `agent/row_runner.py` | workbook row iteration, pending rows, writer callbacks | single-record `run_workflow` (now `execution/workflows/runner.py`) + `save_and_verify` |
| `agent/wizard.py` | workbook/sheet/mapping questions, sample row from sheet 2 | window → fields → sample record → AI steps → config; values asked at run time |
| `config.py` | `ExcelConfig`, column names, `form` block | `AppConfig`, `FieldConfig.name`, `ActionsConfig`, `WorkflowConfig` |
| `ai/step_planner.py` | "column" from workbook headers | "key" from declared field names |
| `ai/grounding.py` | `column_mapping` | `field_mapping`, `unmapped_keys` |
| `ai/verification.py` | column checks | `FieldSpec`/`FieldCheck.key` checks |
| `state_store.py` | workbook paths, mapping approval, row counters | run state, checkpoints, save intent, atomic writes |
| `pause_guard.py`, `run_lock.py` | row wording | unchanged behaviour, generic wording |
| `models/actions.py` | `FillStep.column` | `ClickStep`/`FillStep` with `key` |
| `prompts/*.md` | "headers", "Excel trail" | field names/keys, generic save evidence |

## 4. Destination after this refactor

Top-level packages `ai`, `wizard`, `desktop`, `execution`, `core` plus
`config/`, `tests/`, `docs/`. No module imports `automation.*`, reads or writes a workbook/sheet, or
requires pandas/openpyxl. Business values enter only at
run time (`values: dict[str, str]` or `--values file.json`).

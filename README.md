# FACT Automation — Reusable Automation Foundation

Windows desktop automation for **FACT ERP.NG**. The repository contains reusable
GUI automation, planning, safety, and state infrastructure, plus a registered
sales-order automation and an authenticated local API/queue that dispatches it
through a worker in the logged-in Windows session. It does not read or write
Excel workbooks.

## Layout

```text
fact_automation/
├── main.py                  # entrypoint (python main.py --help)
├── cli.py                   # command dispatch for inspection, forms, automations, wizard, checks
├── requirements.txt         # runtime dependencies
├── pyproject.toml           # package metadata and development dependencies
├── api/                     # authenticated sales-order API, SQLite queue, desktop worker
├── ai/
│   ├── providers/           # Gemini client, retries, cost guards, AI call log
│   ├── planners/            # grounding and one-instruction -> step plan
│   ├── validators/          # pre/post-save AI response validation
│   └── prompts/             # prompt rendering and candidate formatting
├── wizard/
│   ├── wizard.py            # window -> fields -> sample record -> steps -> config
│   └── chat_ui.py           # floating chat window that hosts the wizard
├── desktop/
│   ├── windows/             # window lookup, attach, points, topmost, coordinates
│   ├── uia/                 # UI Automation inspection and control metadata
│   ├── screenshots/         # capture, scaling, cropping, annotation
│   └── input/               # mouse/keyboard/clipboard control, scrolling
├── execution/
│   ├── actions/             # step models and the ordered fill/click engine
│   ├── forms/               # form maps and deterministic sale-order filling
│   ├── scripts/             # registered automation step scripts
│   ├── workflows/           # workflow and registered-automation runners
│   └── verification/        # target resolution, read-back, save evidence, list search
├── core/
│   ├── automation/          # automation registry, validation, resume, segments
│   ├── config.py            # Pydantic config models
│   ├── state/               # run state, atomic writes, run lock
│   ├── safety/              # hotkeys, input lock, overlay, watchdog, controller
│   ├── logging/             # rotating logs + secret redaction
│   └── runtime/             # shared errors and pause guard
├── config/
│   ├── automations/         # registered automation definitions and launchers
│   ├── examples/            # example workflow JSON and sample values JSON
│   ├── forms/               # form maps and sample payloads
│   ├── prompts/             # AI prompt templates (runtime requirement)
│   ├── segments/            # designed automation segments
│   └── states/              # known FACT screen definitions
├── data/                    # runtime output (logs, state, screenshots, AI logs)
├── tests/                   # unit + desktop integration tests
├── docs/                    # architecture notes, historical audits, and roadmap
```

The API and desktop worker have their own setup and safety notes in
[`api/README.md`](api/README.md).

Modules are grouped into top-level packages (`api`, `ai`, `wizard`, `desktop`,
`execution`, `core`) with absolute imports, so no module depends on the old
`automation/` package or on the source project.

## Install

```bash
python -m venv myvenv
myvenv/Scripts/python.exe -m pip install -r requirements.txt
myvenv/Scripts/python.exe -m pip install pytest
# Set GEMINI_API_KEY in the environment or in a local .env file.
```

## Run

```bash
myvenv/Scripts/python.exe main.py --check        # environment report
myvenv/Scripts/python.exe main.py --agent        # interactive wizard
myvenv/Scripts/python.exe main.py form run --values config/examples/values.example.json
```

Business values are supplied per run as a JSON object of `field name -> value`.
The saved workflow holds field names, steps and confirmation rules only, never
business data.

Every path (config, prompts, data) resolves relative to this project root via
`core.config.project_root()`, so the project runs independently of the source
project.

## Tests

```bash
myvenv/Scripts/python.exe -m pytest tests/unit        # offline, no desktop needed
myvenv/Scripts/python.exe -m pytest tests/integration # needs a live Windows form,
                                                      # skips itself otherwise
```

## Documentation

- `docs/project_audit.md` — what was in the destination, classified
- `docs/refactoring_report.md` — retained/removed/modified/relocated modules
- `docs/dependency_map.md` — dependency chains and entry points
- `docs/fact_adaptation_roadmap.md` — adaptation notes and planned work

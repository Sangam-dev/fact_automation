# FACT Automation — Reusable Automation Foundation

Windows desktop-automation foundation for **FACT ERP.NG**, refactored from the
former Excel desktop agent. It now contains only reusable infrastructure: the
interactive wizard, AI GUI understanding and step planning, screenshot and UIA
inspection, the multistep execution engine, run-state persistence/resume, and
the safety/locking layer. There is no Excel reader/writer, row pipeline or
workbook state left in the codebase.

No FACT-specific workflows, state engine, webhook receiver or transaction queue
exist yet; see `docs/fact_adaptation_roadmap.md` for that work.

## Layout

```text
fact_automation/
├── main.py                  # entrypoint (python main.py --help)
├── cli.py                   # command dispatch: inspect, ground, safety, form, wizard, check
├── requirements.txt
├── pyproject.toml
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
│   ├── workflows/           # single-record run: attach, execute, save, verify
│   └── verification/        # target resolution, read-back, save evidence, list search
├── core/
│   ├── config.py            # pydantic config models (no workbook/row concepts)
│   ├── state/               # run state, atomic writes, run lock
│   ├── safety/              # hotkeys, input lock, overlay, watchdog, controller
│   ├── logging/             # rotating logs + secret redaction
│   └── runtime/             # shared errors and pause guard
├── config/
│   ├── prompts/             # AI prompt templates (runtime requirement)
│   ├── examples/            # example workflow JSON and sample values JSON
│   ├── generated/           # wizard output (gitignored)
│   └── schemas/             # where the pydantic schemas live (see its README)
├── data/                    # runtime output (logs, state, screenshots, AI logs)
├── tests/                   # unit + desktop integration tests
└── docs/                    # audit, refactoring report, dependency map, roadmap
```

Modules are grouped into top-level packages (`ai`, `wizard`, `desktop`,
`execution`, `core`) with absolute imports, so no module depends on the old
`automation/` package or on the source project.

## Install

```bash
python -m venv myvenv
myvenv/Scripts/python.exe -m pip install -r requirements.txt
myvenv/Scripts/python.exe -m pip install pytest
```

## Run

```bash
cp .env.example .env        # add GEMINI_API_KEY
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
- `docs/fact_adaptation_roadmap.md` — remaining coupling and FACT work ahead

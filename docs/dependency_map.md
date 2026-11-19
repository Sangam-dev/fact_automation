# Dependency Map

All imports are absolute and rooted at the project. Dependency direction is
one-way: `desktop` and `core` at the bottom, `ai`/`execution` in the middle,
`wizard`/`cli`/`main` on top. No module imports the old `automation` package.

```text
main.py
 └── cli.py
      ├── core.config, core.logging.setup, core.safety.{controller,hotkeys,overlay}, core.state.run_lock
      ├── desktop.screenshots.{capture,images}, desktop.uia.uia, desktop.windows.window
      ├── ai.providers.gemini, ai.planners.grounding
      ├── execution.verification.confirmation, execution.workflows.runner
      └── wizard.chat_ui, wizard.wizard
```

## Desktop automation

```text
desktop.types                     (no project imports)
desktop.windows.window            -> core.config, desktop.types
desktop.windows.points            -> desktop.types
desktop.windows.topmost           (no project imports)
desktop.windows.coordinates       -> desktop.screenshots.capture, desktop.types
desktop.screenshots.capture       -> desktop.types, desktop.windows.window
desktop.screenshots.images        -> desktop.types
desktop.uia.uia                   -> desktop.types, desktop.windows.window
desktop.input.input               -> desktop.windows.points, desktop.windows.window
desktop.input.scroll              -> desktop.input.input, desktop.types,
                                     desktop.windows.points, desktop.windows.window
```

## AI planner

```text
ai.providers.gemini               -> core.config, core.logging.setup
ai.prompts.loader                 -> core.config, desktop.types
ai.planners.grounding             -> ai.prompts.loader, ai.providers.gemini, core.config,
                                     desktop.screenshots.{capture,images}, desktop.types,
                                     desktop.windows.coordinates
ai.planners.step_planner          -> ai.prompts.loader, ai.providers.gemini, core.config,
                                     desktop.screenshots.{capture,images}, desktop.types
ai.validators.models              (no project imports)
ai.validators.verification        -> ai.prompts.loader, ai.providers.gemini,
                                     ai.validators.models, core.config,
                                     desktop.screenshots.{capture,images}
```

## Execution engine

```text
execution.actions.models          (no project imports)
execution.verification.confirmation -> ai.prompts.loader, ai.providers.gemini, core.config
execution.verification.controls     -> desktop.types, desktop.uia.uia, desktop.windows.coordinates
execution.verification.evidence     -> desktop.types, desktop.uia.uia,
                                       execution.verification.{confirmation,controls}
execution.verification.search       -> desktop.input.scroll, desktop.types, desktop.uia.uia,
                                       execution.verification.{confirmation,evidence}
execution.actions.engine            -> core.config, core.runtime.{errors,pause_guard},
                                       core.safety.controller, desktop.input.input,
                                       desktop.screenshots.capture, desktop.types,
                                       desktop.uia.uia, desktop.windows.window,
                                       execution.actions.models,
                                       execution.verification.{confirmation,controls,evidence,search}
execution.workflows.runner          -> core.config, core.runtime.{errors,pause_guard},
                                       core.safety.controller, desktop.input.input,
                                       desktop.screenshots.capture, desktop.types,
                                       desktop.uia.uia, desktop.windows.window,
                                       execution.actions.engine,
                                       execution.verification.{confirmation,controls,evidence,search}
```

## Wizard

```text
wizard.wizard   -> ai.planners.step_planner, ai.providers.gemini, core.config,
                   desktop.screenshots.capture, desktop.uia.uia, desktop.windows.window,
                   execution.actions.{engine,models}, execution.verification.{controls,evidence},
                   execution.workflows.runner
wizard.chat_ui  -> desktop.windows.topmost, wizard.wizard
```

## State and safety

```text
core.config                     -> execution.actions.models
core.state.store                -> core.config
core.state.run_lock             -> core.config
core.safety.controller          -> core.config
core.safety.hotkeys             -> core.config
core.safety.input_lock          -> desktop.input.input
core.safety.overlay             -> desktop.windows.topmost
core.safety.watchdog            (no project imports)
core.runtime.errors             (no project imports)
core.runtime.pause_guard        -> core.runtime.errors, core.safety.controller,
                                   desktop.types, desktop.windows.window
core.logging.setup              (no project imports)
```

## External dependencies

`pydantic` (config/schemas), `python-dotenv` (`.env`), `Pillow` (images),
`google-genai` (Gemini), `pywin32` (`win32gui`/`win32api` capture and input),
`comtypes` + `pywinauto` (UIA), `pyperclip` (paste), `pytest` (tests only).
No pandas, openpyxl or other spreadsheet library is required.

## Entry points

- `main.py` — startup checks and dispatch to `cli.main`.
- `cli.py` — `check`, `inspect`, `ground`, `safety`, `form run`, `wizard`, plus
  `--agent` / `--agent-ui` compatibility flags.
- `wizard/chat_ui.py` — floating window that runs the wizard and streams output.

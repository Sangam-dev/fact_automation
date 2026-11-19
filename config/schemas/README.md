# Schemas

All configuration and AI-response schemas are pydantic models validated at load
time; there are no standalone JSON-schema files.

- `core/config.py` — `AgentConfig`, `AppConfig`, `FieldConfig`, `ActionsConfig`,
  `WorkflowConfig`, `GeminiConfig`, `ConfirmationConfig`, `ScrollConfig`,
  `SafetyConfig`, `NormalizedRegion`
- `execution/forms/form_map.py` — `FormMap`, `FieldSpec`, `GridSpec`,
  `GridColumnSpec`, `ActionsSpec`, `Resolved`
- `execution/actions/models.py` — `ClickStep`, `FillStep`, `FormStep`
- `ai/validators/models.py` — `FieldSpec`, `FieldCheck`, `VerificationError`,
  `PreSaveResponse`, `PostSaveResponse`, `PreSaveVerdict`, `PostSaveVerdict`
- `ai/planners/step_planner.py` — `StepPlan`, `PlannedFill`
- `ai/planners/grounding.py` — `GroundingResult`, `GroundedElement`,
  `FieldMapping`, `GroundingResponse`
- `core/state/store.py` — `RunState`, `RunRow`, `SaveIntent`

`config/examples/workflow.example.json` is the canonical config example and is
validated by `python main.py --check`. Machine-readable JSON schemas can be
generated from these models with `Model.model_json_schema()` when a consumer
needs them.

"""Saved automations: the authored plan, where it lives, and how a run resumes."""

from __future__ import annotations

from core.automation.model import (
    AUTOMATION_SCHEMA,
    RESUME_POLICIES,
    Automation,
    AutomationError,
    AutomationStep,
    Checkpoint,
    SaveSpec,
)
from core.automation.progress import (
    ProgressError,
    ProgressState,
    clear_progress,
    digest_values,
    load_progress,
    new_progress,
    progress_path,
    save_progress,
)
from core.automation.registry import (
    AutomationNameError,
    AutomationNotFound,
    automation_path,
    automations_root,
    exists,
    list_automations,
    load,
    save,
    validate_name,
)
from core.automation.resume import ResumePlan, find_start_index, plan_resume

__all__ = [
    "AUTOMATION_SCHEMA",
    "RESUME_POLICIES",
    "Automation",
    "AutomationError",
    "AutomationNameError",
    "AutomationNotFound",
    "AutomationStep",
    "Checkpoint",
    "ProgressError",
    "ProgressState",
    "ResumePlan",
    "SaveSpec",
    "automation_path",
    "automations_root",
    "clear_progress",
    "digest_values",
    "exists",
    "find_start_index",
    "list_automations",
    "load",
    "load_progress",
    "new_progress",
    "plan_resume",
    "progress_path",
    "save",
    "save_progress",
    "validate_name",
]

"""Utilities for resolving central output paths for multi-stage pipelines.

This module is intentionally small and dependency-free so it can be used by
prompter/CLIs without importing heavy subsystems.

Supported template variables:
- {date}: YYYY-MM-DD (UTC)
- {pipeline}: logical pipeline name (defaults to project_id)
- {agent}: normalized agent type (programmer|researcher|writer|...)
- {task_id}: work item id

Templates are generally interpreted relative to the control repo root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


class OutputPathTemplateError(ValueError):
    pass


def utc_date_str(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def render_output_path_template(template: str, vars: Mapping[str, str]) -> str:
    """Render an output path template.

    Raises OutputPathTemplateError if required variables are missing.
    """

    try:
        rendered = template.format(**vars)
    except KeyError as e:
        raise OutputPathTemplateError(f"Missing template variable: {e.args[0]}") from e

    # normalize separators but keep relative/absolute semantics.
    return str(Path(rendered))


@dataclass(frozen=True)
class OutputPaths:
    """Resolved input/output paths for a work item."""

    output_path: Path | None
    input_path: Path | None
    template_vars: dict[str, str]


def resolve_paths(
    *,
    control_repo_path: Path,
    item: Mapping[str, object],
    project_id: str,
    agent: str,
    task_id: str,
    default_output_template: str | None = None,
) -> OutputPaths:
    """Resolve output/input paths for a work item.

    Conventions:
    - item['outputPath'] wins (string; relative to control repo unless absolute)
    - else item['outputPathTemplate'] if provided
    - else default_output_template if provided
    - input can be specified via item['inputPath'] or item['inputPathTemplate']

    Returned paths are absolute (under control repo) unless the user provides an
    absolute path explicitly.
    """

    pipeline = str(item.get("pipeline") or project_id)
    date = str(item.get("date") or utc_date_str())

    template_vars = {
        "date": date,
        "pipeline": pipeline,
        "agent": agent,
        "task_id": task_id,
    }

    def _to_abs(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (control_repo_path / path)

    out: Path | None = None
    output_path = item.get("outputPath")
    if isinstance(output_path, str) and output_path.strip():
        out = _to_abs(output_path.strip())
    else:
        tpl = item.get("outputPathTemplate")
        if isinstance(tpl, str) and tpl.strip():
            out = _to_abs(render_output_path_template(tpl.strip(), template_vars))
        elif default_output_template:
            out = _to_abs(render_output_path_template(default_output_template, template_vars))

    inp: Path | None = None
    input_path = item.get("inputPath")
    if isinstance(input_path, str) and input_path.strip():
        inp = _to_abs(input_path.strip())
    else:
        tpl = item.get("inputPathTemplate")
        if isinstance(tpl, str) and tpl.strip():
            inp = _to_abs(render_output_path_template(tpl.strip(), template_vars))

    return OutputPaths(output_path=out, input_path=inp, template_vars=template_vars)

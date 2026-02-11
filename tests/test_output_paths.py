from pathlib import Path

import pytest

from orchestrator.utils.output_paths import (
    OutputPathTemplateError,
    render_output_path_template,
    resolve_paths,
)


def test_render_output_path_template_ok():
    tpl = "state/research/{pipeline}/{date}.md"
    s = render_output_path_template(
        tpl,
        {
            "pipeline": "openclaw-usage",
            "date": "2026-02-10",
            "agent": "researcher",
            "task_id": "t123",
        },
    )
    assert s == str(Path("state/research/openclaw-usage/2026-02-10.md"))


def test_render_output_path_template_missing_var():
    with pytest.raises(OutputPathTemplateError):
        render_output_path_template("x/{missing}.md", {"date": "2026-02-10"})


def test_resolve_paths_relative_under_control_repo(tmp_path):
    control = tmp_path / "lobs-control"
    item = {"outputPathTemplate": "state/reports/pending/{pipeline}-{date}.md", "pipeline": "market-trends", "date": "2026-02-10"}

    paths = resolve_paths(
        control_repo_path=control,
        item=item,
        project_id="irrelevant",
        agent="writer",
        task_id="abc",
    )

    assert paths.output_path == control / "state" / "reports" / "pending" / "market-trends-2026-02-10.md"


def test_resolve_paths_outputPath_wins(tmp_path):
    control = tmp_path / "lobs-control"
    item = {"outputPath": "state/research/x.md", "outputPathTemplate": "state/research/y.md"}

    paths = resolve_paths(
        control_repo_path=control,
        item=item,
        project_id="p",
        agent="researcher",
        task_id="t",
    )

    assert paths.output_path == control / "state" / "research" / "x.md"

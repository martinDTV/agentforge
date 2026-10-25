import pytest
from pydantic import ValidationError

from app.contracts.planning import ArchitectureReview, DevelopmentPlan, DevelopmentTask


def test_development_plan_accepts_valid_dependencies():
    plan = DevelopmentPlan(
        summary="s",
        tasks=[
            DevelopmentTask(id="t1", title="a", description="d", acceptance_criteria=["ok"]),
            DevelopmentTask(
                id="t2",
                title="b",
                description="d",
                dependencies=["t1"],
                acceptance_criteria=["ok"],
            ),
        ],
    )
    assert plan.tasks[1].dependencies == ["t1"]


def test_development_plan_rejects_unknown_dependency():
    with pytest.raises(ValidationError, match="ids inexistentes"):
        DevelopmentPlan(
            summary="s",
            tasks=[
                DevelopmentTask(
                    id="t1",
                    title="a",
                    description="d",
                    dependencies=["missing"],
                    acceptance_criteria=["ok"],
                ),
            ],
        )


def test_development_plan_rejects_self_dependency():
    with pytest.raises(ValidationError, match="depender de sí misma"):
        DevelopmentPlan(
            summary="s",
            tasks=[
                DevelopmentTask(
                    id="t1",
                    title="a",
                    description="d",
                    dependencies=["t1"],
                    acceptance_criteria=["ok"],
                ),
            ],
        )


def test_development_plan_requires_at_least_one_task():
    with pytest.raises(ValidationError):
        DevelopmentPlan(summary="s", tasks=[])


def test_development_task_requires_acceptance_criteria():
    with pytest.raises(ValidationError):
        DevelopmentTask(id="t1", title="a", description="d", acceptance_criteria=[])


def test_architecture_review_defaults():
    review = ArchitectureReview(summary="s", decisions=[], approved=False)
    assert review.issues == []


def test_run_response_failed_without_plan():
    from app.contracts.execution import RunError, RunResponse

    response = RunResponse(
        run_id="r1",
        status="failed",
        owner="alice",
        project_id="alpha",
        error=RunError(code="llm_timeout", message="sin respuesta"),
    )
    assert response.plan is None
    assert response.architecture is None
    assert response.architect_approved is None
    assert response.model_dump()["error"] == {"code": "llm_timeout", "message": "sin respuesta"}


def test_run_response_rejects_unknown_status():
    from app.contracts.execution import RunResponse

    with pytest.raises(ValidationError):
        RunResponse(run_id="r1", status="paused", owner="a", project_id="p")  # type: ignore[arg-type]


def test_run_response_rejects_negative_counters():
    from app.contracts.execution import RunResponse

    with pytest.raises(ValidationError):
        RunResponse(run_id="r1", status="failed", owner="a", project_id="p", llm_calls=-1)

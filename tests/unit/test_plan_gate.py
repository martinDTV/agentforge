"""H1: a plan that breaks the platform constraints never reaches a human.

Reproduces the two real goals that died at the plan stage with synthetic fixtures: the
gate sends a violated plan back to the Planner with deterministic feedback (no model
call of its own), and ends the run ``plan_invalid`` when revisions or budget run out,
keeping the plan and the violations as evidence.
"""

from langchain_core.runnables import RunnableConfig

from app.contracts.lifecycle import RunEventKind
from app.contracts.planning import (
    PLAN_INVALID_CODE,
    ArchitectureReview,
    DevelopmentPlan,
    DevelopmentTask,
    PlanConstraints,
)
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.events import events_from_update
from app.orchestration.graph import build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.status import pending_decision, run_response_from_snapshot
from app.projects import (
    constraints_from_policy,
    extract_paths,
    filter_listing,
    policy_context_text,
    validate_plan,
)
from tests.unit.goal_fixtures import policy

RUN_ID = "plan-gate-run"
CONFIG: RunnableConfig = {"configurable": {"thread_id": RUN_ID}}
TASK = (
    "No modificar tests/test_acceptance.py. T2 depende de T1. T1: crear src/mailcheck/"
    "normalize.py con normalize_email y pruebas en tests/test_normalize.py. T2: usar "
    "normalize_email en validate_email (src/mailcheck/validator.py)."
)
SANDBOX_LISTING = [
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "src/mailcheck/__init__.py",
    "src/mailcheck/validator.py",
    "tests/test_acceptance.py",
    "tests/test_validator.py",
]


def task(key: str, title: str, description: str, *criteria: str, deps: list[str] | None = None):
    return DevelopmentTask(
        id=key,
        title=title,
        description=description,
        dependencies=deps or [],
        acceptance_criteria=list(criteria),
    )


# The plan the real Planner produced for goal 88b23688 (2026-10-31), trimmed to its facts.
E2_INVALID_PLAN = DevelopmentPlan(
    summary="Implementar normalize_email, verificar baseline e integrar.",
    tasks=[
        task(
            "T1",
            "Crear src/mailcheck/normalize.py con normalize_email y sus tests límite",
            "Implementar normalize_email(address: str) -> str. Añadir tests/test_normalize.py.",
            "normalize_email pasa los casos límite en tests/test_normalize.py sin tocar "
            "tests/test_acceptance.py",
            "TypeError se lanza exactamente cuando el argumento no es str",
        ),
        task(
            "T1b",
            "Documentar las 7 reglas y verificar baseline de veredictos de test_acceptance.py",
            "Listar las 7 reglas del README en un comentario de bloque en validator.py. "
            "Ejecutar pytest tests/test_acceptance.py y guardar salida baseline, dejando "
            "evidencia en NOTES.md.",
            "NOTES.md lista las 7 reglas",
            deps=["T1"],
        ),
        task(
            "T2",
            "Integrar normalize_email en validate_email y añadir tests de validador",
            "Aplicar normalize_email en validator.py antes de las 7 reglas. Añadir "
            "tests/test_validator.py con casos positivos y negativos.",
            "tests/test_acceptance.py produce los mismos veredictos que el baseline de NOTES.md",
            deps=["T1b"],
        ),
    ],
    risks=["La parte local puede contener @"],
)
# The plan a human approved for goal bac6eb0c the same day: two tasks, T2 after T1.
E2_VALID_PLAN = DevelopmentPlan(
    summary="Implementar normalize_email y usarla en validate_email.",
    tasks=[
        task(
            "T1",
            "Crear src/mailcheck/normalize.py con normalize_email y sus pruebas límite",
            "Implementar normalize_email. Añadir tests/test_normalize.py.",
            "normalize_email('User@Example.COM') == 'User@example.com'",
        ),
        task(
            "T2",
            "Integrar normalize_email en validate_email y añadir pruebas en test_validator.py",
            "Llamar a normalize_email antes de las 7 reglas en src/mailcheck/validator.py sin "
            "tocar test_acceptance.py.",
            "pytest pasa incluyendo tests/test_acceptance.py sin modificarlo",
            deps=["T1"],
        ),
    ],
)


def sandbox_constraints(max_tasks: int | None = 2) -> PlanConstraints:
    return constraints_from_policy(
        policy(denied_globs=["tests/test_acceptance.py"]),
        max_tasks=max_tasks,
        repository_paths=filter_listing(SANDBOX_LISTING, policy()),
        listing_complete=True,
    )


def approving() -> ArchitectureReview:
    return ArchitectureReview(summary="ok", decisions=[], issues=[], approved=True)


def three_tasks() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="tres",
        tasks=[task(k, f"Tarea {k}", f"Hacer {k} en src/{k}.py", f"{k} ok") for k in "abc"],
    )


def two_tasks() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="dos",
        tasks=[task(k, f"Tarea {k}", f"Hacer {k} en src/{k}.py", f"{k} ok") for k in "ab"],
    )


# -- pure validation ----------------------------------------------------------------------------


def test_the_real_invalid_plan_is_refused_for_its_task_count_and_its_notes_file():
    found = validate_plan(E2_INVALID_PLAN, sandbox_constraints())
    assert [(v.code, v.task_id) for v in found] == [
        ("too_many_tasks", None),
        ("path_outside_policy", "T1b"),
        ("path_outside_policy", "T2"),
    ]
    assert "máximo permitido es 2" in found[0].detail and "NOTES.md" in found[1].detail


def test_the_real_valid_plan_passes_even_though_it_names_protected_existing_files():
    assert validate_plan(E2_VALID_PLAN, sandbox_constraints()) == []


def test_structure_rules_count_tasks_and_duplicate_ids():
    duplicated = DevelopmentPlan(
        summary="dup", tasks=[task("a", "A", "x", "ok"), task("a", "A2", "y", "ok")]
    )
    codes = [v.code for v in validate_plan(duplicated, PlanConstraints(max_tasks=1))]
    assert codes == ["too_many_tasks", "duplicate_task_id"]
    assert validate_plan(three_tasks(), PlanConstraints()) == []  # no limit, no listing


def test_path_rules_only_concern_files_the_plan_would_create():
    constraints = sandbox_constraints(max_tasks=None)
    cases = {
        "tests/test_acceptance.py": [],  # exists: reading it is fine, writes die at scope
        "README.md": [],
        "src/mailcheck/new_module.py": [],  # new, inside the writable globs
        "test_normalize.py": [],  # bare code file: the Scoper places it
        "docs/notes.md": ["path_outside_policy"],
        "NOTES.md": ["path_outside_policy"],
        ".env": ["path_denied"],
        "config/secrets.json": ["path_denied"],
    }
    for path, expected in cases.items():
        plan = DevelopmentPlan(summary="p", tasks=[task("t", "T", f"Crear {path}", "ok")])
        assert [v.code for v in validate_plan(plan, constraints)] == expected, path
    without_listing = PlanConstraints(writable_globs=["src/**"], denied_globs=["**/.env"])
    plan = DevelopmentPlan(summary="p", tasks=[task("t", "T", "Crear NOTES.md y .env", "ok")])
    assert validate_plan(plan, without_listing) == []


def test_extract_paths_is_conservative():
    text = (
        "Usar mailcheck.normalize y http://example.com/a.py; tocar ./src/x.py, `tests/t_y.py`, "
        "README.md, v1.0, src/**/*.py y (docs/guide.md)."
    )
    assert extract_paths(text) == ["src/x.py", "tests/t_y.py", "README.md", "docs/guide.md"]


def test_listing_filter_hides_agent_instructions_and_denied_paths():
    shown = filter_listing([*SANDBOX_LISTING, ".env", ".github/workflows/ci.yml"], policy())
    assert "AGENTS.md" not in shown and ".env" not in shown
    assert ".github/workflows/ci.yml" not in shown and "README.md" in shown


def test_policy_context_is_data_about_the_platform_not_instructions():
    text = policy_context_text(sandbox_constraints())
    assert "Máximo de tareas en el plan: 2" in text
    assert "src/**, tests/**" in text and "tests/test_acceptance.py" in text
    assert "QA solo ejecuta: ruff check, ruff format --check, pytest" in text
    assert "src/mailcheck/validator.py" in text and "AGENTS.md" not in text
    assert "aprueba" not in text.lower() and "approved" not in text.lower()


# -- the gate inside the planning graph ---------------------------------------------------------


def run(llm: FakeLLMAdapter, constraints: PlanConstraints | None, limits: RunLimits | None = None):
    graph = build_graph(llm, limits)
    state = {"task": TASK}
    if constraints is not None:
        state["plan_constraints"] = constraints.model_dump(mode="json")
    graph.invoke(state, CONFIG)
    snapshot = graph.get_state(CONFIG)
    return snapshot, run_response_from_snapshot(RUN_ID, snapshot)


def test_an_oversized_plan_goes_back_to_the_planner_without_an_architect_call():
    llm = FakeLLMAdapter()
    llm.register_sequence(DevelopmentPlan, [three_tasks(), two_tasks()])
    llm.register(ArchitectureReview, approving())
    snapshot, result = run(llm, sandbox_constraints())
    assert result.status == "waiting_approval" and result.revision == 1
    assert result.llm_calls == 3  # planner, planner (gate revision), architect
    assert [c[2] for c in llm.calls] == [DevelopmentPlan, DevelopmentPlan, ArchitectureReview]
    assert result.plan is not None and len(result.plan.tasks) == 2
    revision_prompt = llm.calls[1][1]
    assert "máximo permitido es 2" in revision_prompt
    assert "Restricciones de la plataforma" in llm.calls[0][1]
    assert "Restricciones de la plataforma" in llm.calls[2][1]  # the architect sees them too
    assert snapshot.values["plan_violations"] == []


def test_persistent_violations_end_plan_invalid_before_any_human_decision():
    llm = FakeLLMAdapter()
    llm.register_sequence(DevelopmentPlan, [three_tasks(), three_tasks(), three_tasks()])
    llm.register(ArchitectureReview, approving())
    snapshot, result = run(llm, sandbox_constraints(), RunLimits(max_revisions=2, max_llm_calls=8))
    assert result.status == "failed"
    assert result.error is not None and result.error.code == PLAN_INVALID_CODE
    assert "máximo permitido es 2" in result.error.message
    assert result.llm_calls == 3 and result.revision == 2
    assert all(c[2] is DevelopmentPlan for c in llm.calls)  # the architect never ran
    assert pending_decision(snapshot) is None and snapshot.next == ()
    assert result.plan is not None and len(result.plan.tasks) == 3  # evidence kept
    assert [v["code"] for v in snapshot.values["plan_violations"]] == ["too_many_tasks"]


def test_the_gate_respects_the_revision_and_budget_limits():
    llm = FakeLLMAdapter()
    llm.register(DevelopmentPlan, three_tasks())
    llm.register(ArchitectureReview, approving())
    _, no_revisions = run(llm, sandbox_constraints(), RunLimits(max_revisions=0))
    assert no_revisions.status == "failed" and no_revisions.llm_calls == 1
    assert no_revisions.error is not None and no_revisions.error.code == PLAN_INVALID_CODE
    llm = FakeLLMAdapter()
    llm.register(DevelopmentPlan, three_tasks())
    llm.register(ArchitectureReview, approving())
    _, no_budget = run(llm, sandbox_constraints(), RunLimits(max_revisions=2, max_llm_calls=2))
    assert no_budget.status == "failed" and no_budget.llm_calls == 1  # a round would not fit


def test_the_real_invalid_plan_never_reaches_approval_with_max_tasks_two():
    llm = FakeLLMAdapter()
    llm.register_sequence(DevelopmentPlan, [E2_INVALID_PLAN, E2_VALID_PLAN])
    llm.register(ArchitectureReview, approving())
    snapshot, result = run(llm, sandbox_constraints())
    assert result.status == "waiting_approval" and result.plan == E2_VALID_PLAN
    assert "NOTES.md" in llm.calls[1][1]  # the planner was told exactly what was wrong


def test_without_constraints_the_graph_is_unchanged(fake_llm: FakeLLMAdapter):
    snapshot, result = run(fake_llm, None)
    assert result.status == "waiting_approval" and result.llm_calls == 2
    assert snapshot.values["plan_violations"] == []
    assert "Restricciones de la plataforma" not in fake_llm.calls[0][1]


def test_gate_violations_leave_an_audit_event_with_codes_only():
    delta = {"plan_violations": [{"code": "too_many_tasks", "detail": "x"}]}
    events = events_from_update({"plan_gate": delta}, revision=1)
    assert [e.kind for e in events] == [RunEventKind.PLAN_GATE_REJECTED]
    assert events[0].payload == {
        "revision": 1,
        "violations": ["too_many_tasks"],
        "final": False,
        "executed_by": "agentforge:platform",
    }
    final = events_from_update(
        {"plan_gate": {**delta, "error": {"code": PLAN_INVALID_CODE, "message": "m"}}},
        revision=2,
    )
    assert [e.kind for e in final] == [RunEventKind.PLAN_GATE_REJECTED, RunEventKind.RUN_FAILED]
    assert final[0].payload["final"] is True and final[0].payload["violations"] == [
        "too_many_tasks"
    ]
    assert final[1].payload["code"] == PLAN_INVALID_CODE and final[1].payload["node"] == "plan_gate"
    assert events_from_update({"plan_gate": {"plan_violations": []}}, revision=0) == []

"""Fixtures shared by the unit and the integration suites.

The unit suite (``tests/unit``, the default ``pytest`` target) needs no
external service. The integration suite (``tests/integration``) needs
PostgreSQL and is run explicitly. Neither may ever reach Vertex AI: the
autouse guard below makes constructing the real client fail loudly.
"""

import time
from collections.abc import Mapping
from typing import Any

import anthropic
import pytest

from app.contracts.planning import ArchitectureReview, DevelopmentPlan, DevelopmentTask
from app.infrastructure.llm.fake import FakeLLMAdapter


@pytest.fixture(autouse=True)
def _block_vertex_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "Vertex AI está bloqueado durante pytest: inyecta FakeLLMAdapter o un cliente stub"
        )

    monkeypatch.setattr(anthropic.AnthropicVertex, "__init__", _blocked)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sample_plan() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="Plan de prueba",
        tasks=[
            DevelopmentTask(
                id="t1",
                title="Analizar",
                description="Definir contratos",
                acceptance_criteria=["Contrato revisado"],
            )
        ],
        risks=["Ninguno relevante"],
    )


@pytest.fixture
def revised_plan() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="Plan revisado",
        tasks=[
            DevelopmentTask(
                id="t1",
                title="Analizar",
                description="Definir contratos",
                acceptance_criteria=["Contrato revisado"],
            ),
            DevelopmentTask(
                id="t2",
                title="Manejar errores",
                description="Cubrir errores del proveedor",
                dependencies=["t1"],
                acceptance_criteria=["Errores cubiertos"],
            ),
        ],
        risks=[],
    )


@pytest.fixture
def sample_review() -> ArchitectureReview:
    return ArchitectureReview(
        summary="Revisión de prueba",
        decisions=[],
        issues=[],
        approved=True,
    )


@pytest.fixture
def rejecting_review() -> ArchitectureReview:
    return ArchitectureReview(
        summary="Plan incompleto",
        decisions=[],
        issues=["Falta manejo de errores"],
        approved=False,
    )


@pytest.fixture
def fake_llm(sample_plan: DevelopmentPlan, sample_review: ArchitectureReview) -> FakeLLMAdapter:
    """Approving fake: one plan, one approving review, on every call.

    Tests override individual schemas with ``register_sequence`` to drive
    revision loops and failures.
    """
    adapter = FakeLLMAdapter()
    adapter.register(DevelopmentPlan, sample_plan)
    adapter.register(ArchitectureReview, sample_review)
    return adapter


# --- I0.1: background drives ---------------------------------------------------------

SETTLING_STATUSES = frozenset({"running", "resume_pending"})


def settle(
    client: Any,
    run_id: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    runs_path: str = "/api/v1/runs",
) -> dict[str, Any]:
    """Follow a run through ``GET /runs/{id}`` until its background drive stops.

    ``create``, ``approve`` and ``resume`` answer as soon as the work is durable
    (I0.1); API tests that assert on the next stop poll with this helper. The
    deadline is a safety net, never a fixed sleep: the loop returns on the first
    settled read.
    """
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"{runs_path}/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        if payload["status"] not in SETTLING_STATUSES:
            return payload
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} did not settle: {payload['status']}")
        time.sleep(0.02)

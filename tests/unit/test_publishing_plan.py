"""Publication plan, canonical digest, branch naming and the PR/commit templates (ADR 0011)."""

import uuid
from datetime import UTC, datetime

import pytest

from app.publishing import (
    PublicationFile,
    PublicationPlan,
    commit_message,
    neutralize_mentions,
    publish_digest,
    pull_request_body,
    pull_request_title,
    quote_untrusted,
    remote_branch_for,
)
from app.publishing.plan import RESERVED_PREFIXES, validate_branch_prefix

RUN = uuid.UUID("89484248-d461-4b4a-8def-62a62db649bb")
SHA_A = "a" * 40
SHA_B = "b" * 40
D = "d" * 64


def make_plan(**overrides) -> PublicationPlan:
    base = dict(
        run_id=RUN,
        repository_id="sandbox-github",
        project_id="sandbox-github",
        owner="martinDTV",
        name="agentforge-sandbox-demo",
        github_repository_id=1435009323,
        installation_id=175627193,
        base_branch="main",
        base_sha=SHA_A,
        base_branch_head=SHA_A,
        remote_branch=f"agentforge/{RUN}",
        head_sha=SHA_B,
        patch_digest="1" * 64,
        files=[
            PublicationFile(
                path="src/mailcheck/validator.py", change="modified", sha256="2" * 64, size=900
            ),
            PublicationFile(
                path="tests/test_validator.py", change="added", sha256="3" * 64, size=400
            ),
        ],
        scope_digest="4" * 64,
        scan_digest="5" * 64,
        title="[AgentForge] Implementa validate_email",
        summary="resumen del modelo",
        prepared_at=datetime(2026, 10, 27, 1, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return PublicationPlan(**base)  # type: ignore[arg-type]


def test_digest_is_canonical_and_ignores_informative_fields():
    plan = make_plan()
    assert publish_digest(plan) == plan.digest() and len(plan.digest()) == 64
    # Order of files, owner case and timestamps do not change the proof.
    reordered = make_plan(files=list(reversed(plan.files)))
    assert reordered.digest() == plan.digest()
    assert make_plan(owner="MARTINDTV").digest() == plan.digest()
    assert make_plan(prepared_at=datetime(2030, 1, 1, tzinfo=UTC)).digest() == plan.digest()
    assert make_plan(summary="otro resumen").digest() == plan.digest()


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": uuid.uuid4()},
        {"repository_id": "other"},
        {"github_repository_id": 1},
        {"installation_id": 2},
        {"name": "other-repo"},
        {"base_branch": "develop"},
        {"base_sha": "c" * 40},
        {"base_branch_head": "c" * 40},
        {"remote_branch": f"agentforge/{uuid.uuid4()}"},
        {"head_sha": "e" * 40},
        {"patch_digest": "9" * 64},
        {"scope_digest": "8" * 64},
        {"scan_digest": "7" * 64},
        {"title": "[AgentForge] otro título"},
        {
            "files": [
                PublicationFile(
                    path="src/mailcheck/validator.py", change="modified", sha256="f" * 64, size=1
                )
            ]
        },
    ],
)
def test_every_binding_field_changes_the_digest(change: dict):
    assert make_plan(**change).digest() != make_plan().digest()


def test_plan_rejects_malformed_identities_and_unknown_fields():
    with pytest.raises(ValueError):
        make_plan(head_sha="not-a-sha")
    with pytest.raises(ValueError):
        make_plan(files=[])
    with pytest.raises(ValueError):
        PublicationPlan.model_validate({**make_plan().model_dump(), "extra": 1})
    with pytest.raises(ValueError):
        make_plan(provider="gitlab")


def test_remote_branches_live_in_a_reserved_prefix_only():
    assert remote_branch_for("agentforge/", RUN) == f"agentforge/{RUN}"
    assert validate_branch_prefix("agentforge-bot/") == "agentforge-bot/"
    for bad in ("run/", "release/", "main/", "refs/", "", "agentforge", "-x/", "Agent/", "a b/"):
        with pytest.raises(ValueError):
            validate_branch_prefix(bad)
    assert "run/" in RESERVED_PREFIXES and "release/" in RESERVED_PREFIXES


def test_templates_quote_model_text_and_never_mention_anyone():
    assert neutralize_mentions("ping @octocat and @team-x") == "ping @​octocat and @​team-x"
    quoted = quote_untrusted("hola\n```\n<script>alert(1)</script>\x00 @admin", limit=60)
    assert quoted.startswith("```text\n") and quoted.endswith("\n```")
    assert "```\n<script>" not in quoted and "\x00" not in quoted and "@admin" not in quoted
    assert "'''" in quoted
    long = quote_untrusted("x" * 100, limit=20)
    assert "[... truncado]" in long and long.split("\n")[1] == "x" * 20
    title = pull_request_title("[AgentForge]", "Implementa validate_email\n@bob " + "y" * 200)
    assert title.startswith("[AgentForge] Implementa validate_email") and "\n" not in title
    assert "@bob" not in title and len(title) <= 120 and title.endswith("…")
    plan = make_plan()
    message = commit_message(
        title=plan.title,
        task="tarea @eve",
        run_id=plan.run_id,
        base_sha=plan.base_sha,
        scope_digest=plan.scope_digest,
        files=[("edit", "src/mailcheck/validator.py"), ("create", "tests/test_validator.py")],
        scope_approved_by="olivia",
    )
    first, _, rest = message.partition("\n")
    assert first == plan.title and len(first) <= 72
    assert "Run-Id: 89484248-d461-4b4a-8def-62a62db649bb" in rest
    assert f"Base-Sha: {SHA_A}" in rest and "Scope-Approved-By: olivia" in rest
    assert "@eve" not in message and "Co-Authored-By" not in message
    body = pull_request_body(
        plan,
        task="tarea",
        developer_summary="hecho @bob",
        qa_summary="ok",
        qa_checks=[
            {"program": "pytest", "exit_code": 0},
            {"program": "ruff_check", "exit_code": 1},
        ],
        scope_approved_by="olivia",
        scope_approved_at=datetime(2026, 10, 27, tzinfo=UTC),
        publish_approved_by="omar",
        publish_approved_at=datetime(2026, 10, 27, 1, tzinfo=UTC),
    )
    for expected in (
        "generados mediante agentes de IA",
        "## Resumen",
        "## Archivos modificados",
        "`src/mailcheck/validator.py` | modified",
        "## Pruebas ejecutadas en el sandbox",
        "`pytest` | 0 | ok",
        "`ruff_check` | 1 | falló",
        "## Commit base y trazabilidad",
        f"`main` en `{SHA_A}`",
        f"`agentforge/{RUN}` en `{SHA_B}`",
        "Alcance aprobado por `olivia`",
        "Publicación aprobada por `omar`",
        "No se fusiona",
    ):
        assert expected in body, expected
    assert "@bob" not in body and "ghs_" not in body

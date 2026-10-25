"""Authentication, health and protocol v1 conformance with the API's contracts."""

from pathlib import Path

import pytest
from agentforge_runner.auth import bearer_matches, client_allowed, generate_token, token_digest
from agentforge_runner.config import RunnerSettings
from agentforge_runner.programs import PREFIXES
from agentforge_runner.protocol import SandboxRequest as RunnerRequest
from agentforge_runner.protocol import SandboxResult as RunnerResult
from agentforge_runner.protocol import SandboxStatus as RunnerStatus
from pydantic import ValidationError

from app.infrastructure.sandbox import RunnerConfig
from app.sandbox import PROGRAMS, SandboxRequest, SandboxResult, SandboxStatus
from tests.unit.runner_fixtures import (
    AUTH,
    DEFAULT_ARCHIVE,
    IMAGE,
    RUNNER_ID,
    TOKEN,
    make_harness,
    make_request,
    make_settings,
    multipart,
)
from tests.unit.test_remote_sandbox import request as api_request


def test_tokens_are_compared_by_digest_and_never_stored():
    digest = token_digest(TOKEN)
    assert bearer_matches(f"Bearer {TOKEN}", digest) and bearer_matches(f"bearer {TOKEN}", digest)
    for bad in (None, "", "Bearer", "Basic abc", f"Bearer {TOKEN}x", "Bearer " + "x" * 600, TOKEN):
        assert not bearer_matches(bad, digest), bad
    assert not bearer_matches(f"Bearer {digest}", digest)  # the hash is not a credential
    fresh = generate_token()
    assert len(fresh) >= 48 and token_digest(fresh) != digest
    assert client_allowed("100.64.0.7", ["100.64.0.0/10"]) and client_allowed(
        "127.0.0.1", ["127.0.0.1/32"]
    )
    assert not client_allowed("192.168.1.9", ["100.64.0.0/10"]) and not client_allowed(
        "testclient", ["0.0.0.0/0"]
    )
    assert not client_allowed(None, ["0.0.0.0/0"])


def test_every_endpoint_requires_the_token_and_a_permitted_origin(tmp_path: Path):
    harness = make_harness(tmp_path)
    with harness.client() as client:
        for method, path in (
            ("GET", "/v1/health"),
            ("POST", "/v1/executions"),
            ("DELETE", f"/v1/executions/{make_request().execution_id}"),
            ("GET", f"/v1/executions/{make_request().execution_id}"),
        ):
            anonymous = client.request(method, path)
            assert anonymous.status_code == 401 and anonymous.json()["code"] == "unauthenticated", (
                path
            )
            wrong = client.request(method, path, headers={"Authorization": "Bearer nope"})
            assert wrong.status_code == 401
        assert client.get("/v1/health", headers=AUTH).status_code == 200
        assert (
            client.get("/docs").status_code == 404
            and client.get("/openapi.json").status_code == 404
        )
    with harness.client(address=("192.168.1.50", 4000)) as outsider:
        refused = outsider.get("/v1/health", headers=AUTH)
        assert refused.status_code == 403 and refused.json()["code"] == "client_not_allowed"
    assert TOKEN not in repr(harness.settings) and TOKEN not in str(harness.settings.model_dump())


def test_health_reports_identity_backend_and_readiness(tmp_path: Path):
    harness = make_harness(tmp_path)
    with harness.client() as client:
        body = client.get("/v1/health", headers=AUTH).json()
    assert body["runner_id"] == RUNNER_ID and body["backend"] == "podman-rootless"
    assert (
        body["rootless"] is True and body["ready"] is True and body["profiles"] == ["python-3.13"]
    )
    assert {c["name"] for c in body["checks"]} == {"rootless", "cgroups_v2", "probe:simulated"}
    with harness.client() as client:
        assert client.get("/v1/health", params={"refresh": "true"}, headers=AUTH).status_code == 200
    config = RunnerConfig(
        base_url="http://100.64.0.9:8787", token=TOKEN, expected_runner_id=RUNNER_ID
    )  # type: ignore[arg-type]
    from app.sandbox import RunnerHealth

    parsed = RunnerHealth.model_validate(body)  # the API ignores the extras
    assert parsed.runner_id == config.expected_runner_id


def test_protocol_models_round_trip_between_api_and_runner():
    api = api_request()
    runner_side = RunnerRequest.model_validate_json(api.model_dump_json())
    assert runner_side.execution_id == api.execution_id and runner_side.argv == api.argv
    back = SandboxRequest.model_validate_json(runner_side.model_dump_json())
    assert back == api
    result = RunnerResult(
        execution_id=api.execution_id,
        status=RunnerStatus.COMPLETED,
        exit_code=0,
        duration_ms=3,
        runner_id="r",
    )
    assert SandboxResult.model_validate_json(result.model_dump_json()).succeeded
    assert [s.value for s in RunnerStatus] == [s.value for s in SandboxStatus]
    assert {p.value: list(PREFIXES[p]) for p in PREFIXES} == {
        p.value: list(spec.argv_prefix) for p, spec in PROGRAMS.items()
    }
    with pytest.raises(ValidationError):
        RunnerRequest.model_validate_json(
            api.model_copy(update={"env": {"GITHUB_TOKEN": "x"}}).model_dump_json()
        )


def test_settings_refuse_unpinned_images_and_public_bind_addresses(tmp_path: Path):
    make_settings(tmp_path, bind_host="100.101.1.1")
    make_settings(tmp_path, bind_host="runner.tailnet-abcd.ts.net")
    with pytest.raises(ValidationError, match="sha256"):
        make_settings(tmp_path, images={"python-3.13": "docker.io/library/python:3.13"})
    with pytest.raises(ValidationError, match="al menos un perfil"):
        make_settings(tmp_path, images={})
    make_settings(tmp_path, bind_host="192.168.1.10")  # a local VM or a VPC address is private
    for public in ("0.0.0.0", "::", "8.8.8.8"):  # TEST-NET ranges count as private in ipaddress
        with pytest.raises(ValidationError, match="loopback|comodín"):
            make_settings(tmp_path, bind_host=public)
    with pytest.raises(ValidationError, match="comodín"):
        make_settings(tmp_path, bind_host="0.0.0.0", allow_public_bind=True)
    make_settings(tmp_path, bind_host="8.8.8.8", allow_public_bind=True)
    with pytest.raises(ValidationError):
        RunnerSettings.model_validate(
            {"runner_id": "r", "token_sha256": "short", "images": {"p": IMAGE}}
        )
    assert make_settings(tmp_path).state_dir == tmp_path / "state"


def test_a_complete_execution_answers_with_a_sandbox_result(tmp_path: Path):
    harness = make_harness(tmp_path)
    request = make_request()
    with harness.client() as client:
        response = client.post(
            "/v1/executions", files=multipart(request, DEFAULT_ARCHIVE), headers=AUTH
        )
    assert response.status_code == 200, response.text
    result = SandboxResult.model_validate(response.json())
    assert result.succeeded and result.execution_id == request.execution_id
    assert result.runner_id == RUNNER_ID and result.backend == "podman-rootless"
    [spec] = harness.backend.runs
    assert spec.container_name == f"agentforge-{request.execution_id}" and spec.image == IMAGE
    assert spec.argv == request.argv and spec.limits == request.limits and spec.env == {}
    assert spec.archive == DEFAULT_ARCHIVE  # streamed to the container, never written here
    assert not any(harness.settings.state_dir.rglob("README.md"))
    record = harness.store.get(request.execution_id)
    assert record is not None and record.state == "finished" and record.result is not None
    assert record.result.model_dump(mode="json") == response.json()  # stored == answered

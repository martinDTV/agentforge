"""AuthorizationPolicy: deny by default, roles -> permissions, project scope, ownership."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.lifecycle import RunLifecycleStatus
from app.orchestration.ports import PersistedRun
from app.security import (
    DEFAULT_ROLE_PERMISSIONS,
    LEGACY_OWNER,
    LEGACY_PROJECT,
    AuthorizationPolicy,
    Permission,
    PermissionDeniedError,
    Principal,
    ProjectRequiredError,
    Role,
    SelfApprovalForbiddenError,
)

P = Permission


def who(subject: str, roles: set[str] = set(), projects: set[str] = {"alpha"}) -> Principal:
    return Principal(
        subject=subject, issuer="t", roles=frozenset(roles), projects=frozenset(projects)
    )


def run(owner: str = "alice", project: str = "alpha") -> PersistedRun:
    now = datetime.now(UTC)
    return PersistedRun(
        id=uuid4(),
        task="t",
        status=RunLifecycleStatus.WAITING_APPROVAL,
        owner_subject=owner,
        project_id=project,
        created_at=now,
        updated_at=now,
    )


def test_roles_map_to_explicit_permissions_and_nothing_else():
    policy = AuthorizationPolicy()
    assert policy.permissions_of(who("u", {"viewer"})) == {P.RUNS_READ, P.RUNS_AUDIT_READ}
    assert P.RUNS_CREATE in policy.permissions_of(who("u", {"operator"}))
    assert P.RUNS_APPROVE not in policy.permissions_of(who("u", {"operator"}))
    assert P.RUNS_APPROVE in policy.permissions_of(who("u", {"approver"}))
    assert P.PROJECTS_ALL not in policy.permissions_of(who("u", {"admin"}))
    assert P.PROJECTS_ALL in policy.permissions_of(who("u", {"platform_admin"}))
    assert set(DEFAULT_ROLE_PERMISSIONS) == {r.value for r in Role}


def test_deny_by_default_for_no_roles_or_unknown_roles():
    policy = AuthorizationPolicy()
    for principal in (who("u"), who("u", {"superuser", "root"})):
        assert policy.permissions_of(principal) == frozenset()
        assert not policy.can_see(principal, run(owner="someone-else"))
        with pytest.raises(PermissionDeniedError):
            policy.resolve_project_for_create(principal, None)


def test_project_membership_bounds_every_role_except_projects_all():
    policy = AuthorizationPolicy()
    alpha_admin = who("adm", {"admin"}, {"alpha"})
    assert policy.can_see(alpha_admin, run(project="alpha"))
    assert not policy.can_see(alpha_admin, run(project="beta"))
    assert policy.decide(alpha_admin, P.RUNS_APPROVE, run(project="beta")).outcome == "hidden"
    platform = who("root", {"platform_admin"}, set())
    assert policy.can_see(platform, run(project="beta"))
    assert policy.decide(platform, P.RUNS_APPROVE, run(project="beta")).allowed


def test_legacy_project_is_reserved_even_if_a_token_claims_it():
    policy = AuthorizationPolicy()
    sneaky = who("u", {"admin"}, {LEGACY_PROJECT, "alpha"})
    legacy = run(owner=LEGACY_OWNER, project=LEGACY_PROJECT)
    assert not policy.can_see(sneaky, legacy)
    with pytest.raises(PermissionDeniedError):
        policy.resolve_project_for_create(sneaky, LEGACY_PROJECT)
    assert policy.resolve_project_for_create(sneaky, None) == "alpha"  # legacy never counts
    assert policy.can_see(who("root", {"platform_admin"}, set()), legacy)


def test_owner_sees_own_run_without_read_permission_but_cannot_approve_it():
    policy = AuthorizationPolicy()
    owner_without_roles = who("alice", set())
    assert policy.can_see(owner_without_roles, run(owner="alice"))
    assert policy.decide(owner_without_roles, P.RUNS_READ, run(owner="alice")).allowed
    assert policy.decide(owner_without_roles, P.RUNS_APPROVE, run(owner="alice")).outcome == (
        "forbidden"
    )


def test_hidden_vs_forbidden_vs_self_approval():
    strict = AuthorizationPolicy(separation_of_duties=True)
    lenient = AuthorizationPolicy(separation_of_duties=False)
    viewer = who("v", {"viewer"})
    approver = who("a", {"approver"})
    creator_approver = who("alice", {"operator", "approver"})
    target = run(owner="alice")

    assert strict.decide(viewer, P.RUNS_APPROVE, target).outcome == "forbidden"
    assert strict.decide(who("x", {"approver"}, {"beta"}), P.RUNS_APPROVE, target).outcome == (
        "hidden"
    )
    assert strict.decide(approver, P.RUNS_APPROVE, target).allowed
    self_decision = strict.decide(creator_approver, P.RUNS_APPROVE, target)
    assert self_decision.outcome == "forbidden" and self_decision.reason == "self_approval"
    with pytest.raises(SelfApprovalForbiddenError):
        strict.raise_for(self_decision, run_id="r")
    assert lenient.decide(creator_approver, P.RUNS_APPROVE, target).allowed
    with pytest.raises(PermissionDeniedError) as info:
        strict.raise_for(strict.decide(viewer, P.RUNS_APPROVE, target), run_id="r")
    assert "runs:approve" in str(info.value)


def test_project_resolution_for_create():
    policy = AuthorizationPolicy()
    assert policy.resolve_project_for_create(who("u", {"operator"}, {"alpha"}), None) == "alpha"
    assert policy.resolve_project_for_create(who("u", {"operator"}, {"alpha"}), "alpha") == "alpha"
    with pytest.raises(PermissionDeniedError):
        policy.resolve_project_for_create(who("u", {"operator"}, {"alpha"}), "beta")
    with pytest.raises(PermissionDeniedError, match="ningún proyecto"):
        policy.resolve_project_for_create(who("u", {"operator"}, set()), None)
    with pytest.raises(ProjectRequiredError) as info:
        policy.resolve_project_for_create(who("u", {"operator"}, {"alpha", "beta"}), None)
    assert info.value.projects == ["alpha", "beta"]
    with pytest.raises(PermissionDeniedError, match="runs:create"):
        policy.resolve_project_for_create(who("u", {"viewer"}, {"alpha"}), None)
    root = who("root", {"platform_admin"}, set())
    assert policy.resolve_project_for_create(root, "gamma") == "gamma"
    with pytest.raises(PermissionDeniedError):
        policy.resolve_project_for_create(
            root, None
        )  # global scope still needs an explicit project


def test_only_the_agent_role_grants_tools_and_only_humans_grant_repo_writes():
    policy = AuthorizationPolicy()
    writers = {Role.PROJECT_OPERATOR, Role.ADMIN, Role.PLATFORM_ADMIN}
    for role in Role:
        granted = policy.permissions_of(who("u", {role}))
        assert (P.TOOLS_EXECUTE in granted) == (role is Role.AGENT)
        assert (P.REPOS_WRITE in granted) == (role in writers)  # ADR 0009: humans only
        assert (P.REPOS_PUBLISH in granted) == (role in writers)  # ADR 0011: humans only
        assert (P.RUNS_CANCEL in granted) == (role in writers | {Role.OPERATOR})
    assert policy.permissions_of(who("u", {Role.AGENT})) == {P.TOOLS_EXECUTE}


def test_listing_scope_mirrors_visibility():
    policy = AuthorizationPolicy()
    scope = policy.listing_scope(who("v", {"viewer"}, {"alpha", "beta", LEGACY_PROJECT}))
    assert scope.projects == {"alpha", "beta"} and scope.owner_only is None
    owner_only = policy.listing_scope(who("alice", set(), {"alpha"}))
    assert owner_only.projects == {"alpha"} and owner_only.owner_only == "alice"
    everything = policy.listing_scope(who("root", {"platform_admin"}, set()))
    assert everything.projects is None and everything.owner_only is None
    nothing = policy.listing_scope(who("nomad", {"operator"}, set()))
    assert nothing.is_empty
    assert scope.narrowed_to("alpha").projects == {"alpha"}
    assert scope.narrowed_to("gamma").is_empty  # a filter never widens the scope
    assert everything.narrowed_to("gamma").projects == {"gamma"}


def test_sweep_scope_requires_the_permission_and_follows_project_membership():
    policy = AuthorizationPolicy()
    with pytest.raises(PermissionDeniedError, match="recovery:sweep"):
        policy.sweep_scope(who("op", {"operator"}, {"alpha"}))
    assert policy.sweep_scope(who("adm", {"admin"}, {"alpha", LEGACY_PROJECT})).projects == {
        "alpha"
    }
    assert policy.sweep_scope(who("root", {"platform_admin"}, set())).projects is None


def test_publication_approval_needs_repos_publish_and_distinct_humans():
    policy = AuthorizationPolicy(separation_of_duties=True)
    row = run(owner="olivia")
    omar = who("omar", {"project_operator"})
    assert policy.decide_publish(omar, row, scope_approver="olivia").allowed
    # The creator never publishes their own run, whatever their roles.
    creator = policy.decide_publish(who("olivia", {"project_operator"}), row, scope_approver=None)
    assert creator.outcome == "forbidden" and creator.reason == "self_approval"
    with pytest.raises(SelfApprovalForbiddenError):
        policy.raise_for(creator, run_id="r")
    # runs:approve without repos:publish is not enough; viewers are forbidden; outsiders hidden.
    approver = policy.decide_publish(who("alice", {"approver"}), row, scope_approver=None)
    assert approver.outcome == "forbidden" and "repos:publish" in approver.reason
    assert policy.decide_publish(who("victor", {"viewer"}), row, scope_approver=None).outcome == (
        "forbidden"
    )
    outsider = who("eve", {"project_operator"}, {"other"})
    assert policy.decide_publish(outsider, row, scope_approver=None).outcome == "hidden"
    # Four real eyes: the scope approver may not also approve publication.
    same = policy.decide_publish(omar, row, scope_approver="omar")
    assert same.outcome == "forbidden" and same.reason == "publish_same_approver"
    with pytest.raises(PermissionDeniedError, match="distinto"):
        policy.raise_for(same, run_id="r")
    relaxed = AuthorizationPolicy(separation_of_duties=False, publish_distinct_approver=False)
    assert relaxed.decide_publish(omar, row, scope_approver="omar").allowed
    # Even relaxed, the creator never publishes their own run.
    assert relaxed.decide_publish(who("olivia", {"admin"}), row, scope_approver=None).reason == (
        "self_approval"
    )
    # Agents hold no run permission at all.
    agent = who("agentforge:developer", {"agent"})
    assert policy.decide_publish(agent, row, scope_approver=None).outcome == "hidden"

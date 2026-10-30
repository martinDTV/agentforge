"""Turn persisted events into outbox rows, inside the caller's transaction."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.goals.ports import GoalEvent
from app.orchestration.ports import RunEvent

from .contracts import NotificationKind
from .policy import GoalContext, NotificationPolicy, RunContext, goal_message_id_for, message_id_for


@dataclass(frozen=True)
class OutboxDraft:
    run_context: RunContext | None
    event_id: int | None
    kind: NotificationKind
    recipient: str
    subject: str
    text_body: str
    html_body: str
    dedup_key: str
    message_id: str
    goal_id: UUID | None = None

    @property
    def run_id(self) -> UUID | None:
        return self.run_context.run_id if self.run_context is not None else None


NotificationLevel = Literal["priority", "verbose"]

# ``priority`` (default): what needs a human or ends a run — pending approvals,
# failures and terminal outcomes. ``verbose`` adds every intermediate stage.
PRIORITY_KINDS: frozenset[NotificationKind] = frozenset(
    {
        NotificationKind.PLAN_PENDING,
        NotificationKind.SCOPE_PENDING,
        NotificationKind.PUBLISH_PENDING,
        NotificationKind.RUN_FAILED,
        NotificationKind.RUN_COMPLETED,
        NotificationKind.PULL_REQUEST_OPENED,
        NotificationKind.RUN_PUBLISHED,
        NotificationKind.RUN_CANCELLED,
        NotificationKind.RUN_EXPIRED,
        NotificationKind.GOAL_NEEDS_HUMAN,
        NotificationKind.GOAL_COMPLETED,
        NotificationKind.GOAL_PARTIAL,
        NotificationKind.GOAL_FAILED,
        NotificationKind.GOAL_CANCELLED,
        NotificationKind.GOAL_EXPIRED,
    }
)


class NotificationEnqueuer:
    """Stateless: recipients, level and the message-id domain come from Settings."""

    def __init__(
        self,
        policy: NotificationPolicy,
        *,
        recipients: Sequence[str],
        domain: str = "agentforge.local",
        level: NotificationLevel = "priority",
    ) -> None:
        self._policy = policy
        self._recipients = [r for r in recipients if r]
        self._domain = domain
        self._level: NotificationLevel = level

    @property
    def level(self) -> NotificationLevel:
        return self._level

    def wants(self, kind: NotificationKind) -> bool:
        return self._level == "verbose" or kind in PRIORITY_KINDS

    @property
    def active(self) -> bool:
        return bool(self._recipients)

    def drafts(
        self,
        run: RunContext,
        events: Sequence[tuple[int | None, RunEvent]],
        *,
        moment: datetime,
    ) -> list[OutboxDraft]:
        drafts: list[OutboxDraft] = []
        if not self._recipients:
            return drafts
        for event_id, event in events:
            draft = self._policy.draft(
                run, kind=event.kind, payload=event.payload, ordinal=event.ordinal, moment=moment
            )
            if draft is None or not self.wants(draft.kind):
                continue
            for recipient in self._recipients:
                drafts.append(
                    OutboxDraft(
                        run_context=run,
                        event_id=event_id,
                        kind=draft.kind,
                        recipient=recipient,
                        subject=draft.subject,
                        text_body=draft.text,
                        html_body=draft.html,
                        dedup_key=draft.dedup_key(run.run_id, recipient),
                        message_id=message_id_for(
                            run.run_id, draft.kind, draft.ordinal, self._domain
                        ),
                    )
                )
        return drafts

    def goal_drafts(
        self,
        goal: GoalContext,
        events: Sequence[tuple[int | None, GoalEvent]],
        *,
        moment: datetime,
    ) -> list[OutboxDraft]:
        """Same contract as ``drafts`` for a goal's events (ADR 0012 §2.8)."""
        drafts: list[OutboxDraft] = []
        if not self._recipients:
            return drafts
        for event_id, event in events:
            draft = self._policy.draft_goal(
                goal, kind=event.kind, payload=event.payload, ordinal=event.ordinal, moment=moment
            )
            if draft is None or not self.wants(draft.kind):
                continue
            for recipient in self._recipients:
                drafts.append(
                    OutboxDraft(
                        run_context=None,
                        goal_id=goal.goal_id,
                        event_id=event_id,
                        kind=draft.kind,
                        recipient=recipient,
                        subject=draft.subject,
                        text_body=draft.text,
                        html_body=draft.html,
                        dedup_key=draft.goal_dedup_key(goal.goal_id, recipient),
                        message_id=goal_message_id_for(
                            goal.goal_id, draft.kind, draft.ordinal, self._domain
                        ),
                    )
                )
        return drafts

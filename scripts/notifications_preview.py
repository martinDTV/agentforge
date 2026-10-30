"""Render every notification kind from synthetic fixtures into one local HTML page.

    uv run python -m scripts.notifications_preview      # -> .local/notifications-preview.html
    uv run python -m scripts.notifications_preview --out /tmp/p.html --public-url https://x.example

Nothing is sent and nothing real is read: the fixtures in ``app.notifications.samples``
are invented. Open the file in a browser to review the layout; the text/plain
alternative of each mail is shown next to its HTML.
"""

import argparse
import html
import sys
from collections.abc import Sequence
from pathlib import Path

from app.notifications import NotificationPolicy, PublicLinks
from app.notifications.policy import NotificationDraft
from app.notifications.samples import (
    MOMENT,
    goal_context,
    goal_samples,
    run_context,
    run_samples,
)

DEFAULT_OUT = Path(".local") / "notifications-preview.html"


def render_all(policy: NotificationPolicy) -> list[tuple[str, NotificationDraft]]:
    drafts: list[tuple[str, NotificationDraft]] = []
    for sample in run_samples():
        draft = policy.draft(
            sample.run or run_context(),
            kind=sample.kind,
            payload=sample.payload,
            ordinal=sample.ordinal,
            moment=MOMENT,
            context=sample.context,
        )
        if draft is not None:
            drafts.append((sample.name, draft))
    goal = goal_context()
    for goal_sample in goal_samples():
        draft = policy.draft_goal(
            goal,
            kind=goal_sample.kind,
            payload=goal_sample.payload,
            ordinal=goal_sample.ordinal,
            moment=MOMENT,
            context=goal_sample.context,
        )
        if draft is not None:
            drafts.append((goal_sample.name, draft))
    return drafts


def page(drafts: list[tuple[str, NotificationDraft]]) -> str:
    cards = []
    for name, draft in drafts:
        cards.append(
            "<section style='margin:0 0 48px 0'>"
            f"<h2 style='font:600 15px -apple-system,Segoe UI,sans-serif;color:#374151;"
            f"margin:0 0 6px 0'>{html.escape(name)} · "
            f"<code>{html.escape(draft.kind.value)}</code> · ordinal {draft.ordinal}</h2>"
            "<div style='font:13px -apple-system,Segoe UI,sans-serif;color:#6b7280;"
            f"margin:0 0 10px 0'>Asunto: <strong>{html.escape(draft.subject)}</strong></div>"
            "<div style='display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap'>"
            f"<iframe title='{html.escape(name)}' sandbox "
            f'srcdoc="{html.escape(draft.html, quote=True)}" '
            "style='width:640px;height:760px;border:1px solid #cbd2d9;background:#fff'></iframe>"
            "<pre style='flex:1;min-width:320px;max-width:520px;white-space:pre-wrap;"
            "font:12px/18px Menlo,Consolas,monospace;background:#f8fafc;"
            "border:1px solid #d9e2ec;padding:12px;margin:0'>"
            f"{html.escape(draft.text)}</pre></div></section>"
        )
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<title>AgentForge — previsualización de correos</title></head>"
        "<body style='margin:0;padding:24px;background:#eef1f5'>"
        f"<h1 style='font:600 20px -apple-system,Segoe UI,sans-serif;color:#111827'>"
        f"AgentForge — previsualización de {len(drafts)} correos (fixtures sintéticos)</h1>"
        "<p style='font:13px -apple-system,Segoe UI,sans-serif;color:#6b7280'>Generado con "
        "scripts.notifications_preview; nada se ha enviado.</p>" + "".join(cards) + "</body></html>"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--public-url", default=None, help="PUBLIC_APP_URL to preview platform links (https)"
    )
    args = parser.parse_args(argv)
    policy = NotificationPolicy(links=PublicLinks(args.public_url) if args.public_url else None)
    drafts = render_all(policy)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page(drafts), encoding="utf-8")
    print(f"{len(drafts)} correos previsualizados en {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

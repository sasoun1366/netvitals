#!/usr/bin/env python3
"""Announce a release in a Telegram channel.

Runs inside GitHub Actions on a ``release: published`` event, or by hand from the
Actions tab ("Run workflow"). The bot token and the channel id are read from
repository secrets, so nothing secret lives in the repository.

    python3 .github/telegram/announce.py --dry-run
    python3 .github/telegram/announce.py --lang both
    python3 .github/telegram/announce.py --test-message "hello from CI"

Exit codes: 0 posted (or deliberately skipped), 1 Telegram refused the post.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

#: Telegram's Bot API. Overridable with --api for a self-hosted Bot API server (and for
#: the test suite, which talks to a local double).
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org/bot{token}/{method}")
MAX_BODY = 340  # characters of release notes to include


# ── writing the post ────────────────────────────────────────────────────────────────


def esc(text: Any) -> str:
    """Escape for Telegram's HTML parse mode."""
    return html.escape(str(text or ""), quote=False)


def human_size(num: Any) -> str:
    try:
        size = float(num)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return ""


def summarise(body: str) -> str:
    """The first paragraph or two of the release notes, trimmed to something readable."""
    text = (body or "").strip()
    if not text:
        return ""
    # Drop markdown headings and code fences; Telegram HTML mode does not render them.
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            if lines:
                break
            continue
        if line.lower().lstrip("*_ ").startswith("full changelog"):
            # GitHub writes this line itself when a release has no notes of its own;
            # the post already links to the release below.
            continue
        lines.append(line.lstrip("#").strip())
        if len(" ".join(lines)) > MAX_BODY:
            break
    summary = " ".join(lines).strip()
    # Inline markdown emphasis would show up as literal characters.
    for token in ("**", "`", "__"):
        summary = summary.replace(token, "")
    if len(summary) > MAX_BODY:
        summary = summary[:MAX_BODY].rsplit(" ", 1)[0] + " …"
    return summary


def build_message(
    payload: dict[str, Any], lang: str = "fa", bullets: list[str] | None = None
) -> str:
    """Render the announcement from a GitHub ``release`` webhook payload."""
    repo = (payload.get("repository") or {}).get("name") or "repository"
    release = payload.get("release") or {}
    tag = release.get("tag_name") or release.get("name") or "release"
    version = str(tag).lstrip("vV") or tag
    url = release.get("html_url") or (
        f"https://github.com/{(payload.get('repository') or {}).get('full_name', '')}"
        f"/releases/tag/{tag}"
    )
    summary = summarise(release.get("body") or "")
    bullets = bullets or []
    assets = [
        asset
        for asset in (release.get("assets") or [])
        if asset.get("name") and asset.get("browser_download_url")
    ]

    blocks: list[str] = []
    if lang in ("fa", "both"):
        lines = [f"🚀 <b>{esc(repo)}</b> — نسخهٔ <b>{esc(version)}</b> منتشر شد"]
        if summary:
            lines += ["", esc(summary)]
        if bullets:
            lines += ["", "🛠 تغییرات:"] + [f"• {esc(b)}" for b in bullets]
        if assets:
            lines += ["", "📥 دانلود:"]
            lines += [
                f"• <a href=\"{esc(a['browser_download_url'])}\">{esc(a['name'])}</a>"
                + (f" — {human_size(a.get('size'))}" if a.get("size") else "")
                for a in assets
            ]
        lines += ["", f"🔗 <a href=\"{esc(url)}\">جزئیات و تغییرات</a>"]
        blocks.append("\n".join(lines))

    if lang in ("en", "both"):
        lines = [f"🚀 <b>{esc(repo)}</b> <b>{esc(version)}</b> is out"]
        if summary and lang == "en":
            lines += ["", esc(summary)]
        if bullets and lang == "en":
            lines += ["", "🛠 Changes:"] + [f"• {esc(b)}" for b in bullets]
        if assets:
            lines += ["", "📥 Downloads:"]
            lines += [
                f"• <a href=\"{esc(a['browser_download_url'])}\">{esc(a['name'])}</a>"
                + (f" — {human_size(a.get('size'))}" if a.get("size") else "")
                for a in assets
            ]
        lines += ["", f"🔗 <a href=\"{esc(url)}\">Release notes</a>"]
        blocks.append("\n".join(lines))

    return "\n\n—————\n\n".join(blocks) if len(blocks) > 1 else blocks[0]


def build_test_message(text: str, lang: str = "both") -> str:
    """The post sent by the "Run workflow" button, so a channel can be checked on demand.

    With ``lang=both`` the two halves of ``fa || en`` go to their own block; a single
    string is used for whichever languages were asked for.
    """
    repo = (os.environ.get("GITHUB_REPOSITORY") or "netpilot").split("/")[-1]
    fa_text, _, en_text = text.partition("||")
    en_text = en_text.strip() or fa_text.strip()
    default_fa = "این یک پست آزمایشی است؛ اتصال گیت‌هاب به کانال برقرار شد."
    default_en = "This is a test post; GitHub is connected to the channel."

    blocks: list[str] = []
    if lang in ("fa", "both"):
        blocks.append(
            f"✅ <b>{esc(repo)}</b> — {esc(fa_text.strip() or default_fa)}"
        )
    if lang in ("en", "both"):
        blocks.append(
            f"✅ <b>{esc(repo)}</b> — {esc(en_text.strip() or default_en)}"
        )
    return "\n\n—————\n\n".join(blocks) if len(blocks) > 1 else blocks[0]


def _api(path: str, token: str | None) -> Any:
    request = urllib.request.Request(f"https://api.github.com{path}")
    request.add_header("Accept", "application/vnd.github+json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed host
        return json.loads(response.read().decode("utf-8"))


def changelog_bullets(
    repo_full_name: str, tag: str, token: str | None = None, limit: int = 5
) -> list[str]:
    """Commit subjects since the previous release, for when the release has no notes.

    Best-effort: any API problem means the post simply has no change list.
    """
    if not repo_full_name or not tag:
        return []
    try:
        releases = _api(f"/repos/{repo_full_name}/releases?per_page=10", token)
        others = [r.get("tag_name") for r in releases if r.get("tag_name") != tag]
        subjects: list[str] = []
        if others:
            compare = _api(f"/repos/{repo_full_name}/compare/{others[0]}...{tag}", token)
            for commit in compare.get("commits") or []:
                message = ((commit.get("commit") or {}).get("message") or "").splitlines()
                if message and not message[0].startswith("Merge "):
                    subjects.append(message[0])
            extra = max(0, int(compare.get("total_commits") or 0) - len(subjects))
        else:
            commits = _api(f"/repos/{repo_full_name}/commits?sha={tag}&per_page={limit}", token)
            subjects = [
                ((c.get("commit") or {}).get("message") or "").splitlines()[0]
                for c in commits
                if (c.get("commit") or {}).get("message")
            ]
            extra = 0
    except Exception as exc:  # noqa: BLE001 - a nice-to-have, never a blocker
        print(f"could not build the change list ({exc}); posting without it")
        return []

    bullets = [s.strip()[:110] for s in subjects[:limit]]
    if extra or len(subjects) > limit:
        bullets.append(f"… (+{extra or len(subjects) - limit} more)")
    return bullets


# ── talking to Telegram ─────────────────────────────────────────────────────────────


def send(
    token: str,
    chat_id: str,
    text: str,
    *,
    dry_run: bool = False,
    api: str = API,
) -> dict[str, Any]:
    if dry_run:
        print("--- dry run: nothing was sent ---")
        print(text)
        print("--- end of message ---")
        return {"ok": True, "result": {"dry_run": True}}

    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode()
    request = urllib.request.Request(api.format(token=token, method="sendMessage"), data=body)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed host
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            description = parsed.get("description", detail)
            code = parsed.get("error_code", exc.code)
        except json.JSONDecodeError:
            description, code = detail, exc.code
        raise SystemExit(
            f"Telegram refused the post ({code}): {description}\n"
            "Common causes: the bot is not an administrator of the channel, the channel id "
            "is wrong (a private channel id looks like -1001234567890), or the token was "
            "revoked in BotFather."
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach the Telegram API: {exc.reason}") from exc


def load_payload(path: str | None) -> dict[str, Any]:
    if path and Path(path).is_file():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post a release announcement to Telegram.")
    parser.add_argument(
        "--lang",
        default=os.environ.get("TELEGRAM_LANG", "both"),
        choices=["fa", "en", "both"],
        help="language of the post (default: $TELEGRAM_LANG or both)",
    )
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID"))
    parser.add_argument("--test-message", help="send this instead of a release announcement")
    parser.add_argument(
        "--api",
        default=API,
        help="Bot API endpoint template (default: %(default)s)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the post, send nothing")
    args = parser.parse_args(argv)

    if args.test_message:
        text = build_test_message(args.test_message, args.lang)
    else:
        payload = load_payload(args.event)
        if not payload.get("release"):
            print("no release in this event; nothing to announce")
            return 0
        release = payload["release"]
        bullets: list[str] = []
        if not summarise(release.get("body") or ""):
            repo_full = (payload.get("repository") or {}).get("full_name", "")
            print("the release has no notes of its own; listing the commits instead")
            bullets = changelog_bullets(
                repo_full, release.get("tag_name") or "", os.environ.get("GITHUB_TOKEN")
            )
        text = build_message(payload, args.lang, bullets)

    if not args.token or not args.chat_id:
        if args.dry_run:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set; showing the message only.")
            send("", "", text, dry_run=True)
            return 0
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", args.token),
                ("TELEGRAM_CHAT_ID", args.chat_id),
            )
            if not value
        ]
        # A repository that has not been connected yet should not fail its release.
        print(
            f"::warning::{' and '.join(missing)} not set — skipping the Telegram announcement. "
            "Add them under Settings → Secrets and variables → Actions."
        )
        return 0

    result = send(args.token, args.chat_id, text, dry_run=args.dry_run, api=args.api)
    if not result.get("ok"):
        raise SystemExit(f"Telegram said: {result}")
    if args.dry_run:
        return 0
    message_id = (result.get("result") or {}).get("message_id")
    print(f"posted to {args.chat_id} (message_id={message_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

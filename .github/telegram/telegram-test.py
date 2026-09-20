"""A local stand-in for the Telegram Bot API, used to test announce.py for real.

    python3 telegram-test.py            # runs the checks and prints a summary
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ANNOUNCE = os.path.join(HERE, "announce.py")

received: list[dict] = []
mode = {"status": 200, "body": {"ok": True, "result": {"message_id": 42}}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server's spelling
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        received.append(
            {
                "path": self.path,
                "params": dict(urllib.parse.parse_qsl(raw)),
            }
        )
        payload = json.dumps(mode["body"]).encode()
        self.send_response(mode["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep the test output clean
        pass


def run(**env_overrides) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run(
        [sys.executable, ANNOUNCE, "--lang", env.get("LANG_ARG", "both")],
        capture_output=True,
        text=True,
        env=env,
        cwd=HERE,
        check=False,
    )


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    api = f"http://127.0.0.1:{port}/bot{{token}}/{{method}}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"{'PASS' if condition else 'FAIL'}  {name}")
        if not condition:
            failures.append(f"{name}: {detail}")

    # 1. a release event with secrets set: one POST, correct chat, HTML, both languages
    payload = {
        "repository": {"name": "netpilot", "full_name": "sasoun1366/netpilot"},
        "release": {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/sasoun1366/netpilot/releases/tag/v9.9.9",
            "body": "",
            "assets": [
                {
                    "name": "netpilot-9.9.9-windows-desktop.exe",
                    "size": 51_700_000,
                    "browser_download_url": "https://example.invalid/x.exe",
                }
            ],
        },
    }
    event = os.path.join(HERE, "event.json")
    with open(event, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    env = {
        "TELEGRAM_BOT_TOKEN": "123:TEST",
        "TELEGRAM_CHAT_ID": "-1001234567890",
        "GITHUB_EVENT_PATH": event,
        "TELEGRAM_API": api,
        "LANG_ARG": "both",
    }
    result = run(**env)
    check("exit code 0 on a successful post", result.returncode == 0, result.stderr[-300:])
    check("exactly one request reached the API", len(received) == 1, str(len(received)))
    if received:
        sent = received[0]["params"]
        check("post went to the right chat", sent.get("chat_id") == "-1001234567890", sent.get("chat_id"))
        check("HTML parse mode", sent.get("parse_mode") == "HTML", sent.get("parse_mode"))
        text = sent.get("text", "")
        check("mentions the version", "9.9.9" in text, text[:120])
        check("links the asset", "netpilot-9.9.9-windows-desktop.exe" in text)
        check("has both languages", "منتشر شد" in text and "is out" in text, text[:200])
        check("no raw HTML left unescaped", "<b>" in text and "&lt;" not in text)
    check("printed the message id", "message_id=42" in result.stdout, result.stdout[-200:])

    # 2. no secrets configured: a warning, exit 0, nothing sent
    received.clear()
    result = run(TELEGRAM_API=api, GITHUB_EVENT_PATH=event)
    check("missing secrets do not fail the job", result.returncode == 0, result.stderr[-300:])
    check("missing secrets send nothing", not received)
    check("missing secrets warn in the log", "::warning::" in result.stdout, result.stdout[-300:])

    # 3. Telegram refuses (bot not an admin): exit 1 with a readable reason
    received.clear()
    mode["status"] = 403
    mode["body"] = {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member of the channel chat"}
    result = run(**env)
    check("a refusal is a failure", result.returncode == 1, str(result.returncode))
    check("the reason is explained", "administrator" in (result.stderr or ""), result.stderr[-300:])
    mode["status"] = 200
    mode["body"] = {"ok": True, "result": {"message_id": 42}}

    # 4. dry run prints and sends nothing
    received.clear()
    result = subprocess.run(
        [sys.executable, ANNOUNCE, "--lang", "fa", "--dry-run", "--test-message", "سلام"],
        capture_output=True,
        text=True,
        env={**os.environ, "TELEGRAM_API": api, "GITHUB_REPOSITORY": "sasoun1366/netpilot"},
        check=False,
    )
    check("dry run exits 0", result.returncode == 0, result.stderr[-200:])
    check("dry run sends nothing", not received)
    check("dry run prints the post", "سلام" in result.stdout and "netpilot" in result.stdout, result.stdout[-200:])

    # 5. both halves of "fa || en" reach their own block in a test post
    received.clear()
    result = subprocess.run(
        [
            sys.executable,
            ANNOUNCE,
            "--lang",
            "both",
            "--token",
            "123:TEST",
            "--chat-id",
            "-1001234567890",
            "--test-message",
            "اتصال برقرار شد || the connection works",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "TELEGRAM_API": api, "GITHUB_REPOSITORY": "sasoun1366/netpilot"},
        check=False,
    )
    check("a bilingual test post is sent", result.returncode == 0 and len(received) == 1, result.stderr[-200:])
    if received:
        text = received[-1]["params"].get("text", "")
        check("both languages in the test post", "اتصال برقرار شد" in text and "the connection works" in text, text[:200])
        check("the repository name, not the owner, is the header", "<b>netpilot</b>" in text, text[:200])

    server.shutdown()
    os.remove(event)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

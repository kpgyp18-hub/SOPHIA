"""G2 A' in the live chat: /attach is granted by Core policy only when opted in."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sophia.runtime.attachment_policy import FLAG_ENV

from .chat_session import ChatSession


class FakeAgent:
    def __init__(self, name: str) -> None:
        self.display_name = name
        self.calls: list[str] = []

    def send(self, task_id: str, prompt: str, *, context: str | None = None) -> str:
        self.calls.append(prompt)
        return "ok"


class Chat:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.output: list[tuple[str, str]] = []
        self.claude, self.codex = FakeAgent("Claude"), FakeAgent("Codex")
        self.session = ChatSession({"Claude": self.claude, "Codex": self.codex},
                                   default_agent="Claude", attachment_root=root,
                                   printer=lambda speaker, text: self.output.append((speaker, text)))

    def say(self, *lines: str) -> None:
        for line in lines:
            self.session.handle_line(line)

    def system(self) -> list[str]:
        return [text for speaker, text in self.output if speaker == "System"]

    def audit(self) -> list[dict]:
        path = self.root / ".sophia" / "permission_audit.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def chat(tmp_path) -> Chat:
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("notes.md", "api_token.txt", "reference image.png"):
        (root / name).write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("o", encoding="utf-8")
    return Chat(root)


def test_default_path_is_unchanged_and_consults_no_policy(chat, monkeypatch) -> None:
    monkeypatch.delenv(FLAG_ENV, raising=False)
    chat.say("/attach api_token.txt", "/attach --approve notes.md", "look")
    assert "attachment queued: api_token.txt" in chat.system()
    assert "attachment rejected: file not found or not a regular file" in chat.system()
    assert str(chat.root / "api_token.txt") in chat.claude.calls[-1]
    assert not (chat.root / ".sophia").exists()


def test_granted_attachment_is_sent_once_and_audited(chat, monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    chat.say('/attach "reference image.png"', "/attach notes.md", "/attach notes.md",
             "inspect these", "next message")
    assert chat.system().count("attachment queued: notes.md") == 1
    assert "attachment already pending: notes.md" in chat.system()
    first, second = chat.claude.calls[-2], chat.claude.calls[-1]
    assert str(chat.root / "reference image.png") in first and str(chat.root / "notes.md") in first
    assert "Attached workspace files" not in second
    assert all(event["allowed"] and event["risk"] == "R0" for event in chat.audit())
    assert {event["supervisor"] for event in chat.audit()} == {"blanc", "aide"}


def test_escalated_attachment_needs_the_users_explicit_approval(chat, monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    chat.say("/attach api_token.txt", "/attachments", "first message")
    rejected = [line for line in chat.system() if line.startswith("attachment rejected by Core policy")]
    assert len(rejected) == 1 and "/attach --approve <path>" in rejected[0]
    assert "no pending attachments" in chat.system()
    assert "Attached workspace files" not in chat.claude.calls[-1]
    assert [(e["risk"], e["allowed"]) for e in chat.audit()] == [("R4", False)]

    chat.say("/attach --approve api_token.txt", "second message")
    assert "attachment queued: api_token.txt" in chat.system()
    assert str(chat.root / "api_token.txt") in chat.claude.calls[-1]
    assert [e["user_approval_granted"] for e in chat.audit()[1:]] == [True, True]

    chat.say("/attach api_token.txt")              # approval was for that one request
    assert len([line for line in chat.system()
                if line.startswith("attachment rejected by Core policy")]) == 2


def test_policy_rejections_queue_nothing(chat, monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    chat.say("/attach ../outside.txt", f"/attach {chat.root.parent / 'outside.txt'}",
             "/attach missing.md", "/attach --approve", "/attachments")
    lines = chat.system()
    assert lines.count("attachment rejected: path must stay inside the chat workspace") == 2
    assert "attachment rejected: file not found or not a regular file" in lines
    assert "usage: /attach --approve <workspace-relative file path>" in lines
    assert "no pending attachments" in lines


def test_explicit_mentions_and_dispatch_are_untouched(chat, monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    chat.say("/attach notes.md", "@codex check the attachment")
    assert len(chat.codex.calls) == 1 and chat.claude.calls == []
    assert str(chat.root / "notes.md") in chat.codex.calls[0]
    chat.say("@both status")
    assert (len(chat.claude.calls), len(chat.codex.calls)) == (1, 2)

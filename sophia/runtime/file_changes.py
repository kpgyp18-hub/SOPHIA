"""Apply explicit, verifiable file artifacts without destructive whole-file edits."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sophia.runtime.tools import ToolGateway

# The right boundary deliberately uses ASCII word characters.  ``\w`` is
# Unicode-aware, so it incorrectly rejects a Korean particle directly after a
# filename (for example ``calculator.py에``).
_PATH = re.compile(r"(?<![A-Za-z0-9_.-])((?:[\w.-]+[\\/])*[\w.-]+\.(?:py|js|ts|json|md|txt|yaml|yml|toml|css|html))(?![A-Za-z0-9_.-])", re.I)
_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class FileChange:
    path: str
    content: str


def requested_path(user_input: str) -> str | None:
    match = _PATH.search(user_input)
    return match.group(1).replace("\\", "/") if match else None


def is_file_followup(user_input: str) -> bool:
    """Identify an explicit follow-up; SophiaCore also accepts prior coding context.

    This deliberately remains a conservative text-only signal for callers which
    do not have session state.  ``SophiaCore.run`` supplies the wider v0.3.2
    continuation decision from its durable prior task context.
    """
    lowered = user_input.lower()
    markers = (
        "then modify", "then fix", "then update", "continue", "apply it",
        "그럼 수정", "수정해", "고쳐", "반영해", "계속",
    )
    return any(marker in lowered for marker in markers)


def _apply_unified_diff(original: str, patch: str) -> str:
    source, output, index = original.splitlines(keepends=True), [], 0
    lines = patch.splitlines(keepends=True)
    try:
        first_hunk = next(position for position, line in enumerate(lines) if line.startswith("@@ "))
    except StopIteration as error:
        raise ValueError("no unified-diff hunk") from error
    position = first_hunk
    while position < len(lines):
        header = _HUNK.match(lines[position].rstrip("\r\n"))
        if header is None:
            raise ValueError("invalid unified-diff hunk")
        old_start = int(header.group(1)) - 1
        output.extend(source[index:old_start])
        index, position = old_start, position + 1
        while position < len(lines) and not lines[position].startswith("@@ "):
            line = lines[position]
            if line.startswith("\\ No newline"):
                position += 1
                continue
            if not line or line[0] not in " +-":
                raise ValueError("invalid unified-diff line")
            payload = line[1:]
            if line[0] == " ":
                if index >= len(source) or source[index].rstrip("\r\n") != payload.rstrip("\r\n"):
                    raise ValueError("diff context does not match current file")
                output.append(source[index]); index += 1
            elif line[0] == "-":
                if index >= len(source) or source[index].rstrip("\r\n") != payload.rstrip("\r\n"):
                    raise ValueError("diff removal does not match current file")
                index += 1
            else:
                output.append(payload if payload.endswith(("\n", "\r")) else payload + "\n")
            position += 1
    output.extend(source[index:])
    return "".join(output)


def apply_file_change(gateway: ToolGateway, user_input: str, response: str, *,
                      target_path: str | None = None) -> FileChange | None:
    """Existing files require a matching unified diff; new files accept one code block."""
    path = target_path or requested_path(user_input)
    blocks = _FENCE.findall(response)
    if path is None or not blocks:
        return None
    target = gateway.resolve(path)
    language, artifact = blocks[0]
    if not artifact.strip():
        return None
    if target.exists():
        if language.strip().lower() not in {"diff", "patch"}:
            raise ValueError("existing files require a unified diff artifact")
        content = _apply_unified_diff(gateway.read_text(path), artifact)
    else:
        content = artifact.rstrip() + "\n"
    gateway.write_text(path, content)
    return FileChange(path, content)

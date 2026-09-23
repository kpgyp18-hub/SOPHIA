from __future__ import annotations

import argparse
import json
import unicodedata

from sophia.brains.registry import load_database_config, load_embedder, load_registry, load_resource_manager_config
from sophia.core.orchestrator import SophiaCore
from sophia.database import DatabaseManager


def build_core(*, interactive: bool = False) -> SophiaCore:
    def approve(description: str) -> bool:
        return input(f"LEVEL 3 approval required — {description}. Continue? [y/N] ").strip().lower() in {"y", "yes"}
    return SophiaCore(load_registry(), approval=approve if interactive else None,
                      database=DatabaseManager(load_database_config()),
                      resource_manager_config=load_resource_manager_config(), embedder=load_embedder())


def status_lines(core: SophiaCore) -> list[str]:
    active = core.resources.active_name or "NONE"
    return ["S.O.P.H.I.A Cognitive OS v0.3.2", "CORE        ONLINE", "BLANC       READY", "AIDE        READY",
            f"Loaded Brain: {active}", f"Configured Brains: {', '.join(core.registry.names())}",
            f"Resources: {core.resources.status()['usage']}", "MODE        AUTO",
            *core.health.status_lines()]


def telemetry_summary(core: SophiaCore) -> str:
    if not core.telemetry_path.is_file():
        return "Current Session\nBrain Calls: 0\nInput Tokens: 0\nOutput Tokens: 0\nRetries: 0"
    events = [json.loads(line) for line in core.telemetry_path.read_text(encoding="utf-8").splitlines() if line]
    return "\n".join(("Current Session", f"Brain Calls: {len(events)}",
                      f"Input Tokens: {sum(event['input_tokens'] for event in events)}",
                      f"Output Tokens: {sum(event['output_tokens'] for event in events)}",
                      f"Retries: {sum(event.get('retry_count', 0) for event in events)}"))


_HELP_ENTRIES = [
    ("/help, /?", "이 명령어 안내를 표시합니다."),
    ("/status, /models, /resources, /health", "코어 상태, 로드된 브레인 목록, 리소스(VRAM/RAM) 사용량을 표시합니다."),
    ("/usage, /telemetry", "이번 세션의 토큰 사용량(호출 수, 입력/출력 토큰, 재시도 횟수)을 표시합니다."),
    ("/brain <이름|auto>", "특정 브레인으로 강제 라우팅하거나 auto로 자동 라우팅으로 되돌립니다."),
    ("/auto", "자동 라우팅으로 되돌립니다. (/brain auto 와 동일)"),
    ("/blanc, /aide", "블랑(추론) 또는 에이드(코더) 역할로 전환합니다."),
    ("/run <메시지>", "REPL을 유지한 채 작업을 한 번 실행합니다."),
    ("/shell <명령>", "셸 명령을 실행합니다."),
    ("/delete <경로>", "파일을 삭제합니다."),
    ("/db <하위명령>", "데이터베이스 관리: status|list|check|backup <경로>|restore <경로>|migrate|switch <postgresql|sqlite>"),
    ("/exit, /quit", "세션을 종료합니다."),
]


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad_display(text: str, width: int) -> str:
    return text + " " * (width - _display_width(text))


def help_text() -> str:
    width = max(_display_width(command) for command, _ in _HELP_ENTRIES)
    lines = ["S.O.P.H.I.A 명령어 안내"]
    lines += [f"  {_pad_display(command, width)}  {description}" for command, description in _HELP_ENTRIES]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="S.O.P.H.I.A Cognitive OS — 로컬 코드/추론 에이전트 CLI")
    parser.add_argument("message", nargs="?", help="실행할 메시지 (생략하면 REPL을 시작합니다).")
    parser.add_argument("--image", help="이미지를 로컬에서 분석하고 결과를 JSON 아티팩트로 저장합니다.")
    parser.add_argument("--vision-output", help="로컬 비전 분석 결과 JSON을 저장할 경로.")
    parser.add_argument("--run", metavar="MESSAGE", help="REPL에 들어가지 않고 작업 메시지 하나만 실행합니다.")
    args = parser.parse_args()
    if args.image:
        core = build_core()
        result = core.inspect_image(args.image, args.vision_output)
        print(f"[Vision] {result['status']} {result['path']}")
        return 0 if result["status"] == "analyzed" else 1
    message = args.run or args.message
    if message:
        core = build_core()
        task, result = core.run(message)
        print(f"[{task.id}] {result.text}")
        return 0
    core = build_core(interactive=True)
    brain_override: str | None = None
    print("S.O.P.H.I.A Cognitive OS — 명령어 목록은 /help 를 입력하세요.")
    while True:
        try:
            text = input("SOPHIA > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return 0
        if text in {"/exit", "/quit"}: return 0
        if text.startswith("/delete "):
            try:
                core.delete_file(text.removeprefix("/delete ").strip())
                print("[Tool] deleted")
            except (OSError, PermissionError) as error:
                print(f"[Tool] denied/failed: {error}")
            continue
        if text.startswith("/shell "):
            try:
                print(core.run_shell(text.removeprefix("/shell ").strip()), end="")
            except (OSError, PermissionError) as error:
                print(f"[Tool] denied/failed: {error}")
            continue
        if text.startswith("/brain "):
            requested = text.removeprefix("/brain ").strip()
            if requested == "auto":
                brain_override = None
                print("[Brain] automatic routing")
            elif requested in core.registry.names():
                brain_override = requested
                print(f"[Brain] override: {requested}")
            else:
                print(f"[Brain] unknown: {requested} (available: {', '.join(core.registry.names())})")
            continue
        if text in {"/help", "/?"}:
            print(help_text())
            continue
        if text in {"/status", "/models", "/resources"}:
            print("\n".join(status_lines(core)))
            continue
        if text == "/health":
            print("\n".join(status_lines(core)))
            continue
        if text.startswith("/run "):
            task, result = core.run(text.removeprefix("/run ").strip(), brain_override=brain_override)
            print(f"[{task.id}] {result.text}")
            continue
        if text.startswith("/db"):
            command = text.removeprefix("/db").strip()
            if command in {"", "status", "list", "check"}:
                print("\n".join(core.database.status_lines()))
            elif command.startswith("backup "):
                print(f"[DB] backup: {core.database.backup(command.removeprefix('backup ').strip())}")
            elif command.startswith("restore "):
                print(f"[DB] restored into {core.database.restore(command.removeprefix('restore ').strip())}")
            elif command.startswith("migrate ") or command.startswith("switch "):
                _, _, target = command.partition(" ")
                try:
                    print("[DB] " + core.database.migrate(target.strip(), "workspace/sophia/db-backup.json"))
                except (RuntimeError, ValueError) as error:
                    print(f"[DB] migration failed; prior backend remains active: {error}")
            else:
                print("usage: /db [status|list|check|backup <path>|restore <path>|migrate|switch <postgresql|sqlite>]")
            continue
        if text in {"/telemetry", "/usage"}:
            print(telemetry_summary(core))
            continue
        if text == "/auto":
            brain_override = None
            print("[Brain] automatic routing")
            continue
        if text in {"/blanc", "/aide"}:
            brain_override = "reasoning" if text == "/blanc" and core.registry.has("reasoning") else "coder" if text == "/aide" else None
            print(f"[Supervisor] {text.removeprefix('/')} mode")
            continue
        if text:
            task, result = core.run(text, brain_override=brain_override)
            print(f"[{task.id}] {result.text}")


if __name__ == "__main__":
    raise SystemExit(main())

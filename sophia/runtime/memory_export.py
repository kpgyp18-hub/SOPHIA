"""One-way, review-first Markdown snapshots of S.O.P.H.I.A runtime memory."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sophia.context.validator import validate_content
from sophia.runtime.memory import MemoryExportRecord
from sophia.security import InjectionDetector, SecretRedactor


_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


@dataclass(frozen=True)
class ExportPolicy:
    output_root: Path = Path("workspace/sophia/memory_exports")


@dataclass(frozen=True)
class ExportRecordResult:
    memory_id: int
    status: str
    redaction_kinds: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ExportReport:
    snapshot_path: Path
    exported: int
    skipped: int
    redacted: int
    records: tuple[ExportRecordResult, ...]


class MemoryRepoExporter:
    """Serialize selected records without importing, committing, or pushing."""

    def __init__(self, policy: ExportPolicy | None = None) -> None:
        self.policy = policy or ExportPolicy()
        self.injection_detector = InjectionDetector()
        self.secret_redactor = SecretRedactor()

    def export_snapshot(self, records: Collection[MemoryExportRecord], *, snapshot_id: str) -> ExportReport:
        self._validate_snapshot_id(snapshot_id)
        selected = sorted(records, key=lambda record: record.id)
        if not selected:
            raise ValueError("at least one memory record is required")
        identifiers = [record.id for record in selected]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("memory record ids must be unique")

        output_root = Path(self.policy.output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        final_path = (output_root / snapshot_id).resolve()
        if final_path.parent != output_root:
            raise ValueError("snapshot path must be a direct child of output_root")
        if final_path.exists():
            raise FileExistsError(f"snapshot already exists: {snapshot_id}")

        rendered_entries: list[tuple[int, str]] = []
        results: list[ExportRecordResult] = []
        for record in selected:
            injection = self.injection_detector.detected_pattern(record.text)
            if injection:
                results.append(ExportRecordResult(
                    memory_id=record.id, status="skipped", reason=f"injection:{injection}",
                ))
                continue
            redaction = self.secret_redactor.redact(record.text)
            rendered = self._render_record(record, redaction.text)
            validate_content(rendered)
            rendered_entries.append((record.id, rendered))
            results.append(ExportRecordResult(
                memory_id=record.id, status="exported", redaction_kinds=redaction.kinds,
            ))

        if not rendered_entries:
            raise ValueError("no safe memory records remain after validation")

        index = self._render_index(snapshot_id, [memory_id for memory_id, _text in rendered_entries])
        validate_content(index)
        manifest = self._build_manifest(snapshot_id, selected, rendered_entries, results, index)
        temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.tmp-", dir=output_root))
        try:
            memories_path = temporary / "memories"
            memories_path.mkdir()
            (temporary / "MEMORY.md").write_text(index, encoding="utf-8", newline="\n")
            for memory_id, rendered in rendered_entries:
                (memories_path / f"{memory_id}.md").write_text(rendered, encoding="utf-8", newline="\n")
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8", newline="\n",
            )
            self._verify_staged_snapshot(temporary, manifest)
            temporary.rename(final_path)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

        exported = len(rendered_entries)
        skipped = len(selected) - exported
        redacted = sum(bool(result.redaction_kinds) for result in results)
        return ExportReport(final_path, exported, skipped, redacted, tuple(results))

    @staticmethod
    def _validate_snapshot_id(snapshot_id: str) -> None:
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            raise ValueError("invalid snapshot_id")
        if snapshot_id.endswith((".", " ")):
            raise ValueError("snapshot_id must not end with a dot or space")

    @staticmethod
    def _render_record(record: MemoryExportRecord, safe_text: str) -> str:
        metadata = (
            ("task_id", record.task_id), ("source_type", record.source_type),
            ("source_id", record.source_id), ("session_id", record.session_id),
            ("supervisor", record.supervisor), ("brain", record.brain), ("provider", record.provider),
            ("confidence", format(record.confidence, ".12g")),
            ("trust_level", record.trust_level), ("importance", format(record.importance, ".12g")),
            ("created_at", record.created_at), ("updated_at", record.updated_at),
            ("version", str(record.version)),
        )
        lines = [f"# Memory {record.id}", ""]
        lines.extend(
            f"- {key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in metadata if value is not None and value != ""
        )
        lines.extend(("", "## Content", "", safe_text.rstrip(), ""))
        return "\n".join(lines)

    @staticmethod
    def _render_index(snapshot_id: str, identifiers: list[int]) -> str:
        lines = [
            "# S.O.P.H.I.A Memory Snapshot", "",
            f"- Snapshot: {snapshot_id}",
            "- Read-only export from the SQLite runtime memory.",
            "- Do not edit this snapshot as a way to update runtime memory.",
            "", "## Index",
        ]
        lines.extend(f"- [[memories/{identifier}]]" for identifier in identifiers)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _build_manifest(self, snapshot_id: str, selected: list[MemoryExportRecord],
                        entries: list[tuple[int, str]], results: list[ExportRecordResult],
                        index: str) -> dict[str, object]:
        files = {"MEMORY.md": self._sha256(index)}
        files.update({f"memories/{memory_id}.md": self._sha256(text) for memory_id, text in entries})
        return {
            "schema_version": "1.0",
            "snapshot_id": snapshot_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "selected": len(selected),
            "exported": len(entries),
            "skipped": len(selected) - len(entries),
            "redacted": sum(bool(result.redaction_kinds) for result in results),
            "exported_ids": [memory_id for memory_id, _text in entries],
            "skipped_records": [
                {"memory_id": result.memory_id, "reason": result.reason}
                for result in results if result.status == "skipped"
            ],
            "redactions": [
                {"memory_id": result.memory_id, "kinds": list(result.redaction_kinds)}
                for result in results if result.redaction_kinds
            ],
            "files": files,
        }

    @staticmethod
    def _verify_staged_snapshot(temporary: Path, manifest: dict[str, object]) -> None:
        files = manifest["files"]
        if not isinstance(files, dict):
            raise RuntimeError("invalid manifest file map")
        for relative, expected_hash in files.items():
            path = temporary / str(relative)
            if not path.is_file():
                raise RuntimeError(f"missing staged export file: {relative}")
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise RuntimeError(f"staged export hash mismatch: {relative}")

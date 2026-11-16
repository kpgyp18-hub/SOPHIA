"""Database-backed runtime source of truth for validated context packs."""
from __future__ import annotations

import hashlib

from sophia.context.loader import ContextLoader
from sophia.context.models import ContextPack, ContextStatus, ContextTrust
from sophia.context.validator import ContextValidationError, semantic_version, validate_pack
from sophia.database import DatabaseManager


class ContextRegistry:
    KIND = "context_pack"

    def __init__(self, database: DatabaseManager) -> None:
        self.database = database

    def register(self, pack: ContextPack) -> ContextPack:
        validate_pack(pack)
        if pack.status is ContextStatus.ACTIVE and pack.trust in {
            ContextTrust.MODEL_GENERATED_CANDIDATE, ContextTrust.UNTRUSTED,
        }:
            raise PermissionError("untrusted context must remain quarantined")
        current = self.get(pack.context_id, include_inactive=True)
        if current is not None:
            if semantic_version(pack.version) < semantic_version(current.version):
                raise ContextValidationError(f"context downgrade rejected: {current.version} -> {pack.version}")
            if pack.version == current.version:
                if pack.content_hash != current.content_hash:
                    raise ContextValidationError("same-version context hash conflict")
                return current
        self.database.save_goal_runtime_record(
            self.KIND, pack.context_id, pack.to_record(), status=pack.status.value,
        )
        return pack

    def sync(self, loader: ContextLoader) -> list[ContextPack]:
        return [self.register(pack) for pack in loader.load()]

    def get(self, context_id: str, *, include_inactive: bool = False) -> ContextPack | None:
        record = self.database.get_goal_runtime_record(self.KIND, context_id)
        if record is None:
            return None
        pack = ContextPack.from_record(record)
        if not include_inactive and pack.status is not ContextStatus.ACTIVE:
            return None
        return pack

    def list(self, *, status: ContextStatus | None = ContextStatus.ACTIVE) -> list[ContextPack]:
        records = self.database.list_goal_runtime_records(
            self.KIND, status=status.value if status is not None else None,
        )
        return sorted((ContextPack.from_record(record) for record in records), key=lambda pack: pack.context_id)

    def fingerprint(self) -> str:
        state = "\n".join(
            f"{pack.context_id}:{pack.version}:{pack.content_hash}:{pack.status.value}"
            for pack in self.list(status=None)
        )
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

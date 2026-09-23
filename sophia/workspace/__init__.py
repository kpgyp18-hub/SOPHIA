"""Checkpoint-backed workspace modification primitives."""

from sophia.workspace.checkpoint import Checkpoint, CheckpointType
from sophia.workspace.manager import WorkspaceManager
from sophia.workspace.rollback import rollback

__all__ = ["Checkpoint", "CheckpointType", "WorkspaceManager", "rollback"]

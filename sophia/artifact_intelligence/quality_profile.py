"""Declarative artifact-quality profiles and deterministic verdicts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from sophia.context.validator import semantic_version


class QualityVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class QualityEvaluation:
    profile_id: str
    score: float
    verdict: QualityVerdict
    hard_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityProfile:
    profile_id: str
    version: str
    dimensions: dict[str, float]
    minimum_pass: float
    uncertain_min: float
    uncertain_max: float
    hard_fail: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        semantic_version(self.version)
        if not self.profile_id.startswith("quality."):
            raise ValueError("quality_profile_id must start with quality.")
        if not self.dimensions or any(weight <= 0 for weight in self.dimensions.values()):
            raise ValueError("quality dimensions require positive weights")
        if abs(sum(self.dimensions.values()) - 1.0) > 1e-6:
            raise ValueError("quality dimension weights must sum to 1.0")
        if not 0 <= self.uncertain_min <= self.uncertain_max < self.minimum_pass <= 1:
            raise ValueError("invalid quality thresholds")

    def evaluate(self, scores: dict[str, float], *, failures: tuple[str, ...] = ()) -> QualityEvaluation:
        unknown_failures = sorted(set(failures) - set(self.hard_fail))
        if unknown_failures:
            raise ValueError(f"unknown hard-failure codes: {', '.join(unknown_failures)}")
        missing = sorted(set(self.dimensions) - scores.keys())
        if missing:
            raise ValueError(f"missing quality dimensions: {', '.join(missing)}")
        if any(not 0 <= float(scores[name]) <= 1 for name in self.dimensions):
            raise ValueError("quality scores must be between 0 and 1")
        score = sum(float(scores[name]) * weight for name, weight in self.dimensions.items())
        if failures:
            verdict = QualityVerdict.FAIL
        elif score >= self.minimum_pass:
            verdict = QualityVerdict.PASS
        elif self.uncertain_min <= score < self.minimum_pass:
            verdict = QualityVerdict.UNCERTAIN
        else:
            verdict = QualityVerdict.FAIL
        return QualityEvaluation(self.profile_id, score, verdict, failures)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "QualityProfile":
        band = payload.get("uncertain_band", {})
        dimensions = payload.get("dimensions", {})
        if not isinstance(dimensions, dict) or not isinstance(band, dict):
            raise ValueError("quality dimensions and uncertain_band must be mappings")
        return cls(
            profile_id=str(payload["quality_profile_id"]), version=str(payload["version"]),
            dimensions={str(name): float(weight) for name, weight in dimensions.items()},
            minimum_pass=float(payload["minimum_pass"]), uncertain_min=float(band["min"]),
            uncertain_max=float(band["max"]), hard_fail=tuple(payload.get("hard_fail", ())),
        )


class QualityProfileLoader:
    def __init__(self, root: str | Path = "sophia/quality_profiles") -> None:
        self.root = Path(root)

    def load(self) -> dict[str, QualityProfile]:
        profiles: dict[str, QualityProfile] = {}
        if not self.root.exists():
            return profiles
        for path in sorted(self.root.glob("**/*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(payload, dict) or str(payload.get("schema_version")) != "1.0":
                raise ValueError(f"invalid quality profile: {path}")
            profile = QualityProfile.from_mapping(payload)
            if profile.profile_id in profiles:
                raise ValueError(f"duplicate quality profile: {profile.profile_id}")
            profiles[profile.profile_id] = profile
        return profiles

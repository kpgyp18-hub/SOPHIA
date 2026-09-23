"""Task-local model-call budgets."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class ModelBudget:
    max_luna_calls: int = 10
    max_terra_calls: int = 6
    max_sol_calls: int = 2
    max_model_escalations: int = 3
    max_same_model_retries: int = 2
    escalation_threshold: int = 2
    luna_calls: int = 0
    terra_calls: int = 0
    sol_calls: int = 0
    model_escalations: int = 0
    same_model_retries: int = 0

    def can_execute(self, tier: str, *, global_budget_pass: bool) -> bool:
        if not global_budget_pass:
            return False
        return {"LUNA": self.luna_calls < self.max_luna_calls, "TERRA": self.terra_calls < self.max_terra_calls,
                "SOL": self.sol_calls < self.max_sol_calls}.get(tier, False)

    def record_call(self, tier: str, prior_model: str = "NONE") -> None:
        if not self.can_execute(tier, global_budget_pass=True):
            raise RuntimeError("model budget exhausted")
        attribute = {"LUNA": "luna_calls", "TERRA": "terra_calls", "SOL": "sol_calls"}[tier]
        setattr(self, attribute, getattr(self, attribute) + 1)
        if _rank(tier) > _rank(prior_model):
            if self.model_escalations >= self.max_model_escalations:
                raise RuntimeError("model escalation budget exhausted")
            self.model_escalations += 1

    def record_same_model_retry(self) -> bool:
        if self.same_model_retries >= self.max_same_model_retries:
            return False
        self.same_model_retries += 1
        return True

def _rank(tier: str) -> int:
    return {"NONE": 0, "LUNA": 1, "TERRA": 2, "SOL": 3}.get(tier, -1)
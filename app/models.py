from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class PurchaseRequest(BaseModel):
    request_id: str
    requester_id: str
    department: str
    description: str
    amount: float = Field(gt=0)
    currency: str = "USD"
    cost_center: str
    supplier_id: str
    category: str
    urgency: Literal["standard", "urgent"] = "standard"

class Evidence(BaseModel):
    source_id: str
    title: str
    owner: str
    version: str
    effective_date: str
    expiry_date: str | None = None
    classification: str
    access_scope: str
    excerpt: str
    score: float = 0

class AnalysisResult(BaseModel):
    request_id: str
    status: Literal["pending_human_review", "blocked"]
    summary: str
    classification: str
    policy_findings: list[str]
    budget_findings: list[str]
    supplier_findings: list[str]
    exceptions: list[str]
    recommended_approver_role: str | None
    evidence: list[Evidence]
    next_action: str
    trace_id: str

class DecisionInput(BaseModel):
    decision: Literal["approve", "reject", "request_changes"]
    rationale: str = Field(min_length=8, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)

class DecisionRecord(BaseModel):
    request_id: str
    approver_id: str
    decision: str
    rationale: str
    decided_at: datetime

class EvalScenario(BaseModel):
    scenario_id: str
    title: str
    request_id: str
    expected_outcome: str
    checks: list[str]
    expected_approver_role: str | None = None


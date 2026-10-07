"""Tools that drive the state machine rather than the business: they exist only inside the agent."""

from pydantic import BaseModel, ConfigDict, Field


class ProposeDecisionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: str | None = Field(None, description="id from generate_options; omit only when investigate=true")
    investigate: bool = Field(False, description="true when the data cannot support a decision")
    reasons: list[str] = Field(default_factory=list, description="the evidence behind the choice, short")
    information_needed: list[str] = Field(default_factory=list, description="if investigating: what would settle it")


class FinishExecutionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field("", description="one line on what was executed")


CONTROL_TOOLS = {
    "propose_decision": (ProposeDecisionArgs,
                         "Record your decision: choose one option_id from generate_options, or investigate=true."),
    "finish_execution": (FinishExecutionArgs, "Call when the decided option has been fully executed."),
}

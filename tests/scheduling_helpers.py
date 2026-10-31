from flight_agent.config import SchedulerSettings
from flight_agent.runtime.capabilities import Capability, CapabilityRegistry
from flight_agent.runtime.capability_metadata import CapabilityMetadata, MetricDefinition
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.scheduling import ExecutionScheduler
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.coordinator import EngineeringRuntime
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import RmseInput, analysis_plan_data


def make_runtime(tmp_path, planner, operation, *, lane="read_only", settings=None):
    settings = settings or SchedulerSettings()
    effect = "read_only" if lane == "read_only" else "execute_project"
    tools = CapabilityRegistry(agent_mode=True)
    tools.register(
        Capability(
            name="offline_rmse",
            version="1",
            description="Fake scheduled metric.",
            input_model=RmseInput,
            output_model=CapabilityOutcome,
            effect=effect,
            operation=operation,
            scheduling=SchedulingMetadata(lane),
            metadata=CapabilityMetadata(
                metrics=[
                    MetricDefinition(id="rmse", label="RMSE", unit="m"),
                ]
            ),
        )
    )
    resources = ResourceRegistry()
    store = ConversationStore(tmp_path / "state")
    executor = AgentExecutor(
        store, tools, resources, scheduler=ExecutionScheduler(settings)
    )
    runtime = EngineeringRuntime(store, planner, executor, resources)
    return runtime


def propose_fake(runtime, *, lane="read_only", data=None):
    from flight_agent.runtime.agent_contracts import AgentPlan

    data = analysis_plan_data() if data is None else data
    data["allowed_effects"] = [
        "read_only" if lane == "read_only" else "execute_project"
    ]
    record = runtime.store.create()
    return runtime.executor.propose(record, AgentPlan.model_validate(data))

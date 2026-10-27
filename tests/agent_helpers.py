import math
from typing import Annotated, Self

from pydantic import Field, model_validator

from flight_agent.models import FiniteNumber
from flight_agent.runtime.resources import AgentContract


class RmseInput(AgentContract):
    observed: Annotated[list[FiniteNumber], Field(min_length=1, max_length=128)]
    reference: Annotated[list[FiniteNumber], Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def aligned(self) -> Self:
        if len(self.observed) != len(self.reference):
            raise ValueError("RMSE arrays must have equal lengths")
        return self


def register_rmse(registry, calls, *, version="1", unit="m", operation=None):
    from flight_agent.runtime.capabilities import Capability
    from flight_agent.runtime.capability_metadata import CapabilityMetadata, MetricDefinition
    from flight_agent.runtime.observations import CapabilityOutcome, Observation

    def measure(context, inputs):
        calls.append(context.call_id)
        value = math.sqrt(
            sum(
                (observed - reference) ** 2
                for observed, reference in zip(inputs.observed, inputs.reference, strict=True)
            )
            / len(inputs.observed)
        )
        return CapabilityOutcome(
            observations=[
                Observation(
                    node_id=context.node_id, metric_id="rmse", label="RMSE", value=value, unit=unit
                )
            ]
        )

    registry.register(
        Capability(
            name="offline_rmse",
            version=version,
            description="Measure RMSE for supplied synthetic arrays.",
            input_model=RmseInput,
            output_model=CapabilityOutcome,
            effect="read_only",
            operation=operation or measure,
            metadata=CapabilityMetadata(
                metrics=[MetricDefinition(id="rmse", label="RMSE", unit=unit)],
                validity_policy={
                    "id": "aligned-finite-arrays",
                    "version": "1",
                    "basis": "Equal-length finite arrays without interpolation.",
                },
                cacheable=True,
            ),
        )
    )


def analysis_plan_data():
    return {
        "title": "Synthetic RMSE",
        "rationale": "Analyze explicitly supplied synthetic arrays.",
        "requirements": {
            "objective": "Report RMSE without a compliance decision.",
            "mode": "analysis",
            "requested_outputs": [
                {
                    "id": "rmse-output",
                    "kind": "metric",
                    "node_id": "compute",
                    "item_id": "rmse",
                    "description": "Report the synthetic RMSE.",
                }
            ],
            "criteria": [],
            "overall_requested": False,
            "delivery_formats": [],
        },
        "resources": [],
        "nodes": [
            {
                "id": "compute",
                "tool": "offline_rmse",
                "tool_version": "1",
                "inputs": {"observed": [0.0, 2.0], "reference": [0.0, 0.0]},
            }
        ],
        "allowed_effects": ["read_only"],
        "budget": {
            "max_rounds": 3,
            "max_calls": 6,
            "max_seconds": 60,
            "max_no_progress": 2,
        },
    }


def criterion_data(criterion_id="rmse-limit", **changes):
    return {
        "id": criterion_id,
        "metric": {"node_id": "compute", "metric_id": "rmse"},
        "operator": "<=",
        "threshold": 2.0,
        "unit": "m",
        "conditions": {"case": "synthetic"},
        "source": {
            "kind": "user_message",
            "reference": "message-1",
            "quote": "Require synthetic RMSE at most 2 m.",
        },
        **changes,
    }


def verification_plan_data():
    data = analysis_plan_data()
    data["requirements"].update(
        mode="verification",
        criteria=[criterion_data()],
        overall_requested=True,
    )
    data["nodes"].append(
        {
            "id": "evaluate",
            "tool": "evaluate_criteria",
            "tool_version": "1",
            "depends_on": ["compute"],
            "bindings": {"observations": {"node_id": "compute", "path": ["observations"]}},
        }
    )
    data["evaluation_node"] = "evaluate"
    return data


def resource_data(**changes):
    return {
        "id": "measurements",
        "kind": "signal_data",
        "location": "uploads",
        "locator": "/server/private/signals.csv",
        "fingerprint": "a" * 64,
        "version": None,
        "metadata": {"unit": "m"},
        **changes,
    }


def gap_data(**changes):
    return {
        "requirement_id": "rmse-output",
        "missing_capability": "offline_rmse",
        "missing_inputs": ["observed"],
        "limitation": "RMSE cannot be delivered; only other supplied outputs are in scope.",
        "next_step": "Supply the observed array and register the RMSE capability.",
        **changes,
    }


def observation_data(**changes):
    return {
        "node_id": "compute",
        "metric_id": "rmse",
        "label": "Synthetic RMSE",
        "value": 1.0,
        "unit": "m",
        "conditions": {"case": "synthetic"},
        "evidence": ["measurements.json"],
        **changes,
    }


def check_data(**changes):
    return {
        "criterion_id": "rmse-limit",
        "metric": {"node_id": "compute", "metric_id": "rmse"},
        "value": 1.0,
        "value_unit": "m",
        "operator": "<=",
        "threshold": 2.0,
        "unit": "m",
        "status": "pass",
        "reason": "The value meets the criterion.",
        "evidence": ["measurements.json"],
        **changes,
    }


def invocation_spec_data():
    return {
        "capability": "offline_rmse",
        "capability_version": "1",
        "inputs": {"observed": [0.0, 2.0], "reference": [0.0, 0.0]},
        "resource_fingerprints": {"measurements": "a" * 64},
        "conditions": {"case": "synthetic"},
        "numerics": {"alignment": "none"},
        "dependency_output_sha256": {"prepare": "b" * 64},
    }


def make_agent_web(tmp_path, decisions):
    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.verification.adapters.delivery import delivery_capability
    from flight_agent.verification.adapters.evaluation import scalar_evaluation_capability
    from flight_agent.verification.agent_planner import AgentPlanner
    from flight_agent.verification.agent_runtime import AgentExecutor
    from flight_agent.verification.coordinator import EngineeringRuntime
    from flight_agent.verification.resources import ResourceRegistry
    from flight_agent.verification.store import ConversationStore
    from flight_agent.web import create_web_app
    from tests.helpers import ScriptedModel, tool_message

    calls = []
    tools = CapabilityRegistry(agent_mode=True)
    register_rmse(tools, calls)
    tools.register(scalar_evaluation_capability())
    tools.register(delivery_capability())
    resources = ResourceRegistry()
    store = ConversationStore(tmp_path / "state")
    planner = AgentPlanner(
        model=ScriptedModel(responses=[tool_message("propose_agent", item) for item in decisions])
    )
    executor = AgentExecutor(store, tools, resources)
    runtime = EngineeringRuntime(store, planner, executor, resources, legacy=None)
    return create_web_app(tmp_path, engineering_runtime=runtime), runtime, calls, tools


def wait_for_conversation(client, conversation_id, headers, ready):
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/conversations/{conversation_id}", headers=headers)
        assert response.status_code == 200, response.text
        record = response.json()
        if ready(record):
            return record
        if record["status"] == "failed":
            raise AssertionError(record["error"])
        time.sleep(0.01)
    raise AssertionError("Conversation did not reach the required state")


def draft_data():
    data = analysis_plan_data()
    data.pop("resources")
    data["resource_ids"] = []
    return data


def delivery_draft(*, source=None):
    data = draft_data()
    requested = data["requirements"]
    requested["delivery_formats"] = ["json"] if source is None else ["json", "docx"]
    dependencies = ["compute"]
    if source is not None:
        requested.update(
            mode="verification",
            overall_requested=True,
            criteria=[criterion_data(threshold=0.8, conditions={}, source=source)],
        )
        data["nodes"].append(
            {
                "id": "evaluate",
                "tool": "evaluate_criteria",
                "tool_version": "1",
                "depends_on": ["compute"],
                "inputs": {"criteria": requested["criteria"], "overall_requested": True},
                "bindings": {"observations": {"node_id": "compute", "path": ["observations"]}},
            }
        )
        data["evaluation_node"] = "evaluate"
        dependencies.append("evaluate")
    requested["requested_outputs"].extend(
        {
            "id": f"report-{format}",
            "kind": "artifact",
            "node_id": "deliver",
            "item_id": format,
            "description": f"Deliver {format}.",
        }
        for format in requested["delivery_formats"]
    )
    data["allowed_effects"].append("write_delivery")
    data["nodes"].append(
        {
            "id": "deliver",
            "tool": "deliver_results",
            "tool_version": "1",
            "depends_on": dependencies,
            "inputs": {
                "title": "Synthetic array measurement",
                "formats": requested["delivery_formats"],
                "requirements": requested,
            },
        }
    )
    return data


def register_peak_current(registry, calls):
    from flight_agent.runtime.capabilities import Capability
    from flight_agent.runtime.capability_metadata import CapabilityMetadata, MetricDefinition
    from flight_agent.runtime.observations import CapabilityOutcome, Observation

    class Samples(AgentContract):
        samples: Annotated[list[FiniteNumber], Field(min_length=1, max_length=128)]

    def measure(context, inputs):
        calls.append(context.call_id)
        return CapabilityOutcome(
            observations=[
                Observation(
                    node_id=context.node_id,
                    metric_id="peak_current",
                    label="Peak current",
                    value=max(abs(value) for value in inputs.samples),
                    unit="A",
                ),
            ]
        )

    registry.register(
        Capability(
            name="sample_peak",
            version="7",
            description="Peak magnitude of synthetic current samples.",
            input_model=Samples,
            output_model=CapabilityOutcome,
            effect="read_only",
            operation=measure,
            metadata=CapabilityMetadata(
                metrics=[MetricDefinition(id="peak_current", label="Peak current", unit="A")],
                validity_policy={"id": "finite-samples", "version": "1"},
            ),
        )
    )


def peak_current_draft():
    data = draft_data()
    data["title"] = "Synthetic current"
    data["requirements"]["objective"] = "Analyze synthetic current samples without acceptance."
    data["requirements"]["requested_outputs"] = [
        {
            "id": "peak",
            "kind": "metric",
            "node_id": "measure",
            "item_id": "peak_current",
            "description": "Report peak current in A.",
        }
    ]
    data["nodes"] = [
        {
            "id": "measure",
            "tool": "sample_peak",
            "tool_version": "7",
            "inputs": {"samples": [1.0, -3.0, 2.0]},
        }
    ]
    return data

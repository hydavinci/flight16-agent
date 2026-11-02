from dataclasses import dataclass
from pathlib import Path

from .config import SchedulerSettings
from .runtime.capabilities import CapabilityRegistry
from .runtime.scheduling import (
    ExecutionScheduler,
    ExecutionTurnScheduler,
    PlanningScheduler,
)
from .verification.adapters.local_matlab import LOCAL_MATLAB_CAPABILITY_NAMES
from .verification.agent_planner import AgentPlanner
from .verification.agent_runtime import AgentContext, AgentExecutor
from .verification.backend import AnalysisBackend, RemoteSimulinkBackend
from .verification.coordinator import EngineeringRuntime
from .verification.diagnostic_planner import DiagnosticReviewer
from .verification.local_matlab import LocalMatlabBackend
from .verification.plugins import register_builtin_plugins
from .verification.project_snapshots import ProjectSnapshotStore
from .verification.resources import ResourceRegistry
from .verification.routes import ConversationManager
from .verification.sqlite_store import SQLiteConversationStore


@dataclass(frozen=True)
class ApplicationServices:
    runtime: EngineeringRuntime
    manager: ConversationManager
    resources: ResourceRegistry
    local_backend: LocalMatlabBackend | None
    backend: AnalysisBackend | None
    owns_manager: bool

    def close(self) -> None:
        if self.owns_manager:
            self.manager.close()


def _bind_local_matlab_backend(
    backend: LocalMatlabBackend,
    snapshots: ProjectSnapshotStore,
) -> LocalMatlabBackend:
    if backend.snapshots is None:
        backend.snapshots = snapshots
    elif backend.snapshots is not snapshots:
        raise ValueError("Local MATLAB backend and executor must share one snapshot store")
    return backend


def _registered_local_matlab_backend(
    runtime: EngineeringRuntime,
) -> LocalMatlabBackend | None:
    tools = runtime.executor.tools
    registered = [
        name for name in LOCAL_MATLAB_CAPABILITY_NAMES if tools.registered(name)
    ]
    if not registered:
        return runtime.local_matlab_backend
    owners = []
    for name in registered:
        owner = tools.runtime_owner(name, LocalMatlabBackend)
        if owner is None:
            raise ValueError(
                f"Registered local MATLAB capability requires its backend owner: {name}"
            )
        owners.append(owner)
    registered_owner = owners[0]
    if any(owner is not registered_owner for owner in owners[1:]):
        raise ValueError(
            "Registered local MATLAB capabilities must share the same backend owner"
        )
    declared_owner = runtime.local_matlab_backend
    if declared_owner is not None and declared_owner is not registered_owner:
        raise ValueError(
            "Declared local MATLAB backend differs from the registered backend owner"
        )
    return registered_owner


def build_application(
    root: Path,
    *,
    engineering_runtime: EngineeringRuntime | None = None,
    scheduler_settings: SchedulerSettings | None = None,
    conversation_manager: ConversationManager | None = None,
) -> ApplicationServices:
    if conversation_manager is not None and (
        engineering_runtime is None
        or conversation_manager.runtime is not engineering_runtime
    ):
        raise ValueError("Injected manager and runtime must be the same objects")
    settings = (
        scheduler_settings
        if scheduler_settings is not None
        else (
            engineering_runtime.executor.scheduler.settings
            if engineering_runtime is not None
            else SchedulerSettings.from_environment()
        )
    )
    if (
        engineering_runtime is not None
        and engineering_runtime.executor.scheduler.settings != settings
    ):
        raise ValueError("Injected runtime and Web scheduler settings must match")

    resources = (
        engineering_runtime.resources
        if engineering_runtime is not None
        else ResourceRegistry()
    )
    try:
        backend: AnalysisBackend | None = RemoteSimulinkBackend.from_environment()
    except ValueError:
        backend = None

    local_backend: LocalMatlabBackend | None
    if engineering_runtime is None:
        conversations = SQLiteConversationStore(root / "runs" / "verification")
        snapshots = ProjectSnapshotStore(conversations.root / "snapshots")
        local_backend = _bind_local_matlab_backend(
            LocalMatlabBackend.from_environment(),
            snapshots,
        )
        tools: CapabilityRegistry[AgentContext] = CapabilityRegistry(agent_mode=True)
        register_builtin_plugins(
            tools,
            resources,
            backend=backend,
            diagnostic_reviewer=DiagnosticReviewer(),
            local_backend=local_backend,
        )
        engineering_runtime = EngineeringRuntime(
            conversations,
            AgentPlanner(),
            AgentExecutor(
                conversations,
                tools,
                resources,
                scheduler=ExecutionScheduler(settings),
                snapshots=snapshots,
            ),
            resources,
            local_matlab_backend=local_backend,
        )
    else:
        snapshots = engineering_runtime.executor.snapshots
        owned_local_backend = _registered_local_matlab_backend(engineering_runtime)
        local_backend = (
            _bind_local_matlab_backend(
                owned_local_backend,
                snapshots,
            )
            if owned_local_backend is not None
            else None
        )

    manager = (
        conversation_manager
        if conversation_manager is not None
        else ConversationManager(
            engineering_runtime,
            PlanningScheduler(settings.planning_workers, settings.planning_queue),
            ExecutionTurnScheduler(settings.execution_turns),
        )
    )
    return ApplicationServices(
        runtime=engineering_runtime,
        manager=manager,
        resources=resources,
        local_backend=local_backend,
        backend=backend,
        owns_manager=conversation_manager is None,
    )

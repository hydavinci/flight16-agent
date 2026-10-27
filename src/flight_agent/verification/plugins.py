from ..directories import DirectoryListing, browse_directories
from ..runtime.capabilities import CapabilityRegistry
from ..runtime.capability_metadata import Availability
from ..runtime.contracts import fingerprint
from ..runtime.resources import ResourceReference
from .adapters.delivery import delivery_capability
from .adapters.diagnostics import diagnostic_capability
from .adapters.evaluation import scalar_evaluation_capability
from .adapters.local_matlab import local_matlab_capabilities
from .adapters.simulink import simulink_capabilities
from .agent_runtime import AgentContext
from .backend import AnalysisBackend, RemoteSimulinkBackend
from .diagnostic_data import inspect_diagnostic_project
from .local_matlab import LocalMatlabBackend
from .matlab_projects import discover_matlab_project
from .planner import VerificationPlanner
from .resources import ResourceAdapter, ResourceRegistry


def register_builtin_plugins(
    tools: CapabilityRegistry[AgentContext],
    resources: ResourceRegistry,
    *,
    backend: AnalysisBackend | None,
    planner: VerificationPlanner,
    local_backend: LocalMatlabBackend | None = None,
) -> None:
    def inspect_local_project(locator: str) -> ResourceReference:
        project = discover_matlab_project(locator)
        return ResourceReference(
            id="resource-" + fingerprint(["matlab_project", "agent", project.root])[:24],
            kind="matlab_project",
            location="agent",
            locator=project.root,
            fingerprint=project.fingerprint,
            version="1",
            metadata={"project": project.model_dump(mode="json")},
        )

    def verify_local_project(resource: ResourceReference) -> None:
        if inspect_local_project(resource.locator) != resource:
            raise ValueError("Local project resource fingerprint or metadata changed")

    def inspect_project(locator: str) -> ResourceReference:
        if backend is None:
            raise ValueError("A configured Simulink Worker is required")
        project = backend.inspect(locator)
        return ResourceReference(
            id="resource-" + fingerprint(["simulink_project", "worker", project.root])[:24],
            kind="simulink_project",
            location="worker",
            locator=project.root,
            fingerprint=project.fingerprint,
            metadata={
                "name": project.config.name,
                "config": project.config.model_dump(mode="json"),
                "warnings": [warning for warning in project.warnings],
            },
        )

    def verify_project(resource: ResourceReference) -> None:
        if inspect_project(resource.locator) != resource:
            raise ValueError("Project resource fingerprint or metadata changed")

    def browse_project(locator: str) -> DirectoryListing:
        if not isinstance(backend, RemoteSimulinkBackend):
            raise ValueError("Worker directory browsing is unavailable for this backend")
        return backend.browse(locator)

    def inspect_diagnostic(locator: str) -> ResourceReference:
        project = inspect_diagnostic_project(locator)
        return ResourceReference(
            id="resource-" + fingerprint(["diagnostic_data", "agent", project.root])[:24],
            kind="diagnostic_data",
            location="agent",
            locator=project.root,
            fingerprint=project.fingerprint,
            metadata={
                "summary": project.summary,
                "manifest": project.manifest.model_dump(mode="json"),
            },
        )

    def verify_diagnostic(resource: ResourceReference) -> None:
        if inspect_diagnostic(resource.locator) != resource:
            raise ValueError("Diagnostic resource fingerprint or metadata changed")

    resources.register(
        ResourceAdapter(
            name="matlab_project",
            kind="matlab_project",
            location="agent",
            inspect=inspect_local_project,
            verify=verify_local_project,
            browse=browse_directories,
            display_name="本机 MATLAB / Simulink 工程",
            local=True,
        )
    )
    resources.register(
        ResourceAdapter(
            name="simulink_project",
            kind="simulink_project",
            location="worker",
            inspect=inspect_project,
            verify=verify_project,
            browse=browse_project,
            display_name="Simulink project on Worker",
            availability=Availability(state="available")
            if backend is not None
            else Availability(
                state="unavailable",
                reason="A configured Simulink Worker is required",
            ),
        )
    )
    resources.register(
        ResourceAdapter(
            name="diagnostic_data",
            kind="diagnostic_data",
            location="agent",
            inspect=inspect_diagnostic,
            verify=verify_diagnostic,
            browse=browse_directories,
            display_name="Registered offline diagnostic data",
            local=True,
        )
    )
    for capability in simulink_capabilities(backend):
        tools.register(capability)
    for local_capability in local_matlab_capabilities(
        local_backend if local_backend is not None else LocalMatlabBackend.from_environment()
    ):
        tools.register(local_capability)
    tools.register(scalar_evaluation_capability())
    tools.register(delivery_capability())
    tools.register(diagnostic_capability(planner))

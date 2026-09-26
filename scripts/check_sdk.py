"""Read-only, offline capability probe. Does not create a client or inspect secrets."""
import inspect
from importlib.metadata import version
import json
import platform


def capabilities():
    import agent_framework as af
    from agent_framework import observability
    from agent_framework._harness._agent import _assemble_compaction

    parameters = inspect.signature(af.create_harness_agent).parameters
    required = ["SkillsProvider", "ContextProvider", "AgentMiddleware", "ChatMiddleware", "FunctionMiddleware"]
    features = {name: hasattr(af, name) for name in required}
    features.update({name: name in parameters for name in ("skills_provider", "skills_paths", "max_context_window_tokens",
                                                          "max_output_tokens", "middleware", "otel_provider_name")})
    # Read the installed implementation, rather than assuming a supplied limit enabled compaction.
    source = inspect.getsource(_assemble_compaction)
    return {"python": platform.python_version(),
            "dependencies": {n: version(n) for n in ("agent-framework", "agent-framework-core", "pydantic", "openai",
                                                     "opentelemetry-api", "opentelemetry-sdk")},
            "features": features,
            "skills_signature": str(inspect.signature(af.SkillsProvider.from_paths)),
            "tool_signature": str(inspect.signature(af.tool)),
            "otel_setup_available": hasattr(observability, "configure_otel_providers"),
            "compaction_factory_source": source}


if __name__ == "__main__":
    result = capabilities()
    print(json.dumps(result, indent=2))
    if not all(result["features"].values()):
        raise SystemExit("Some planned SDK features are missing; do not install automatically.")

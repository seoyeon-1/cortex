"""Phase 6.5 - Agent Registry (sub-agent marketplace).

Sub-agents are packaged like skills, but heavier: a versioned manifest declares the class
entrypoint, capabilities, dedicated tools, validation bar and routing keywords. The
Orchestrator only does Decompose -> Route -> Synthesize; personas come from this registry
(and can run out-of-process through `registry.server` JSON-RPC)."""

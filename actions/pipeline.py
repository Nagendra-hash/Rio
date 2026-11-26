"""
Pipeline skill: compose multiple actions into a single reusable workflow.

Usage:
    TOOL = {
        "name": "my_pipeline",
        "description": "Run a sequence of actions",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "steps": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "tool": {"type": "STRING"},
                            "params": {"type": "OBJECT"},
                            "output_var": {"type": "STRING"}  # optional: store result for next step
                        },
                        "required": ["tool"]
                    }
                }
            },
            "required": ["steps"]
        },
        "handler": run_pipeline,
    }

Each step can reference previous step outputs using {{var_name}} in params.
"""
from typing import Any, Dict, List, Optional


def _interpolate_params(params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Replace {{var}} placeholders in params with values from context."""
    import json
    import copy

    def _recursive_interpolate(obj):
        if isinstance(obj, dict):
            return {k: _recursive_interpolate(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_recursive_interpolate(v) for v in obj]
        elif isinstance(obj, str):
            result = obj
            for key, value in context.items():
                placeholder = f"{{{{{key}}}}}"
                if placeholder in result:
                    if isinstance(value, (dict, list)):
                        # For complex types, we can't easily interpolate into string
                        # Just use JSON representation
                        result = result.replace(placeholder, json.dumps(value, ensure_ascii=False))
                    else:
                        # Sanitize string value
                        safe_value = str(value).replace('\x00', '').replace('\r', ' ')
                        result = result.replace(placeholder, safe_value)
            return result
        else:
            return obj

    return _recursive_interpolate(copy.deepcopy(params))


def run_pipeline(
    parameters: dict,
    player=None,
    speak=None,
    response=None,
    session_memory=None,
    dashboard=None
) -> str:
    """Execute a pipeline of actions sequentially."""
    from core.action_loader import discover_actions
    from pathlib import Path

    steps = parameters.get("steps", [])
    if not steps:
        return "Pipeline: no steps provided"

    # Get action registry (we need to access it from main.py's JarvisLive instance)
    # For now, we'll re-discover actions. In production, this would use the live registry.
    actions_dir = Path(__file__).parent
    registry = discover_actions(actions_dir, reserved_names=set())

    context = {}
    results = []

    for i, step in enumerate(steps):
        tool_name = step.get("tool")
        raw_params = step.get("params", {})
        output_var = step.get("output_var")

        # Interpolate params with previous results
        params = _interpolate_params(raw_params, context)

        # Execute the tool
        if not registry.has(tool_name):
            return f"Pipeline step {i+1}: tool '{tool_name}' not found"

        try:
            result = registry.run(tool_name, params, {
                "player": player,
                "speak": speak,
                "response": response,
                "session_memory": session_memory,
                "dashboard": dashboard
            })
        except Exception as e:
            return f"Pipeline step {i+1} ('{tool_name}') failed: {e}"

        results.append({"step": i+1, "tool": tool_name, "result": result})

        # Store result for next step if output_var specified
        if output_var:
            context[output_var] = result

    # Return summary
    summary = f"Pipeline completed ({len(results)} steps):\n"
    for r in results:
        summary += f"  {r['step']}. {r['tool']} → {r['result'][:80]}{'...' if len(r['result']) > 80 else ''}\n"
    return summary


TOOL = {
    "name": "pipeline",
    "description": "Execute a sequence of actions as a pipeline. Each step can reference previous results using {{var_name}} in params.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "steps": {
                "type": "ARRAY",
                "description": "Array of pipeline steps",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "tool": {"type": "STRING", "description": "Name of the action/tool to execute"},
                        "params": {"type": "OBJECT", "description": "Parameters for the tool (supports {{var}} interpolation)"},
                        "output_var": {"type": "STRING", "description": "Optional variable name to store this step's result for later steps"}
                    },
                    "required": ["tool"]
                }
            }
        },
        "required": ["steps"]
    },
    "handler": run_pipeline,
    "behavior": "BLOCKING",
    "scheduling": "WHEN_IDLE"
}
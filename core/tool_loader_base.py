"""
Shared base for action_loader and plugin_loader.

Contains the common discovery, validation, and dispatch logic so both loaders
don't duplicate ~150 lines of nearly-identical code.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}


def _opt_upper(value, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None


def _call_with_context(
    fn: Callable,
    parameters: dict,
    ctx: dict,
    context_keys: tuple[str, ...]
) -> any:
    """Invoke fn passing only the context kwargs it actually declares
    (or all of them if it has **kwargs)."""
    sig = inspect.signature(fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    for key in context_keys:
        if has_var_kw or key in sig.parameters:
            kwargs[key] = ctx.get(key)
    return fn(parameters=parameters, **kwargs)


@dataclass
class ToolRecord(Generic[T]):
    """Base record for both actions and plugins."""
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    handler: Optional[Callable] = None   # action: handler, plugin: run
    file: str = ""
    valid: bool = False
    error: str = ""
    # Optional API declaration metadata (included in function declaration to LLM)
    behavior: Optional[str] = None       # None = API default (blocking)
    scheduling: Optional[str] = None     # None = API default (WHEN_IDLE)


class ToolRegistry(Generic[T]):
    """Base registry for actions and plugins."""

    def __init__(
        self,
        tools: dict[str, ToolRecord],
        logger: Callable[[str], None],
        tools_dir: Path | None = None,
        reserved_names: set[str] | None = None
    ):
        self._tools = tools              # name -> ToolRecord, VALID entries only
        self._all_records: list[ToolRecord] = []
        self._logger = logger
        self._tools_dir = tools_dir
        self._reserved_names = reserved_names or set()

    def get_tool_declarations(self, enabled_check: Callable[[str], bool] | None = None) -> list[dict]:
        """Build function declarations for the LLM API."""
        out = []
        for rec in self._tools.values():
            if enabled_check and not enabled_check(rec.name):
                continue
            decl = {"name": rec.name, "description": rec.description,
                    "parameters": rec.parameters}
            if rec.behavior:
                decl["behavior"] = rec.behavior
            out.append(decl)
        return out

    def has(self, name: str) -> bool:
        return name in self._tools

    def scheduling(self, name: str) -> Optional[str]:
        """How this tool's result should re-enter the conversation, if it said."""
        rec = self._tools.get(name)
        return rec.scheduling if rec else None

    def names(self) -> set[str]:
        return set(self._tools.keys())

    def reload(self, discover_fn: Callable) -> tuple[int, int]:
        """Re-discover tools from disk. Returns (loaded_count, rejected_count)."""
        if self._tools_dir is None:
            self._logger(f"[{self.__class__.__name__}] Reload skipped: no tools_dir configured")
            return (0, 0)
        new_registry = discover_fn(
            self._tools_dir,
            reserved_names=self._reserved_names,
            logger=self._logger
        )
        old_count = len(self._tools)
        self._tools = new_registry._tools
        self._all_records = new_registry._all_records
        new_count = len(self._tools)
        self._logger(f"[{self.__class__.__name__}] Hot-reload complete: {new_count} active (was {old_count})")
        return (new_count, len(new_registry._all_records) - new_count)


def _discover_tools(
    tools_dir: Path,
    module_prefix: str,
    tool_dict_name: str,
    validate_fn: Callable,
    reserved_names: set[str] | None,
    logger: Callable[[str], None]
) -> tuple[dict[str, ToolRecord], list[ToolRecord]]:
    """
    Shared discovery logic for actions and plugins.

    Returns (valid_dict, all_records_list).
    """
    reserved = reserved_names or set()
    tools_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, ToolRecord] = {}
    all_records: list[ToolRecord] = []

    files = sorted(tools_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"{module_prefix}.{path.stem}"
            # Reuse already-imported module when present so handlers are the
            # same objects the rest of the app holds.
            module = sys.modules.get(module_name)
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not build import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

            if getattr(module, tool_dict_name, None) is None:
                continue   # not a tool file — a helper/capture-only module

            rec = validate_fn(module, path.name)

            if rec.valid and rec.name in reserved:
                rec = ToolRecord(name=rec.name, file=path.name,
                                 error=f"Name '{rec.name}' collides with a reserved core tool — rejected.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = ToolRecord(name=rec.name, file=path.name,
                                 error=f"Name '{rec.name}' already used by {module_prefix} '{other}' — rejected.")

        except Exception as e:
            rec = ToolRecord(name=path.stem, file=path.name,
                             error=f"Failed to load: {e}")
            traceback.print_exc()

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"{module_prefix.title()} loaded: {rec.name} ({path.name})")
        else:
            # Only log a rejection if the file actually tried to be a tool.
            logger(f"{module_prefix.title()} rejected: {path.name} — {rec.error}")

    return valid, all_records
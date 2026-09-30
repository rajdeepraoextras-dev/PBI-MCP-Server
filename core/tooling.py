"""Tool registration helpers shared by both servers.

``make_tool(mcp, state)`` returns a decorator that replaces the bare
``@mcp.tool()``:

* it attaches MCP tool annotations (read-only / destructive / idempotent
  hints) so hosts can auto-approve reads and gate deletes;
* for write tools it adds a ``dry_run`` keyword (unless the tool already has
  one with its own preview semantics) and routes real calls through the write
  journal (core/journal.py) so every change is revertible with ``pbi_undo``.

``load_tool_modules`` imports every ``tools_*.py`` in a server package and
calls its ``register(mcp, state, tool)``; feature packages add tools without
editing the server module.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import pkgutil

from core import journal
from core.mcp_compat import tool_annotations

DRY_RUN_DOC = ("\n\nSet dry_run=true to preview: the operation runs against a "
               "scratch copy and the response carries the unified diff; "
               "nothing is written.")


def make_tool(mcp, state):
    """Build the ``@tool(...)`` decorator bound to one server + its state."""

    def tool(*, read: bool = False, write: bool = False,
             destructive: bool = False, idempotent: bool = False,
             journaled: bool = True, preview: bool = True,
             name: str | None = None):
        """``preview=False`` keeps the journal but injects no ``dry_run``:
        for tools whose arguments are paths into the live project (restore
        from trash or backup), which a scratch-copy preview would act on for
        real."""
        if read == write:
            raise ValueError("specify exactly one of read=True or write=True")
        ann = tool_annotations(read_only=read,
                               destructive=destructive if write else False,
                               idempotent=idempotent)

        def deco(fn):
            wrapped = (fn if (read or not journaled)
                       else _journaled(fn, state, preview))
            return mcp.tool(name=name, annotations=ann)(wrapped)
        return deco

    return tool


def _journaled(fn, state, preview: bool = True):
    sig = inspect.signature(fn, eval_str=True)
    if not preview:
        @functools.wraps(fn)
        def plain(*args, **kwargs):
            return journal.for_state(state).record(
                fn.__name__, lambda: fn(*args, **kwargs))
        plain.__signature__ = sig
        return plain
    if "dry_run" in sig.parameters:
        # The tool implements its own preview; only journal real writes.
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            if kwargs.get("dry_run"):
                return fn(*args, **kwargs)
            return journal.for_state(state).record(
                fn.__name__, lambda: fn(*args, **kwargs))
        inner.__signature__ = sig
        return inner

    params = list(sig.parameters.values())
    var_kw = [p for p in params if p.kind is inspect.Parameter.VAR_KEYWORD]
    params = [p for p in params if p.kind is not inspect.Parameter.VAR_KEYWORD]
    params.append(inspect.Parameter("dry_run", inspect.Parameter.KEYWORD_ONLY,
                                    default=False, annotation=bool))
    params.extend(var_kw)

    @functools.wraps(fn)
    def inner(*args, dry_run: bool = False, **kwargs):
        if dry_run:
            return journal.dry_run(state, lambda: fn(*args, **kwargs))
        return journal.for_state(state).record(
            fn.__name__, lambda: fn(*args, **kwargs))

    inner.__signature__ = sig.replace(parameters=params)
    inner.__annotations__ = {
        **{p.name: p.annotation for p in params
           if p.annotation is not inspect.Parameter.empty},
        "return": sig.return_annotation,
    }
    inner.__doc__ = (fn.__doc__ or "").rstrip() + DRY_RUN_DOC
    return inner


def register_journal_tools(mcp, state, tool) -> None:
    """pbi_undo / pbi_undo_history / transactions, identical in both servers."""

    @tool(read=True, idempotent=True)
    def pbi_undo_history(limit: int = 20) -> list[dict]:
        """Journaled writes for the selected project, newest first: id, tool,
        time and the files it added/modified/deleted. Both servers share the
        history; any entry can be reverted with pbi_undo."""
        return journal.for_state(state).history(limit)

    @tool(write=True, destructive=True, journaled=False)
    def pbi_undo(steps: int = 1) -> dict:
        """Revert the latest `steps` journaled writes (newest first), restoring
        every changed file byte-for-byte and deleting files those writes
        added. Not journaled itself, so it cannot be undone."""
        return journal.for_state(state).undo(steps)

    @tool(write=True, journaled=False, idempotent=False)
    def pbi_begin_transaction() -> dict:
        """Mark a point so several writes can be reverted together with
        pbi_rollback, or kept with pbi_commit. One open transaction at a time."""
        return journal.for_state(state).begin()

    @tool(write=True, journaled=False)
    def pbi_commit() -> dict:
        """Close the open transaction, keeping every write made since
        pbi_begin_transaction."""
        return journal.for_state(state).commit()

    @tool(write=True, destructive=True, journaled=False)
    def pbi_rollback() -> dict:
        """Revert every write made since pbi_begin_transaction and close the
        transaction."""
        return journal.for_state(state).rollback()


def load_tool_modules(package: str, mcp, state, tool) -> list[str]:
    """Import ``<package>.tools_*`` modules and call their ``register()``."""
    pkg = importlib.import_module(package)
    loaded: list[str] = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if not info.name.startswith("tools_"):
            continue
        mod = importlib.import_module(f"{package}.{info.name}")
        register = getattr(mod, "register", None)
        if register is not None:
            register(mcp, state, tool)
            loaded.append(info.name)
    return loaded

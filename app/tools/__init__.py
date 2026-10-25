"""Tool Gateway (Fase 5, ADR 0006): typed registry, trusted context, policy, audit."""

from .errors import ToolExecutionError, ToolRegistrationError
from .exec_tools import ExecRunTool, ExecutionPolicy
from .filesystem import workspace_tools
from .gateway import ToolGateway, digest_arguments
from .git_tools import GitTools
from .limits import ToolLimits
from .ports import (
    CancellableTool,
    InvocationCompletion,
    InvocationRef,
    ToolContext,
    ToolDescription,
    ToolInvocation,
    ToolInvocationRepository,
    ToolPort,
    ToolSpec,
    invocation_id,
)
from .registry import ToolRegistry


def default_registry(
    limits: ToolLimits | None = None,
    git_tools: GitTools | None = None,
    exec_tool: ExecRunTool | None = None,
) -> ToolRegistry:
    """The tools the platform ships today: read-only workspace access and, when
    managed repositories are configured, Git reads plus the write contracts and
    ``exec.run`` (registered, but refused by the risk gate until enabled)."""
    bounds = limits or ToolLimits()
    tools: list[ToolPort] = list(workspace_tools(bounds))
    if git_tools is not None:
        tools.extend(git_tools.tools())
    if exec_tool is not None:
        tools.append(exec_tool)
    return ToolRegistry(tools)


__all__ = [
    "CancellableTool",
    "ExecRunTool",
    "ExecutionPolicy",
    "GitTools",
    "InvocationCompletion",
    "InvocationRef",
    "ToolContext",
    "ToolDescription",
    "ToolExecutionError",
    "ToolGateway",
    "ToolInvocation",
    "ToolInvocationRepository",
    "ToolLimits",
    "ToolPort",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolSpec",
    "default_registry",
    "digest_arguments",
    "invocation_id",
    "workspace_tools",
]

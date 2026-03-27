from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from vibe.core.tools.base import BaseTool, InvokeContext
from vibe.core.types import LLMMessage, Role


class MemoryTool(BaseTool):
    """Tool for managing conversation memories."""
    
    name = "memory"
    description = (
        "Manage conversation memories. Memories are context-aware information "
        "that can be automatically injected into conversations based on triggers. "
        "Use this tool to add, list, or clear memories."
    )
    
    class MemoryAddArgs(BaseTool.Args):
        content: Annotated[str, Field(description="The content to remember")]
        trigger: Annotated[
            str, 
            Field(
                description="When to inject this memory (always, tool_use, compaction)",
                default="always"
            )
        ]
        scope: Annotated[
            Literal["session", "project", "global"], 
            Field(
                description="Memory scope: session (current conversation), project (current directory), or global (all conversations)",
                default="session"
            )
        ]
        priority: Annotated[
            int, 
            Field(
                description="Memory priority (higher numbers are loaded first)",
                ge=0, 
                le=100,
                default=0
            )
        ]
        tool_name: Annotated[
            str | None, 
            Field(
                description="Optional: tool name for tool-specific memories (only used with tool_use trigger)",
                default=None
            )
        ]
    
    class MemoryListArgs(BaseTool.Args):
        trigger: Annotated[
            str | None, 
            Field(
                description="Optional: filter by trigger type",
                default=None
            )
        ]
        scope: Annotated[
            Literal["session", "project", "global", "all"] | None, 
            Field(
                description="Optional: filter by scope",
                default=None
            )
        ]
    
    class MemoryClearArgs(BaseTool.Args):
        scope: Annotated[
            Literal["session", "project", "global", "all"], 
            Field(
                description="Scope to clear: session, project, global, or all",
                default="session"
            )
        ]
    
    async def invoke(
        self, 
        ctx: InvokeContext, 
        action: Literal["add", "list", "clear"],
        **kwargs
    ) -> LLMMessage:
        """Handle memory management actions."""
        agent_loop = ctx.agent_manager._agent_loop
        
        if action == "add":
            args = self.MemoryAddArgs(**kwargs)
            memory = agent_loop.memory_manager.add_memory(
                content=args.content,
                trigger=args.trigger,
                scope=args.scope,
                priority=args.priority,
                tool_name=args.tool_name if args.trigger == "tool_use" else None
            )
            return LLMMessage(
                role=Role.tool,
                content=f"Memory added: {memory.content[:50]}... (trigger: {memory.trigger}, scope: {memory.metadata.get('scope')})",
                tool_call_id=ctx.tool_call_id
            )
        
        elif action == "list":
            args = self.MemoryListArgs(**kwargs)
            memories = agent_loop.memory_manager._memories
            
            # Filter by trigger
            if args.trigger:
                memories = [m for m in memories if m.trigger == args.trigger]
            
            # Filter by scope
            if args.scope and args.scope != "all":
                memories = [m for m in memories if m.metadata.get("scope") == args.scope]
            
            if not memories:
                return LLMMessage(
                    role=Role.tool,
                    content="No memories found.",
                    tool_call_id=ctx.tool_call_id
                )
            
            memory_list = "\n".join(
                f"{i+1}. {m.content[:60]}... (trigger: {m.trigger}, scope: {m.metadata.get('scope')}, priority: {m.priority})"
                for i, m in enumerate(memories[:10])  # Limit to 10 memories
            )
            
            if len(memories) > 10:
                memory_list += f"\n... and {len(memories) - 10} more memories"
            
            return LLMMessage(
                role=Role.tool,
                content=f"Found {len(memories)} memories:\n{memory_list}",
                tool_call_id=ctx.tool_call_id
            )
        
        elif action == "clear":
            args = self.MemoryClearArgs(**kwargs)
            scope = args.scope if args.scope != "all" else None
            agent_loop.memory_manager.clear_memories(scope)
            
            scope_name = args.scope if args.scope != "all" else "all scopes"
            return LLMMessage(
                role=Role.tool,
                content=f"Cleared memories from {scope_name}.",
                tool_call_id=ctx.tool_call_id
            )
        
        else:
            return LLMMessage(
                role=Role.tool,
                content=f"Unknown action: {action}",
                tool_call_id=ctx.tool_call_id
            )
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

from vibe.core.types import LLMMessage, Role


class Memory(BaseModel):
    """A single memory entry."""
    trigger: str
    content: str
    scope: Literal["always", "tool_use", "compaction"] = "always"
    priority: int = 0
    metadata: Dict[str, Any] = {}


class MemoryManager:
    """Manages memories for context-aware conversation history."""
    
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.project_dir = Path.cwd()
        self.global_dir = Path.home() / ".vibe" / "memories"
        
        # Ensure directories exist
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.global_dir.mkdir(parents=True, exist_ok=True)
        
        # Load memories from all scopes
        self._memories: List[Memory] = []
        self._load_memories()
    
    def _load_memories(self) -> None:
        """Load memories from all scopes (global, project, session)."""
        # Load in priority order: global < project < session
        self._load_memories_from_scope(self.global_dir, "global")
        self._load_memories_from_scope(self.project_dir, "project")
        self._load_memories_from_scope(self.session_dir, "session")
    
    def _load_memories_from_scope(self, scope_dir: Path, scope_name: str) -> None:
        """Load memories from a specific scope directory."""
        memories_file = scope_dir / f"vibe_memories_{scope_name}.json"
        
        if memories_file.exists():
            try:
                with open(memories_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for memory_data in data:
                        memory = Memory(**memory_data)
                        # Add scope information to metadata
                        memory.metadata["scope"] = scope_name
                        self._memories.append(memory)
            except (json.JSONDecodeError, IOError):
                # Silently ignore corrupt or unreadable files
                pass
    
    def _save_memories(self) -> None:
        """Save memories to all scopes."""
        # Separate memories by scope
        memories_by_scope = {"global": [], "project": [], "session": []}
        
        for memory in self._memories:
            scope = memory.metadata.get("scope", "session")
            memories_by_scope[scope].append(memory.model_dump())
        
        # Save to each scope
        for scope_name, memories in memories_by_scope.items():
            if scope_name == "global":
                scope_dir = self.global_dir
            elif scope_name == "project":
                scope_dir = self.project_dir
            else:
                scope_dir = self.session_dir
            
            if memories:
                scope_dir.mkdir(parents=True, exist_ok=True)
                memories_file = scope_dir / f"vibe_memories_{scope_name}.json"
                with open(memories_file, "w", encoding="utf-8") as f:
                    json.dump(memories, f, indent=2, ensure_ascii=False)
    
    def get_memories_for_trigger(
        self, 
        trigger: str, 
        tool_name: Optional[str] = None
    ) -> List[Memory]:
        """Get memories that match a specific trigger."""
        matching_memories = []
        
        for memory in self._memories:
            if memory.trigger == trigger:
                if tool_name and trigger == "tool_use":
                    # For tool-specific memories, check if tool name matches
                    if memory.metadata.get("tool_name") == tool_name:
                        matching_memories.append(memory)
                else:
                    matching_memories.append(memory)
        
        # Sort by priority (highest first)
        return sorted(matching_memories, key=lambda m: m.priority, reverse=True)
    
    def add_memory(
        self, 
        content: str, 
        trigger: str = "always",
        scope: Literal["global", "project", "session"] = "session",
        priority: int = 0,
        tool_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Memory:
        """Add a new memory."""
        memory_metadata = metadata or {}
        memory_metadata["scope"] = scope
        
        if tool_name:
            memory_metadata["tool_name"] = tool_name
        
        memory = Memory(
            trigger=trigger,
            content=content,
            scope=trigger,  # This will be overridden by metadata
            priority=priority,
            metadata=memory_metadata
        )
        
        self._memories.append(memory)
        self._save_memories()
        
        return memory
    
    def convert_to_llm_messages(self, memories: List[Memory]) -> List[LLMMessage]:
        """Convert memories to LLM messages for injection into conversation."""
        messages = []
        
        for memory in memories:
            # Create a system message for each memory
            system_message = LLMMessage(
                role=Role.system,
                content=f"[Memory: {memory.trigger}] {memory.content}",
                memory=True,
                metadata={"memory_id": str(hash(memory)), **memory.metadata}
            )
            messages.append(system_message)
        
        return messages
    
    def clear_memories(self, scope: Optional[Literal["global", "project", "session"]] = None) -> None:
        """Clear memories from a specific scope or all scopes."""
        if scope:
            # Clear only specified scope
            self._memories = [m for m in self._memories if m.metadata.get("scope") != scope]
        else:
            # Clear all memories
            self._memories = []
        
        self._save_memories()
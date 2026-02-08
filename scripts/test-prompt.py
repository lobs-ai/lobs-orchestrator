#!/usr/bin/env python3
"""Quick test to show what prompts look like now."""

import sys
from pathlib import Path

# Add orchestrator to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.services.prompter import Prompter

# Sample task
sample_task = {
    "id": "TEST-1234",
    "kind": "task",
    "title": "Add user authentication to API",
    "notes": "Implement JWT-based auth with refresh tokens. Use bcrypt for passwords.",
    "projectId": "flock"
}

# Build prompt
prompt = Prompter.build_task_prompt(
    sample_task,
    project_id="flock",
    rules="Use TypeScript. Write tests. Follow REST conventions."
)

print(prompt)
print("\n" + "="*60)
print(f"Prompt length: {len(prompt)} characters")
print(f"Prompt lines: {len(prompt.splitlines())} lines")

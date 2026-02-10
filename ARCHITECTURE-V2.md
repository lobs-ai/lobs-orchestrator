# Lobs Orchestrator V2 - Multi-Agent Architecture

## Overview

V2 evolves the orchestrator from a single-worker task executor into a multi-agent coordination system. Instead of one worker type, we have specialized agents that handle different kinds of work, coordinated by an intelligent routing and workflow system.

## Design Principles

### 1. Specialization Over Generalization

Each agent has a focused role with specific capabilities. A Programmer writes code. A Researcher gathers information. An Architect designs systems. Specialization enables:
- Optimized prompts and tools per agent type
- Appropriate model selection (some tasks need more reasoning)
- Clear boundaries and handoff points

### 2. Infrastructure as Shared Services

Agents interact with infrastructure services (Curator, Monitor, Deployer) rather than managing state themselves. This keeps agents stateless and focused while providing shared capabilities across all agent types.

### 3. Workflow-Aware Orchestration

Complex work often requires multiple agents in sequence or parallel. The orchestrator understands workflows and coordinates multi-agent pipelines, not just individual task dispatch.

### 4. Preserved Core Principles

From V1, we keep:
- **Scripts own control**: State lives in filesystem, scripts manage transitions
- **Single writer pattern**: One component writes to lobs-control
- **Restart safety**: Filesystem is the recovery log

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR                             │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │    Router    │  │   Workflow   │  │   Registry   │           │
│  │              │  │    Engine    │  │              │           │
│  │ Analyzes     │  │              │  │ Agent defs   │           │
│  │ incoming     │  │ Coordinates  │  │ Capabilities │           │
│  │ work, picks  │  │ multi-agent  │  │ Prompts      │           │
│  │ agent(s)     │  │ pipelines    │  │ Tools        │           │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘           │
│         │                 │                  │                    │
│  ┌──────▼─────────────────▼──────────────────▼───────┐           │
│  │              Infrastructure Layer                  │           │
│  │                                                    │           │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │           │
│  │  │ Curator  │  │ Monitor  │  │ Control Manager  │ │           │
│  │  │ (future) │  │ (future) │  │ (git ops)        │ │           │
│  │  └──────────┘  └──────────┘  └──────────────────┘ │           │
│  └───────────────────────────────────────────────────┘           │
└──────────────────────────┬───────────────────────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           │               │               │
           ▼               ▼               ▼
    ┌────────────┐  ┌────────────┐  ┌────────────┐
    │ Programmer │  │ Researcher │  │  Reviewer  │
    │            │  │            │  │            │
    │ Writes     │  │ Gathers    │  │ Reviews    │
    │ code       │  │ info       │  │ quality    │
    └────────────┘  └────────────┘  └────────────┘
           
    ┌────────────┐  ┌────────────┐
    │   Writer   │  │  Architect │
    │            │  │            │
    │ Creates    │  │ Designs    │
    │ prose      │  │ systems    │
    └────────────┘  └────────────┘
```

---

## Agents

### Programmer

**Role:** Implements code, fixes bugs, writes tests, makes changes to codebases.

**When used:**
- Task involves writing or modifying code
- Bug fixes and patches
- Adding features with clear specs
- Writing tests
- Refactoring

**Inputs:** Task spec, codebase context, project files  
**Outputs:** Code changes, work summary  
**Model:** Standard (e.g., Sonnet)

### Researcher

**Role:** Gathers information, synthesizes findings, produces structured reports.

**When used:**
- "What's the state of X?"
- Technical deep-dives
- Competitive analysis
- Exploring options before making decisions
- Background research for other agents

**Inputs:** Research question, scope constraints, desired output format  
**Outputs:** Structured report with sources, confidence levels, open questions  
**Model:** Standard or higher for complex synthesis

### Reviewer

**Role:** Reviews code, docs, or other artifacts for quality, correctness, and improvements.

**When used:**
- After Programmer completes work
- PR review automation
- Doc review before publishing
- Architecture review before major changes

**Inputs:** Artifact to review, review criteria, context  
**Outputs:** Issues, suggestions, approval/concerns  
**Model:** Standard

### Writer

**Role:** Creates and polishes prose—documentation, emails, blog posts, PR descriptions.

**When used:**
- Documentation tasks
- Drafting communications
- Polishing rough notes into finished prose
- Writing commit messages, changelogs

**Inputs:** Content brief or rough draft, style guidance, context  
**Outputs:** Polished prose  
**Model:** Standard

### Architect

**Role:** Designs major systems, plans reworks, makes high-level technical decisions.

**When used:**
- New features touching multiple systems
- Major refactors or reworks
- "How should we build X?" questions
- Technical strategy decisions

**Inputs:** Goal description, constraints, context about existing systems  
**Outputs:** Design doc, task breakdown, technical recommendations  
**Model:** Higher tier for complex reasoning (e.g., Opus for major decisions)

---

## Orchestrator Components

### Router

Analyzes incoming tasks and determines which agent(s) should handle them.

**Routing strategies:**
1. **Explicit**: Task specifies agent type (`agent: programmer`)
2. **Rule-based**: Pattern matching on task type, labels, content
3. **LLM-assisted**: For ambiguous cases, use a small model to classify

**Examples:**
- Task title contains "fix", "bug", "implement" → Programmer
- Task title contains "research", "investigate", "explore" → Researcher
- Task title contains "review", "check" → Reviewer
- Task title contains "write", "doc", "draft" → Writer
- Task title contains "design", "architect", "rework" → Architect

### Workflow Engine

Coordinates multi-agent pipelines for complex work.

**Workflow types:**

1. **Single**: One agent, one task (most common)
   ```
   Task → Programmer → Done
   ```

2. **Pipeline**: Sequential agents
   ```
   Task → Architect → [subtasks] → Programmer → Reviewer → Done
   ```

3. **Parallel**: Multiple agents on independent subtasks
   ```
   Task → Architect → [subtask1] → Programmer1 ─┐
                   → [subtask2] → Programmer2 ─┼→ Merge → Done
                   → [subtask3] → Programmer3 ─┘
   ```

4. **Iterative**: Loop until quality threshold met
   ```
   Task → Programmer → Reviewer → [issues?] → Programmer → Reviewer → Done
   ```

### Registry

Stores agent definitions including:
- System prompt template
- Available tools
- Capabilities (for routing)
- Model selection
- Context requirements

**File structure:**
```
agents/
├── programmer/
│   ├── AGENTS.md      # Agent behavior
│   ├── SOUL.md        # Agent personality
│   ├── TOOLS.md       # Available tools
│   └── IDENTITY.md    # Agent metadata
├── researcher/
│   └── ...
├── reviewer/
│   └── ...
├── writer/
│   └── ...
└── architect/
    └── ...
```

---

## Infrastructure Services

### Curator (Future)

Shared knowledge base that all agents can query and contribute to.

**Interface:**
- `search(query)` → relevant knowledge
- `add(fact, source, context)` → store new knowledge
- `update(id, value)` → modify existing
- `list(filters)` → browse knowledge

**Stores:**
- Project knowledge (decisions, patterns, gotchas)
- Research results (avoid re-researching)
- Learned preferences
- Cross-project insights

### Monitor (Future)

System health and observability.

**Interface:**
- `health(system)` → status
- `alert(condition)` → subscribe to events
- `log(event)` → record
- `query(timerange)` → historical data

### Control Manager (Existing)

Single-writer interface to lobs-control repository.

**Interface:**
- Git operations (pull, commit, push)
- Task state transitions
- Project state management

---

## Task Flow

### Simple Task

```
1. Task created in lobs-control
2. Scanner finds task, returns to Engine
3. Router: "this is a code task" → Programmer
4. WorkerManager spawns Programmer agent
5. Programmer executes, writes code, exits
6. Reconciler: commit changes, update task status
```

### Complex Task (with Architect)

```
1. Task: "Add user authentication to API"
2. Router: "major feature, needs design" → Architect
3. Architect designs, outputs:
   - Design doc
   - Subtasks: [auth-middleware, user-model, login-endpoint, tests]
4. Subtasks created in lobs-control
5. For each subtask:
   - Router → Programmer
   - Programmer implements
6. When subtasks complete:
   - Router → Reviewer
   - Reviewer checks overall integration
7. Parent task marked complete
```

---

## Migration from V1

### Phase 1: Registry + Programmer
- Rename worker-template to programmer
- Create Registry that loads agent definitions
- WorkerManager uses Registry to spawn agents
- Behavior unchanged, just restructured

### Phase 2: Add Agents
- Add Researcher, Reviewer, Writer, Architect templates
- Implement Router (start with rule-based)
- Update Scanner to detect task types

### Phase 3: Workflows
- Implement Workflow Engine
- Support pipelines and parallel execution
- Add Architect → subtask creation flow

### Phase 4: Infrastructure
- Curator MVP
- Monitor integration
- Cross-agent knowledge sharing

---

## Configuration

### Agent Registry Config

```json
{
  "agents": {
    "programmer": {
      "template": "agents/programmer",
      "model": "sonnet",
      "capabilities": ["code", "test", "refactor"],
      "tools": ["read", "write", "exec", "browser"]
    },
    "researcher": {
      "template": "agents/researcher",
      "model": "sonnet",
      "capabilities": ["research", "synthesize"],
      "tools": ["read", "web_search", "web_fetch", "browser"]
    },
    "reviewer": {
      "template": "agents/reviewer",
      "model": "sonnet",
      "capabilities": ["review", "analyze"],
      "tools": ["read", "exec"]
    },
    "writer": {
      "template": "agents/writer",
      "model": "sonnet",
      "capabilities": ["write", "edit", "draft"],
      "tools": ["read", "write", "web_fetch"]
    },
    "architect": {
      "template": "agents/architect",
      "model": "opus",
      "capabilities": ["design", "plan", "strategy"],
      "tools": ["read", "write", "web_search", "web_fetch"]
    }
  }
}
```

### Routing Rules Config

```json
{
  "routing": {
    "rules": [
      {"pattern": "fix|bug|implement|add|update|refactor", "agent": "programmer"},
      {"pattern": "research|investigate|explore|analyze", "agent": "researcher"},
      {"pattern": "review|check|audit", "agent": "reviewer"},
      {"pattern": "write|draft|document|doc", "agent": "writer"},
      {"pattern": "design|architect|rework|restructure", "agent": "architect"}
    ],
    "default": "programmer",
    "fallback": "llm"
  }
}
```

---

## Open Questions

1. **Should Architect create tasks directly or output a plan for human approval?**
   - Current leaning: output plan, human approves, then tasks created

2. **How do we handle agent failures in a pipeline?**
   - Retry? Skip? Escalate?

3. **Should agents be able to request other agents?**
   - e.g., Programmer realizes it needs research → spawns Researcher
   - Or should all routing go through orchestrator?

4. **How does Reviewer → Programmer iteration work?**
   - Automated retry until clean?
   - Max iterations before escalating?

---

## File Structure (V2)

```
lobs-orchestrator/
├── agents/
│   ├── programmer/
│   │   ├── AGENTS.md
│   │   ├── SOUL.md
│   │   ├── TOOLS.md
│   │   └── IDENTITY.md
│   ├── researcher/
│   ├── reviewer/
│   ├── writer/
│   └── architect/
├── orchestrator/
│   ├── core/
│   │   ├── engine.py
│   │   ├── router.py       # NEW: task routing
│   │   ├── workflow.py     # NEW: workflow coordination
│   │   ├── registry.py     # NEW: agent registry
│   │   └── worker.py       # existing, uses registry
│   ├── services/
│   │   ├── control.py
│   │   ├── scanner.py
│   │   └── curator.py      # FUTURE
│   └── ...
├── ARCHITECTURE.md          # V1 (keep for reference)
├── ARCHITECTURE-V2.md       # This document
└── ...
```

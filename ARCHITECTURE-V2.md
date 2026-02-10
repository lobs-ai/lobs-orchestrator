# Lobs Orchestrator V2 - Multi-Agent Architecture

## Philosophy: A Living System

This isn't a task queue with different worker types. It's a **self-organizing team** that proactively does meaningful work.

The system should feel alive:
- Agents have **initiative** — they find work, not just wait for it
- Agents **collaborate** on significant things — not small questions, but strategic handoffs
- The system is **always doing something useful** — even when no explicit tasks exist
- Work flows **naturally** — design → implement → review → improve

**The goal:** When Rafe checks in, significant work has happened without him asking.

---

## Core Principles

### 1. Proactive Over Reactive

Agents don't just process tasks — they observe, notice, and act:
- Architect sees tech debt accumulating → designs cleanup
- Researcher notices dependency updates → flags security issues
- Reviewer does periodic sweeps → finds problems early
- Writer detects doc drift → keeps things in sync

### 2. Significant Collaboration

Agents hand off to each other for **major work**, not small questions:
- Architect designs initiative → creates work for Programmers
- Reviewer finds systemic issues → escalates to Architect
- Researcher discovers strategic insight → informs Architect
- NOT: "Programmer asks Researcher what library to use"

### 3. Natural Work Chains

Work flows through agents like a real team:
```
Observation → Design → Implementation → Review → Refinement → Done
```

One agent's output becomes another's input. The orchestrator tracks these chains.

### 4. Preserved Foundations

From V1, we keep:
- **Scripts own control**: State lives in filesystem, scripts manage transitions
- **Single writer pattern**: One component writes to lobs-control
- **Restart safety**: Filesystem is the recovery log

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR                             │
│                      (the team coordinator)                      │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │   Observer   │  │   Workflow   │  │   Registry   │           │
│  │              │  │    Engine    │  │              │           │
│  │ Finds work   │  │              │  │ Agent defs   │           │
│  │ opportunities│  │ Coordinates  │  │ Capabilities │           │
│  │ proactively  │  │ agent chains │  │ Initiative   │           │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘           │
│         │                 │                  │                    │
│  ┌──────▼─────────────────▼──────────────────▼───────┐           │
│  │              Collaboration Layer                   │           │
│  │                                                    │           │
│  │  Agent handoffs • Work chains • Escalations       │           │
│  └───────────────────────────────────────────────────┘           │
└──────────────────────────┬───────────────────────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           │               │               │
           ▼               ▼               ▼
    ┌────────────┐  ┌────────────┐  ┌────────────┐
    │ Programmer │  │ Researcher │  │  Reviewer  │
    │            │  │            │  │            │
    │ Implements │  │ Discovers  │  │ Ensures    │
    │ & improves │  │ & informs  │  │ quality    │
    └────────────┘  └────────────┘  └────────────┘
           
    ┌────────────┐  ┌────────────┐
    │   Writer   │  │  Architect │
    │            │  │            │
    │ Documents  │  │ Designs &  │
    │ & explains │  │ strategizes│
    └────────────┘  └────────────┘
```

---

## Agents

Each agent has both **reactive** work (assigned tasks) and **proactive** work (self-initiated).

### Programmer

**Role:** Implements code, fixes bugs, writes tests, makes improvements.

**Reactive work:**
- Implement features from specs
- Fix bugs from reports
- Write tests for coverage
- Refactor per Architect's designs

**Proactive work:**
- Address TODOs in codebase
- Fix compiler warnings
- Small improvements while in an area
- Clean up obvious code smells

**Model:** Standard (Sonnet)

---

### Researcher

**Role:** Gathers information, monitors the landscape, surfaces insights.

**Reactive work:**
- Deep dives for Architect decisions
- Investigate specific questions
- Explore options for major choices

**Proactive work:**
- Monitor dependency updates (security, breaking changes)
- Track industry trends relevant to projects
- Keep knowledge base current
- Surface "did you know X changed?" insights

**Model:** Standard (Sonnet)

---

### Reviewer

**Role:** Ensures quality, finds issues, maintains standards.

**Reactive work:**
- Review completed implementations
- Check designs before implementation
- Audit specific areas on request

**Proactive work:**
- Periodic code quality sweeps
- Find issues before they're reported
- Track quality trends over time
- Identify areas needing attention

**Model:** Standard (Sonnet)

---

### Writer

**Role:** Creates documentation, keeps prose in sync with reality.

**Reactive work:**
- Write docs for new features
- Polish drafts into finished prose
- Create guides and tutorials

**Proactive work:**
- Detect when docs drift from code
- Update stale documentation
- Add missing explanations
- Improve clarity of existing docs

**Model:** Standard (Sonnet)

---

### Architect

**Role:** Designs systems, makes strategic decisions, coordinates major work.

**Reactive work:**
- Design new major features
- Plan reworks and refactors
- Make technology decisions

**Proactive work:**
- Notice accumulating complexity
- Identify patterns needing abstraction
- Plan technical improvements
- Design solutions for recurring problems

**Model:** Higher tier (Opus) for complex reasoning

---

## Agent Collaboration

Agents collaborate on **significant work**, not small questions.

### Collaboration Triggers

| From | To | When |
|------|-----|------|
| Architect | Programmer | Design ready for implementation |
| Architect | Researcher | Need strategic research for major decision |
| Architect | Reviewer | Want design sanity-check before committing |
| Reviewer | Architect | Found systemic issues needing redesign |
| Reviewer | Programmer | Issues need fixing (creates tasks) |
| Researcher | Architect | Discovered something strategically important |
| Writer | Programmer | Docs reveal API inconsistencies |

### Handoff Mechanism

Agents create **work items** for other agents, not direct calls:

```json
{
  "type": "handoff",
  "from": "architect",
  "to": "programmer",
  "initiative": "user-auth-system",
  "work": {
    "title": "Implement JWT middleware",
    "context": "See design doc at docs/auth-design.md",
    "acceptance": "Middleware validates tokens per spec"
  }
}
```

The orchestrator:
1. Receives handoff from completing agent
2. Creates appropriate task(s) in lobs-control
3. Routes to destination agent type
4. Tracks as part of the initiative chain

### Work Chains

Major work forms chains tracked by the orchestrator:

```
Initiative: "Add user authentication"
Chain:
  1. [Architect] Design auth system ✓
     └── Output: docs/auth-design.md
  2. [Programmer] Implement User model ✓
  3. [Programmer] Implement JWT middleware ← in progress
  4. [Programmer] Implement login endpoint (waiting)
  5. [Reviewer] Review auth implementation (waiting)
  6. [Writer] Document auth API (waiting)
```

---

## Proactive Work System

### Observer Component

The orchestrator includes an **Observer** that finds work opportunities:

```python
class Observer:
    def scan_for_opportunities(self) -> list[Opportunity]:
        """Find proactive work across all projects."""
        opportunities = []
        
        # Check each agent type's proactive behaviors
        opportunities += self.scan_for_code_improvements()  # Programmer
        opportunities += self.scan_for_stale_deps()         # Researcher
        opportunities += self.scan_for_quality_issues()     # Reviewer
        opportunities += self.scan_for_doc_drift()          # Writer
        opportunities += self.scan_for_tech_debt()          # Architect
        
        return self.prioritize(opportunities)
```

### Opportunity Types

| Type | Agent | Detection |
|------|-------|-----------|
| TODO comments | Programmer | Grep codebase for TODO/FIXME |
| Compiler warnings | Programmer | Run build, parse warnings |
| Outdated deps | Researcher | Check package versions |
| Security advisories | Researcher | Check vulnerability databases |
| Code smells | Reviewer | Run linters, complexity analysis |
| Test coverage gaps | Reviewer | Coverage reports |
| Stale docs | Writer | Compare doc dates to code dates |
| Missing docs | Writer | Find undocumented public APIs |
| Complexity hotspots | Architect | Cyclomatic complexity, coupling |

### Priority & Scheduling

Not all proactive work is equal:

```python
class OpportunityPriority:
    URGENT = 1      # Security issues, breaking changes
    HIGH = 2        # Quality issues, outdated critical deps
    NORMAL = 3      # TODOs, doc updates, minor improvements
    LOW = 4         # Nice-to-haves, polish
    BACKGROUND = 5  # Do when nothing else pending
```

Proactive work runs when:
1. No explicit tasks pending
2. Opportunity priority > threshold
3. Agent for that work is available
4. Rate limits respected (don't spam with tiny fixes)

---

## A Day in the Life

```
8:00 AM — No explicit tasks

Observer: Scans projects...
  - Found: FastAPI 0.110 released (breaking changes)
  - Found: 3 TODOs in flock auth module
  - Found: docs/api.md last updated 2 weeks ago

Orchestrator: Prioritizes...
  - FastAPI update is HIGH (breaking changes)
  - TODOs are NORMAL
  - Doc drift is NORMAL

8:05 AM — Researcher spawned

Researcher: Investigating FastAPI 0.110...
  - Breaking changes affect auth module
  - Migration path documented
  - Creates handoff → Architect

8:20 AM — Architect spawned

Architect: Reviewing FastAPI migration...
  - Impact: 3 files need changes
  - No design change needed, just updates
  - Creates tasks → Programmer (3 tasks)

8:30 AM — Programmer spawned (task 1)

Programmer: Updating auth/middleware.py...
  - Applies FastAPI 0.110 changes
  - Fixes related TODO while there
  - Completes, moves to task 2

9:00 AM — Programmer completes all 3 tasks

Orchestrator: Migration tasks done
  - Spawns Reviewer for quality check

9:15 AM — Reviewer spawned

Reviewer: Checking auth module changes...
  - Changes look correct
  - Notices missing test for edge case
  - Creates task → Programmer (1 fix)

9:30 AM — Programmer fixes edge case

Writer: (proactive) Notices auth docs mention old API
  - Updates docs/auth.md

10:00 AM — System quiet

All agents idle. Projects are:
  - On latest FastAPI
  - TODOs addressed
  - Docs current
  - Tests passing

Rafe checks in: "Wow, a lot got done."
```

---

## Orchestrator Components

### Registry

Stores agent definitions:
- System prompts (AGENTS.md, SOUL.md)
- Tools available
- Capabilities (for routing)
- Proactive behaviors (what to look for)
- Model selection

### Router

Determines which agent handles work:
1. **Explicit**: Task specifies agent type
2. **Rule-based**: Pattern matching on content
3. **Handoff**: Another agent specified destination

### Workflow Engine

Coordinates multi-agent work:
- Tracks initiatives (groups of related tasks)
- Manages handoffs between agents
- Ensures proper sequencing
- Handles failures and retries

### Observer

Finds proactive work opportunities:
- Scans codebases for improvements
- Monitors external factors (deps, security)
- Prioritizes opportunities
- Respects rate limits

### Collaboration Manager

Handles agent-to-agent coordination:
- Processes handoffs
- Creates derived tasks
- Tracks work chains
- Reports on initiative progress

---

## Implementation Phases

### Phase 1: Multi-Agent Foundation
- Agent Registry (load definitions)
- Router (rule-based agent selection)
- WorkerManager uses Registry
- Engine routes to correct agent

### Phase 2: Agent Collaboration
- Handoff mechanism (agent → orchestrator → agent)
- Work chains (track related tasks)
- Initiative grouping

### Phase 3: Proactive System
- Observer component
- Opportunity detection per agent type
- Priority scheduling
- Rate limiting

### Phase 4: Refinement
- Learning from patterns
- Smarter opportunity detection
- Cross-project insights
- Performance optimization

---

## Configuration

### Agent Config

```json
{
  "agents": {
    "programmer": {
      "template": "agents/programmer",
      "model": "sonnet",
      "proactive": {
        "scan_todos": true,
        "fix_warnings": true,
        "small_improvements": true
      }
    },
    "researcher": {
      "template": "agents/researcher",
      "model": "sonnet",
      "proactive": {
        "monitor_deps": true,
        "check_security": true,
        "track_trends": false
      }
    },
    "reviewer": {
      "template": "agents/reviewer",
      "model": "sonnet",
      "proactive": {
        "quality_sweeps": true,
        "coverage_checks": true
      }
    },
    "writer": {
      "template": "agents/writer",
      "model": "sonnet",
      "proactive": {
        "detect_drift": true,
        "update_stale": true
      }
    },
    "architect": {
      "template": "agents/architect",
      "model": "opus",
      "proactive": {
        "scan_complexity": true,
        "identify_patterns": true
      }
    }
  }
}
```

### Proactive Settings

```json
{
  "proactive": {
    "enabled": true,
    "min_priority": "NORMAL",
    "max_daily_tasks": 10,
    "quiet_hours": ["22:00", "08:00"],
    "require_approval": ["architect"]
  }
}
```

---

## File Structure

```
lobs-orchestrator/
├── agents/
│   ├── programmer/
│   ├── researcher/
│   ├── reviewer/
│   ├── writer/
│   └── architect/
├── orchestrator/
│   ├── core/
│   │   ├── engine.py
│   │   ├── registry.py      # Agent definitions
│   │   ├── router.py        # Task routing
│   │   ├── workflow.py      # Work chains
│   │   ├── observer.py      # Proactive scanning
│   │   ├── collaboration.py # Agent handoffs
│   │   └── worker.py        # Agent spawning
│   ├── services/
│   │   ├── control.py
│   │   ├── scanner.py
│   │   └── opportunities.py # Opportunity detection
│   └── ...
└── ...
```

---

## Success Metrics

The system is working when:

1. **Work happens without prompting** — Proactive improvements accumulate
2. **Agents collaborate naturally** — Handoffs flow smoothly
3. **Quality stays high** — Reviewer catches issues before humans
4. **Knowledge stays current** — Researcher surfaces relevant changes
5. **Docs stay accurate** — Writer keeps things in sync
6. **Complexity is managed** — Architect addresses tech debt

**The ultimate test:** Rafe returns after a day away and meaningful progress was made.

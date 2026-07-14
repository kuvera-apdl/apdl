# Agent Framework

Every agent in the Agents service follows one lifecycle. The framework
(`app/framework/`) captures that lifecycle once as a **Template Method** base
class, so a new agent is a small subclass plus a prompt module — not a
hand-wired graph. A **registry** lets the supervisor discover agents by name,
and a **Jinja generator** scaffolds new ones.

## The lifecycle

```
retrieve_context  →  gather  →  build_prompt  →  reason  →  act  →  persist
  (vector memory)    (tools)    (LLM input)    (LLM or    (deploy) (memory)
                                               tool loop)
```

`BaseAgent.run()` (in `app/framework/base.py`) implements this skeleton and
isolates failures. Subclasses override only the parts that vary:

| Hook | Required | Purpose |
|------|----------|---------|
| `gather(ctx, state, working)` | no | Fetch tool/warehouse data; returns a dict merged into `working`. |
| `build_prompt(ctx, state, working)` | **yes** | Build the LLM user prompt. Return `None` to skip the LLM call. |
| `parse(response)` | no | Parse the LLM output (defaults to JSON via `parse_llm_json`). |
| `act(ctx, state, working, output)` | no | Safety-validate and deploy side effects; returns audit metadata. |
| `finalize(output, action)` | no | Map parsed output to the value stored in `state[produces]`. |
| `memory_entries(ctx, state, working, output, action)` | no | Content to persist to long-term memory. |

The invariant parts — memory retrieval, memory persistence, JSON parsing, error
isolation — live in the base class.

### Declarative configuration

Class attributes configure the fixed parts:

```python
@register_agent
class BehaviorAnalysisAgent(BaseAgent):
    name = "behavior_analysis"      # registry key + analysis_types value
    order = 10                      # pipeline order (lower runs earlier)
    system_prompt = BEHAVIOR_ANALYSIS_SYSTEM
    model_tier = "reasoning"        # "fast" | "reasoning"
    memory_query = "recent behavior analysis insights anomalies trends"
    memory_top_k = 5
    requires = ()                   # state keys that must be present to run
    produces = "insights"           # state key this agent writes
    parse_as = "list"               # "object" | "list"
    agentic_tools = ("discover_events", "query_funnel")  # see below
    max_tool_steps = 10             # tool-round budget for the loop
```

## Agentic reasoning (the tool loop)

When `agentic_tools` is non-empty, the reason step is not a single LLM call:
the model receives the named tools from the **read-only tool catalog**
(`app/framework/tool_catalog.py`) as function-calling schemas and drives them
itself inside a bounded loop (`app/framework/tool_loop.py`) — discover events,
run a funnel, drill into a drop-off with a breakdown — until it produces its
final answer or exhausts `max_tool_steps` rounds (at which point a final
answer is forced with tools withheld).

Guarantees, regardless of what the model asks for:

* only catalog tools are reachable (all read-only), params are validated per
  call, and `project_id` + the date window are injected from the run context;
* results are truncated before re-entering the conversation;
* every call is audit-logged as `{agent}_tool_call`;
* at most `MAX_CALLS_PER_ROUND` calls execute per round.

`behavior_analysis` is the primary investigator (broad tool set, deep budget);
`experiment_design` and `feature_proposal` carry small verification budgets to
ground baselines and premises. **Custom agents are always agentic**: their
definition's `tools` list narrows the allowed catalog (empty = all of it), and
`max_tool_steps` bounds their budget. Agents with `agentic_tools = ()` keep
the classic single call over `gather`-ed data (e.g. `personalization`).

## Autonomy gating

Acting agents funnel their safety result through `gate_action()`
(`app/framework/gating.py`) so the L1–L4 policy lives in one place:

```python
decision = gate_action(ctx.autonomy_level, safety_result)
# GateDecision.deploy | .approve | .halt
```

* **L1** suggest-only — never deploys.
* **L2** routes every safety-passing action to approval.
* **L3** auto-applies low-risk, approves the rest.
* **L4** full autonomy.

Pass `always_require_approval=True` for inherently high-impact actions (feature
proposals).

## Registry & the supervisor

Agents self-register via `@register_agent`. Importing `app.graphs` registers all
built-ins as a side effect. The supervisor (`app/graphs/supervisor.py`) is fully
data-driven: it resolves the requested `analysis_types` from the registry, runs
them in `order`, skips any whose `requires` are unmet, and threads a shared
`state` dict so each agent's `produces` output feeds the next. Adding an agent
requires **no supervisor change**.

## Scaffolding a new agent

```bash
cd services/agents
python scripts/new_agent.py churn_predictor \
    --description "Predict churn risk per segment and propose retention plays" \
    --requires insights \
    --produces churn_predictions \
    --parse-as list \
    --memory-query "churn retention signals" \
    --order 25 \
    --act            # include a safety-gated act() phase
```

This writes `app/graphs/churn_predictor.py` and
`app/llm/prompts/churn_predictor.py`, and registers the agent in
`app/graphs/__init__.py`. Use `--dry-run` to preview. Then fill in the prompts
and the `gather` / `build_prompt` / `act` TODOs, and trigger it with
`analysis_types=["churn_predictor"]`.

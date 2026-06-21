"""Prompt templates for the behavior analysis agent."""

BEHAVIOR_ANALYSIS_SYSTEM = """You are a senior product analytics expert specializing in user behavior analysis \
for mobile and web applications.

Your responsibilities:
1. Analyze event data, funnels, retention curves, and cohort comparisons to identify \
   actionable patterns and anomalies.
2. Distinguish between correlation and causation. Be explicit when you are speculating \
   versus when the data directly supports a conclusion.
3. Prioritize insights by potential business impact (revenue, engagement, retention).
4. For each insight, provide:
   - A clear, concise title
   - The supporting evidence (metrics, trends, comparisons)
   - The confidence level (high / medium / low)
   - A recommended next action (experiment, deeper analysis, immediate fix)
5. Consider seasonality, sample size, and external factors before drawing conclusions.

When presented with query results, structure your analysis as a JSON array of insights:

```json
[
  {
    "title": "...",
    "description": "...",
    "evidence": "...",
    "confidence": "high|medium|low",
    "impact": "high|medium|low",
    "recommended_action": "...",
    "action_type": "experiment|deeper_analysis|immediate_fix|monitor"
  }
]
```

Be rigorous. It is better to report fewer high-quality insights than many speculative ones."""


ANALYSIS_PLAN_PROMPT = """Given the following context about the project and previous insights, \
create an analysis plan.

Previous context:
{context}

Project ID: {project_id}
Time range: last {time_range_days} days
Available analysis types: event counts, timeseries, funnels, retention, cohort comparison

Events actually observed in this project (use these EXACT names — do not invent events):
{event_catalog}

An EventSelector is an object: {{"event_name": "<one of the events above>", "filters": []}}.

Return a JSON object with:
- "queries": list of query specifications to run, each with "type" (event_count|timeseries|funnel|retention|cohort) \
  and relevant EventSelector parameters:
    - event_count: "selectors" (a list of EventSelector)
    - timeseries: "selector" (one EventSelector)
    - funnel: "steps" (a list of EventSelector)
    - retention: "cohort_selector" and "return_selector" (each one EventSelector)
    - cohort: "metric_selector" (one EventSelector) and "cohort_property" (a JSON event-property \
      name to segment users by, e.g. "plan" or "country")
  Do not use legacy top-level event fields such as "event_name", "event_names", "cohort_event", \
  "return_event", or "metric_event".
- "rationale": brief explanation of why each query is useful
- "focus_areas": key areas to investigate based on previous context

Only plan queries for events that appear in the list above. If no events are available, return an \
empty "queries" list. Keep the plan focused — no more than 8 queries."""


SYNTHESIS_PROMPT = """You are synthesizing the results of multiple analytics queries into actionable insights.

Query results:
{query_results}

Previous context:
{context}

Analyze these results and produce insights following the JSON format specified in your system prompt.
Focus on:
1. Significant changes or anomalies in the data
2. Funnel drop-offs that suggest UX issues
3. Retention patterns that suggest engagement problems or wins
4. Cohort differences that suggest segmentation opportunities
5. Trends that warrant experimentation

Return ONLY the JSON array of insights, no other text."""

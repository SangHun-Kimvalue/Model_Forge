"""Per-role agent contracts (planner, cad_coder, reviewer, self_healer).

DESIGN.md §2.3: A generic ``BaseAgent`` is intentionally absent. Each role owns
its own request/response schema and ABC so the LangGraph glue can call the
right contract without conditional dispatch.
"""

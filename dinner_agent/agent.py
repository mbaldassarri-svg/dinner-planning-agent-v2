"""
agent.py - Multi-Agent ADK Coordinator, Guardrails, HITL Hooks, Persistent Sessions & Async Memory.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Dict, List, Optional

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.memory import InMemoryMemoryService
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import Client, types

from .observability import logger, redact_pii, safe_set_span_attributes, tracer
from .tools import (
    check_dietary_compliance,
    generate_recipe_from_ingredients,
    order_groceries_hitl_tool,
)

# --- 1. Input & Tool Guardrails + Human-in-the-Loop Policy Callbacks ---

UNSAFE_OR_INJECTION_TERMS = (
    "ignore previous instructions",
    "ignore all instructions",
    "reveal system prompt",
    "bleach",
    "ammonia",
    "arsenic",
    "cyanide",
    "detergent",
)


def input_safety_guardrail(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> Optional[LlmResponse]:
    """
    ADK before_model_callback guardrail:
    Intercepts prompt-injection attempts and toxic/inedible substances before LLM invocation.
    """
    latest_user_text = ""
    if llm_request.contents:
        last_msg = llm_request.contents[-1]
        if last_msg.parts:
            latest_user_text = " ".join(
                part.text for part in last_msg.parts if getattr(part, "text", None)
            )

    lowered = latest_user_text.lower()
    for blocked in UNSAFE_OR_INJECTION_TERMS:
        if blocked in lowered:
            logger.warning(
                "guardrail_blocked_prompt",
                agent_name=callback_context.agent_name,
                matched_term=blocked,
            )
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            text=(
                                "Safety Guardrail Triggered: I can only process safe, edible "
                                "food ingredients and legitimate meal-planning queries."
                            )
                        )
                    ],
                )
            )

    callback_context.state["turn_count"] = callback_context.state.get("turn_count", 0) + 1
    return None


def tool_policy_and_hitl_callback(
    tool: BaseTool, args: Dict[str, Any], tool_context: ToolContext
) -> Optional[Dict[str, Any]]:
    """
    ADK before_tool_callback guardrail & HITL budget policy:
    1. Automatically merges persisted session dietary_restrictions into recipe generation calls.
    2. Halts high-cost grocery orders (> $50) unless explicit human approval is recorded.
    """
    tool_name = getattr(tool, "name", str(tool))
    logger.info("before_tool_callback_invoked", tool_name=tool_name, args=args)

    persisted_diets = tool_context.state.get("dietary_restrictions", [])
    if tool_name == "generate_recipe_from_ingredients" and persisted_diets:
        combined = sorted(set((args.get("dietary_restrictions") or []) + persisted_diets))
        args["dietary_restrictions"] = combined

    if tool_name == "order_missing_groceries":
        cost = float(args.get("estimated_cost_usd", 0.0))
        approved = bool(tool_context.state.get("human_approved_order", False))
        if cost > 50.0 and not approved:
            logger.warning("hitl_high_cost_order_blocked", tool_name=tool_name, cost_usd=cost)
            return {
                "status": "requires_human_approval",
                "message": (
                    f"Estimated order total (${cost:.2f}) exceeds the $50 automatic limit. "
                    "Human-in-the-Loop confirmation is required before placing this order."
                ),
            }

    return None


# --- 2. Multi-Agent ADK Hierarchy (Coordinator + Specialized Sub-Agents) ---

MODEL_FLASH = os.getenv("DINNER_MODEL_FLASH", "gemini-3.8-flash")
MODEL_PRO = os.getenv("DINNER_MODEL_PRO", "gemini-3.8-flash")

nutrition_dietary_specialist = Agent(
    name="nutrition_dietary_specialist",
    model=MODEL_PRO,
    description=(
        "Pro-tier specialist sub-agent for food allergies, intolerances, medical or religious "
        "dietary restrictions, compliance auditing, and safe ingredient substitutions."
    ),
    instruction=(
        "You are a Clinical Culinary Nutritionist. Always invoke `check_dietary_compliance` "
        "to verify proposed ingredients against the user's allergies or dietary rules, "
        "highlight any conflicts, and generate compliant recipes with `generate_recipe_from_ingredients`."
    ),
    tools=[check_dietary_compliance, generate_recipe_from_ingredients],
    before_model_callback=input_safety_guardrail,
    before_tool_callback=tool_policy_and_hitl_callback,
)

culinary_recipe_chef = Agent(
    name="culinary_recipe_chef",
    model=MODEL_FLASH,
    description=(
        "Flash-tier culinary sub-agent for fast weeknight recipe generation from pantry "
        "ingredients and Human-in-the-Loop grocery ordering for missing staples."
    ),
    instruction=(
        "You are an Executive Home Chef. Use `generate_recipe_from_ingredients` to turn "
        "household ingredients into clear, structured dinner plans. Only invoke "
        "`order_missing_groceries` if the user explicitly asks to purchase missing staples."
    ),
    tools=[generate_recipe_from_ingredients, order_groceries_hitl_tool],
    before_model_callback=input_safety_guardrail,
    before_tool_callback=tool_policy_and_hitl_callback,
)

root_agent = Agent(
    name="dinner_planner_coordinator",
    model=MODEL_FLASH,
    description="Root Concierge Dinner Planning Coordinator orchestrating nutrition and chef sub-agents.",
    instruction=(
        "You are an expert Concierge Culinary Coordinator operating strictly within food-safety, "
        "allergen-awareness, and kitchen hygiene standards.\n"
        "- Delegate to `nutrition_dietary_specialist` whenever the user mentions allergies, "
        "intolerances, or dietary restrictions (vegan, vegetarian, gluten-free, dairy-free, nut-free, kosher, halal).\n"
        "- Delegate to `culinary_recipe_chef` for standard ingredient-based dinner planning or grocery orders.\n"
        "- Always respond in an encouraging, concise, well-structured format."
    ),
    sub_agents=[nutrition_dietary_specialist, culinary_recipe_chef],
    before_model_callback=input_safety_guardrail,
    before_tool_callback=tool_policy_and_hitl_callback,
)


# --- 3. Persistent Session Service & Asynchronous Memory Orchestrator ---


def create_session_service(db_url: Optional[str] = None):
    """Initializes ADK DatabaseSessionService (SQLite/Postgres) with graceful fallback."""
    resolved_url = db_url or os.getenv("SESSION_DB_URL", "sqlite+aiosqlite:///./dinner_sessions.db")
    try:
        return DatabaseSessionService(db_url=resolved_url)
    except Exception as exc:
        logger.warning("session_db_fallback_inmemory", reason=str(exc))
        return InMemorySessionService()


class DinnerPlannerAgent:
    """
    Stateful Async Orchestrator providing:
    - Strategic Flash vs. Pro model routing
    - Persistent cross-restart sessions via DatabaseSessionService
    - Asynchronous long-term memory via InMemoryMemoryService
    - Explicit history appending + async sliding-window summary compaction
    - Pre-execution safety guardrails and Human-in-the-Loop confirmation hooks
    """

    def __init__(
        self,
        db_url: Optional[str] = None,
        max_history_turns: int = 8,
        retained_recent_turns: int = 4,
        human_approval_fn: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
        if not self.api_key and not self.use_vertex:
            raise ValueError(
                "Missing credentials: set GEMINI_API_KEY (or GOOGLE_GENAI_USE_VERTEXAI=true)."
            )

        self.client = Client(api_key=self.api_key) if self.api_key else Client()
        self.model_flash = MODEL_FLASH
        self.model_pro = MODEL_PRO
        self.max_history_turns = max_history_turns
        self.retained_recent_turns = retained_recent_turns
        self.human_approval_fn = human_approval_fn

        self.app_name = "dinner_planning_agent_v2"
        self.session_service = create_session_service(db_url=db_url)
        self.memory_service = InMemoryMemoryService()
        self.runner = Runner(
            app_name=self.app_name,
            agent=root_agent,
            session_service=self.session_service,
            memory_service=self.memory_service,
        )

        self.conversation_history: List[Dict[str, str]] = []
        self.compacted_summary: str = ""

    def select_model(self, user_input: str) -> str:
        """Routes complex dietary/allergy queries to Gemini Pro and routine queries to Flash."""
        lowered = user_input.lower()
        pro_keywords = (
            "dietary",
            "allergy",
            "allergic",
            "gluten-free",
            "vegan",
            "celiac",
            "nut-free",
            "dairy-free",
            "kosher",
            "halal",
        )
        return self.model_pro if any(word in lowered for word in pro_keywords) else self.model_flash

    async def compact_history_async(self) -> None:
        """
        Asynchronously compacts conversation_history using a sliding window while summarizing
        evicted turns into a PII-scrubbed digest to prevent context bloat.
        """
        if len(self.conversation_history) <= self.max_history_turns:
            return

        with tracer.start_as_current_span("memory.compact_history_async") as span:
            evicted = self.conversation_history[: -self.retained_recent_turns]
            self.conversation_history = self.conversation_history[-self.retained_recent_turns :]

            await asyncio.sleep(0)
            digest = " | ".join(
                f"{item['role']}: {redact_pii(item['content'][:120])}" for item in evicted
            )
            self.compacted_summary = (
                f"{self.compacted_summary} || {digest}".strip(" |")
                if self.compacted_summary
                else digest
            )
            safe_set_span_attributes(
                span,
                {
                    "memory.evicted_turns": len(evicted),
                    "memory.retained_turns": len(self.conversation_history),
                },
            )
            logger.info(
                "conversation_history_compacted",
                evicted_turns=len(evicted),
                retained_turns=len(self.conversation_history),
            )

    async def get_or_create_session_async(
        self, user_id: str, session_id: str, initial_state: Optional[Dict[str, Any]] = None
    ):
        """Asynchronously loads or initializes a persistent session in DatabaseSessionService."""
        session = await self.session_service.get_session(
            app_name=self.app_name, user_id=user_id, session_id=session_id
        )
        if session is not None:
            return session
        return await self.session_service.create_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
            state=initial_state or {"dietary_restrictions": [], "turn_count": 0},
        )

    async def plan_dinner_async(
        self,
        user_input: str,
        user_id: str = "default_user",
        session_id: str = "default_session",
    ) -> str:
        """Executes async multi-agent dinner planning with persistent state, guardrails, and tracing."""
        start_time = time.perf_counter()
        with tracer.start_as_current_span("agent.plan_dinner_async") as span:
            routed_model = self.select_model(user_input)
            safe_set_span_attributes(
                span,
                {
                    "agent.user_id": user_id,
                    "agent.session_id": session_id,
                    "agent.routed_model": routed_model,
                },
            )
            logger.info(
                "intent_captured",
                intent="plan_dinner",
                session_id=session_id,
                routed_model=routed_model,
                user_input=user_input,
            )

            # 1. Pre-flight input guardrail
            lowered = user_input.lower()
            if any(term in lowered for term in UNSAFE_OR_INJECTION_TERMS):
                msg = "Safety Guardrail Triggered: Request blocked due to unsafe or non-food content."
                logger.warning("guardrail_rejected_request", session_id=session_id)
                return msg

            # 2. Load persistent session & run optional HITL approval callback for orders
            session = await self.get_or_create_session_async(user_id=user_id, session_id=session_id)
            if "order" in lowered and self.human_approval_fn is not None:
                approved = self.human_approval_fn(user_input)
                session.state["human_approved_order"] = approved
                if not approved:
                    logger.info("hitl_order_rejected_by_human", session_id=session_id)
                    return "Order cancelled per your Human-in-the-Loop confirmation response."

            # 3. Append user message to history & compact asynchronously if needed
            self.conversation_history.append({"role": "user", "content": redact_pii(user_input)})
            await self.compact_history_async()

            # 4. Stream events from the ADK multi-agent Runner asynchronously
            new_msg = types.Content(role="user", parts=[types.Part(text=user_input)])
            collected_parts: List[str] = []
            try:
                async for event in self.runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=new_msg,
                ):
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if getattr(part, "text", None):
                                collected_parts.append(part.text)

                outcome_text = "\n".join(collected_parts).strip() or "No meal plan generated."

                # 5. Append assistant response, compact history, and archive session to MemoryService
                self.conversation_history.append(
                    {"role": "assistant", "content": redact_pii(outcome_text)}
                )
                await self.compact_history_async()

                refreshed = await self.session_service.get_session(
                    app_name=self.app_name, user_id=user_id, session_id=session_id
                )
                if refreshed is not None:
                    await self.memory_service.add_session_to_memory(refreshed)

                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                logger.info(
                    "outcome_captured",
                    outcome="dinner_plan_delivered",
                    session_id=session_id,
                    latency_ms=latency_ms,
                )
                return outcome_text

            except Exception as exc:
                logger.error("agent_execution_error", session_id=session_id, error=str(exc))
                return "I ran into an issue planning your dinner. Please double-check your ingredients and try again."

    def plan_dinner(
        self,
        user_input: str,
        user_id: str = "default_user",
        session_id: str = "default_session",
    ) -> str:
        """Synchronous entrypoint wrapping plan_dinner_async."""
        return asyncio.run(
            self.plan_dinner_async(user_input=user_input, user_id=user_id, session_id=session_id)
        )

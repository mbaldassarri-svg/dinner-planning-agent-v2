"""
tools.py - Pydantic JSON-Schema Validated Culinary Tools & Human-in-the-Loop (HITL) Actions.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .observability import logger, safe_set_span_attributes, tracer


# --- 1. Explicit Pydantic JSON Schemas for Strict Input/Output Validation ---


class RecipeRequest(BaseModel):
    """Input JSON Schema contract for generating a personalized dinner recipe."""

    model_config = ConfigDict(extra="forbid")

    ingredients: List[str] = Field(
        ...,
        min_length=1,
        description="Non-empty list of available kitchen ingredients (e.g., ['salmon', 'asparagus', 'lemon']).",
    )
    dietary_restrictions: List[str] = Field(
        default_factory=list,
        description="Optional allergies or dietary rules (e.g., ['gluten-free', 'dairy-free', 'vegan']).",
    )
    max_cook_time_minutes: int = Field(
        default=45,
        ge=5,
        le=240,
        description="Maximum allowed active cooking time in minutes (between 5 and 240).",
    )

    @field_validator("ingredients")
    @classmethod
    def clean_ingredients(cls, values: List[str]) -> List[str]:
        normalized = [item.strip().lower() for item in values if item and item.strip()]
        if not normalized:
            raise ValueError("At least one non-empty ingredient string is required.")
        return normalized


class RecipeResponse(BaseModel):
    """Output JSON Schema contract for a validated dinner recipe."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(default="success", description="Execution status ('success').")
    recipe_name: str = Field(..., description="Descriptive culinary title of the suggested dish.")
    prep_time_minutes: int = Field(..., ge=0, description="Preparation time in minutes.")
    cook_time_minutes: int = Field(..., ge=0, description="Active cooking time in minutes.")
    dietary_tags: List[str] = Field(default_factory=list, description="Applied dietary tags.")
    instructions: List[str] = Field(
        ..., min_length=1, description="Step-by-step preparation and cooking instructions."
    )
    missing_ingredients: List[str] = Field(
        default_factory=list,
        description="Common pantry staples or optional garnishes needed.",
    )
    schema_version: str = Field(default="2.0", description="JSON Schema contract version.")


class DietaryComplianceRequest(BaseModel):
    """Input JSON Schema contract for auditing ingredients against allergies/diets."""

    model_config = ConfigDict(extra="forbid")

    ingredients: List[str] = Field(..., min_length=1, description="Proposed meal ingredients.")
    restrictions: List[str] = Field(
        default_factory=list, description="User allergies or dietary restrictions."
    )


class DietaryComplianceResponse(BaseModel):
    """Output JSON Schema contract for dietary compliance verification."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(default="success")
    compliant: bool = Field(..., description="True if zero allergen/diet conflicts were found.")
    conflicts: List[str] = Field(default_factory=list, description="Ingredients violating rules.")
    substitutions: Dict[str, str] = Field(
        default_factory=dict, description="Recommended safe culinary substitutions."
    )


class GroceryOrderRequest(BaseModel):
    """Input JSON Schema contract for Human-in-the-Loop grocery ordering."""

    model_config = ConfigDict(extra="forbid")

    items: List[str] = Field(..., min_length=1, description="Missing grocery staples to order.")
    estimated_cost_usd: float = Field(
        ..., ge=0.0, le=500.0, description="Estimated order cost in USD (0.00 to 500.00)."
    )


class ToolErrorResponse(BaseModel):
    """Structured guided error schema with recovery instructions and expected JSON Schema."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(default="error")
    error: str = Field(..., description="Clear summary of the validation or execution failure.")
    recovery_instructions: str = Field(
        ..., description="Actionable guidance telling the agent/user how to fix the call."
    )
    expected_json_schema: Dict[str, Any] = Field(
        ..., description="Formal JSON Schema expected by this tool."
    )


# Export machine-readable JSON Schemas for documentation, MCP/OpenAPI, and automated tests
TOOL_JSON_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "RecipeRequest": RecipeRequest.model_json_schema(),
    "RecipeResponse": RecipeResponse.model_json_schema(),
    "DietaryComplianceRequest": DietaryComplianceRequest.model_json_schema(),
    "DietaryComplianceResponse": DietaryComplianceResponse.model_json_schema(),
    "GroceryOrderRequest": GroceryOrderRequest.model_json_schema(),
}

ALLERGEN_RULES = {
    "vegetarian": {"chicken", "beef", "pork", "salmon", "shrimp", "bacon", "turkey"},
    "vegan": {"chicken", "beef", "pork", "salmon", "egg", "eggs", "milk", "cheese", "butter", "honey"},
    "gluten-free": {"wheat", "flour", "pasta", "bread", "soy sauce", "barley"},
    "dairy-free": {"milk", "cheese", "butter", "cream", "yogurt"},
    "nut-free": {"peanut", "peanuts", "almond", "almonds", "walnut", "cashew", "pecan"},
}

SAFE_SUBSTITUTIONS = {
    "soy sauce": "gluten-free tamari or coconut aminos",
    "butter": "extra-virgin olive oil or vegan butter",
    "milk": "unsweetened oat milk",
    "pasta": "chickpea or brown-rice pasta",
    "chicken": "crispy pressed tofu or chickpeas",
    "beef": "portobello mushrooms or lentils",
    "cheese": "nutritional yeast",
}


# --- 2. Tool Functions ---


def generate_recipe_from_ingredients(
    ingredients: List[str],
    dietary_restrictions: Optional[List[str]] = None,
    max_cook_time_minutes: int = 45,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    Generates a personalized, JSON-schema-validated dinner recipe from on-hand ingredients.

    Args:
        ingredients: Non-empty list of available kitchen ingredients (e.g., ["chicken", "broccoli"]).
        dietary_restrictions: Optional dietary exclusions or allergies (e.g., ["gluten-free"]).
        max_cook_time_minutes: Maximum allowed cooking time in minutes (5 to 240).
        tool_context: Optional ADK ToolContext used to store recipe metadata in persistent session state.

    Returns:
        Dictionary validated against RecipeResponse (or ToolErrorResponse on invalid input).
    """
    with tracer.start_as_current_span("tool.generate_recipe_from_ingredients") as span:
        try:
            req = RecipeRequest(
                ingredients=ingredients or [],
                dietary_restrictions=dietary_restrictions or [],
                max_cook_time_minutes=max_cook_time_minutes,
            )
        except ValidationError as exc:
            logger.warning(
                "tool_input_validation_error",
                tool="generate_recipe_from_ingredients",
                errors=exc.errors(),
            )
            return ToolErrorResponse(
                error=f"Input validation failed: {exc.errors()[0]['msg']}",
                recovery_instructions=(
                    "Ask the user to supply at least one valid kitchen ingredient and ensure "
                    "max_cook_time_minutes is an integer between 5 and 240."
                ),
                expected_json_schema=RecipeRequest.model_json_schema(),
            ).model_dump()

        safe_set_span_attributes(
            span,
            {
                "tool.ingredient_count": len(req.ingredients),
                "tool.restrictions": req.dietary_restrictions,
                "tool.max_cook_time_minutes": req.max_cook_time_minutes,
            },
        )

        # Automatically swap any ingredient that conflicts with declared restrictions
        adapted_ingredients = list(req.ingredients)
        for rule in req.dietary_restrictions:
            forbidden = ALLERGEN_RULES.get(rule.lower().strip(), set())
            adapted_ingredients = [
                SAFE_SUBSTITUTIONS.get(ing, f"{rule.lower()}-safe substitute")
                if ing in forbidden
                else ing
                for ing in adapted_ingredients
            ]

        cook_time = min(15, req.max_cook_time_minutes)
        response_model = RecipeResponse(
            recipe_name=f"Savory Skillet {' & '.join(adapted_ingredients[:2]).title()}",
            prep_time_minutes=10,
            cook_time_minutes=cook_time,
            dietary_tags=[r.lower() for r in req.dietary_restrictions],
            instructions=[
                f"Wash and chop {', '.join(adapted_ingredients)} into even bite-sized pieces.",
                "Heat 1 tbsp olive oil in a skillet over medium heat.",
                f"Saute {', '.join(adapted_ingredients)} for {cook_time} minutes; season with salt and pepper.",
                "Plate warm and serve immediately.",
            ],
            missing_ingredients=["olive oil", "kosher salt", "black pepper"],
        )

        payload = response_model.model_dump()
        if tool_context is not None:
            tool_context.state["last_recipe_name"] = payload["recipe_name"]
            tool_context.state["missing_ingredients"] = payload["missing_ingredients"]

        logger.info(
            "tool_succeeded",
            tool="generate_recipe_from_ingredients",
            outcome=payload["recipe_name"],
        )
        return payload


def check_dietary_compliance(
    ingredients: List[str],
    restrictions: Optional[List[str]] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    Audits a list of ingredients against allergies and dietary restrictions, suggesting safe swaps.

    Args:
        ingredients: Proposed kitchen ingredients to inspect.
        restrictions: Allergies or diet constraints (e.g., ["vegan", "nut-free", "gluten-free"]).
        tool_context: Optional ADK ToolContext used to persist verified dietary restrictions.

    Returns:
        Dictionary validated against DietaryComplianceResponse JSON Schema.
    """
    with tracer.start_as_current_span("tool.check_dietary_compliance") as span:
        try:
            req = DietaryComplianceRequest(
                ingredients=ingredients or [],
                restrictions=restrictions or [],
            )
        except ValidationError as exc:
            return ToolErrorResponse(
                error=f"Dietary check validation failed: {exc.errors()[0]['msg']}",
                recovery_instructions="Pass a non-empty list of ingredient names to check.",
                expected_json_schema=DietaryComplianceRequest.model_json_schema(),
            ).model_dump()

        conflicts: List[str] = []
        substitutions: Dict[str, str] = {}
        for rule in req.restrictions:
            forbidden = ALLERGEN_RULES.get(rule.lower().strip(), set())
            for raw_item in req.ingredients:
                item = raw_item.lower().strip()
                if item in forbidden and item not in conflicts:
                    conflicts.append(item)
                    substitutions[item] = SAFE_SUBSTITUTIONS.get(
                        item, f"certified {rule.lower()}-safe alternative"
                    )

        if tool_context is not None and req.restrictions:
            tool_context.state["dietary_restrictions"] = [r.lower() for r in req.restrictions]

        resp = DietaryComplianceResponse(
            compliant=len(conflicts) == 0,
            conflicts=conflicts,
            substitutions=substitutions,
        )
        safe_set_span_attributes(
            span, {"tool.compliant": resp.compliant, "tool.conflict_count": len(conflicts)}
        )
        return resp.model_dump()


def order_missing_groceries(
    items: List[str],
    estimated_cost_usd: float = 12.50,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    Places a grocery order for missing dinner ingredients AFTER Human-in-the-Loop confirmation.

    Args:
        items: Non-empty list of grocery items to purchase.
        estimated_cost_usd: Estimated USD cost (between 0.00 and 500.00).
        tool_context: ADK ToolContext used to pause execution and request human confirmation.

    Returns:
        Dictionary describing either pending HITL confirmation or completed order details.
    """
    with tracer.start_as_current_span("tool.order_missing_groceries") as span:
        try:
            req = GroceryOrderRequest(items=items or [], estimated_cost_usd=estimated_cost_usd)
        except ValidationError as exc:
            return ToolErrorResponse(
                error=f"Grocery order validation failed: {exc.errors()[0]['msg']}",
                recovery_instructions="Provide at least one grocery item and a cost between 0 and 500.",
                expected_json_schema=GroceryOrderRequest.model_json_schema(),
            ).model_dump()

        # Pause for Human-in-the-Loop confirmation when running inside ADK ToolContext
        if tool_context is not None:
            if not (tool_context.tool_confirmation and tool_context.tool_confirmation.confirmed):
                tool_context.request_confirmation(
                    hint=(
                        f"Approve grocery delivery order for {', '.join(req.items)} "
                        f"(estimated cost: ${req.estimated_cost_usd:.2f})?"
                    ),
                    payload={"items": req.items, "estimated_cost_usd": req.estimated_cost_usd},
                )
                logger.info(
                    "hitl_confirmation_requested",
                    tool="order_missing_groceries",
                    items=req.items,
                    estimated_cost_usd=req.estimated_cost_usd,
                )
                return {
                    "status": "pending_human_approval",
                    "message": "Paused for Human-in-the-Loop confirmation before charging order.",
                    "items": req.items,
                    "estimated_cost_usd": req.estimated_cost_usd,
                }

        safe_set_span_attributes(
            span, {"order.items": req.items, "order.estimated_cost_usd": req.estimated_cost_usd}
        )
        logger.info("grocery_order_placed", items=req.items, cost_usd=req.estimated_cost_usd)
        return {
            "status": "order_placed",
            "items_ordered": req.items,
            "charged_usd": req.estimated_cost_usd,
        }


# Native ADK FunctionTool wrapper enforcing Human-in-the-Loop confirmation
order_groceries_hitl_tool = FunctionTool(
    order_missing_groceries,
    require_confirmation=True,
)

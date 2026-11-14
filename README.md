# Dinner Planning Agent v2 (Google ADK Multi-Agent System)

Production-grade **Concierge Dinner Planning Multi-Agent System** built with the **Google Agent Development Kit (ADK)** (`google-adk`). It transforms household kitchen ingredients into structured, allergen-safe dinner plans with Human-in-the-Loop grocery approvals, persistent SQLite/Cloud SQL session state, asynchronous context compaction, PII-scrubbed structured JSON logging, and OpenTelemetry tracing.

## Key Capabilities & AgentOps Architecture

- **Hierarchical Multi-Agent Orchestration (`dinner_agent/agent.py`)**:
  - `dinner_planner_coordinator` (Root Coordinator) delegates dynamically across:
    - `nutrition_dietary_specialist` (`gemini-2.5-pro`) for complex allergy/dietary auditing and safe culinary substitutions.
    - `culinary_recipe_chef` (`gemini-2.5-flash`) for fast weeknight recipe generation and Human-in-the-Loop grocery orders.
- **Explicit Pydantic v2 JSON Schemas (`dinner_agent/tools.py`)**: Strict input/output validation (`RecipeRequest`, `RecipeResponse`, `DietaryComplianceRequest`, `GroceryOrderRequest`), exported `TOOL_JSON_SCHEMAS`, and guided recovery instructions on validation errors.
- **Persistent Sessions & Async Memory**: Uses ADK's `DatabaseSessionService` (`sqlite+aiosqlite:///./dinner_sessions.db` locally or Postgres in production) + `InMemoryMemoryService` + asynchronous sliding-window conversation compaction (`compact_history_async`).
- **Guardrails & Human-in-the-Loop (HITL)**: `before_model_callback` blocks prompt injections and unsafe/inedible substances; `before_tool_callback` enforces dietary profiles and pauses orders over $50; `FunctionTool(..., require_confirmation=True)` + `tool_context.request_confirmation()` requires explicit human approval before ordering groceries.
- **Observability, Tracing & PII Redaction (`dinner_agent/observability.py`)**: `structlog` JSON output correlated with OpenTelemetry `trace_id`/`span_id` and regex PII scrubbing (`EMAIL`, `PHONE`, `SSN`, `CREDIT_CARD`, `API_KEY`).

## Repository Structure

```text
dinner-planning-agent-v2/
├── dinner_agent/
│   ├── __init__.py               # Exposes root_agent for ADK CLI discovery
│   ├── agent.py                  # Multi-agent coordinator, guardrails, persistent sessions & async memory
│   ├── tools.py                  # Pydantic JSON-schema validated tools & HITL grocery ordering
│   └── observability.py          # structlog JSON logging, PII redactor & OpenTelemetry tracing
├── tests/
│   └── test_agent.py             # Automated golden dataset, schema, guardrail, HITL & memory tests
├── eval/
│   ├── dinner_agent.evalset.json # ADK CLI trajectory & response evaluation dataset
│   └── test_config.json          # Evaluation score thresholds
├── deployment/
│   └── terraform/                # Cloud Run v2, Secret Manager & least-privilege IAM IaC
├── .github/workflows/ci.yml      # Automated GitHub Actions CI pipeline
├── Dockerfile                    # Non-root production container running adk api_server
├── agent-config.yaml             # Declarative AgentOps manifest
└── requirements.txt
```

## Quickstart & Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
export GEMINI_API_KEY="your-gemini-api-key"
export SESSION_DB_URL="sqlite+aiosqlite:///./dinner_sessions.db"
```

## Google ADK Agent CLI Usage

Run all `adk` CLI commands directly from the repository root (`dinner-planning-agent-v2/`):

```bash
# 1. Interactive Terminal Session via ADK CLI
adk run dinner_agent

# 2. Launch ADK Web Playground UI with Persistent SQLite Session Storage
adk web --session_service_uri="sqlite+aiosqlite:///./dinner_sessions.db"

# 3. Run Automated Golden-Dataset & Trajectory Evaluation via ADK CLI
adk eval dinner_agent eval/dinner_agent.evalset.json --config_file_path=eval/test_config.json

# 4. Start Local FastAPI Production Server
adk api_server . --host 0.0.0.0 --port 8080
```

## Running the Automated Test Suite

```bash
pytest -v tests/test_agent.py
```

## Cloud Deployment (ADK CLI, Docker & Terraform IaC)

### Option 1: One-Command Cloud Run Deploy with ADK CLI

```bash
adk deploy cloud_run \
  --project="$GOOGLE_CLOUD_PROJECT" \
  --region="us-central1" \
  --service_name="dinner-planning-agent-v2" \
  dinner_agent
```

### Option 2: Infrastructure as Code with Terraform

```bash
# Build and push container image to Artifact Registry
gcloud builds submit --tag "us-central1-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/agents/dinner-planning-agent-v2:latest"

# Provision Secret Manager, Least-Privilege Service Account, and Cloud Run v2 Service
cd deployment/terraform
terraform init
terraform plan \
  -var="project_id=${GOOGLE_CLOUD_PROJECT}" \
  -var="container_image=us-central1-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/agents/dinner-planning-agent-v2:latest"
terraform apply
```

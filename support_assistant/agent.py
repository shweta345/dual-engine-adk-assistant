import os
from pathlib import Path
from typing import Any, Optional
import requests
from google.adk.agents import LlmAgent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.cloud import discoveryengine_v1beta as discoveryengine

# Load declarative routing & conflict-resolution rules from SKILL.md
SKILL_FILE = Path(__file__).parent / "skills" / "cloud-support-triage" / "SKILL.md"
SKILL_INSTRUCTIONS = SKILL_FILE.read_text(encoding="utf-8")


# Tool 1: Private Knowledge Engine (Agent Search over Internal Playbooks)
def search_private_playbook(search_query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Searches internal company runbooks in Agent Search for client architecture,
    Cloud SQL connection limits, FinOps min-instances rules, approvers, and CI/CD policies.

    Args:
        search_query: Keywords to search in the private runbook (e.g. 'Acme frontend cold start').
    """
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    engine_id = os.environ.get("ENGINE_ID")

    chunks = []
    try:
        client = discoveryengine.SearchServiceClient()
        serving_config = (
            f"projects/{project_id}/locations/global/collections/"
            f"default_collection/engines/{engine_id}/servingConfigs/default_search"
        )
        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=search_query,
            page_size=3,
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True
                ),
            ),
        )
        response = client.search(request)

        for result in response.results:
            doc_data = result.document.derived_struct_data
            if "snippets" in doc_data:
                for s in doc_data["snippets"]:
                    chunks.append(s.get("snippet", ""))
    except Exception:
        pass

    extracted = "\n".join(filter(None, chunks))
    if not extracted:
        for candidate in [Path("acme-cloud-runbook.txt"), Path(__file__).parent.parent / "acme-cloud-runbook.txt"]:
            if candidate.exists():
                extracted = candidate.read_text(encoding="utf-8")
                break

    # Record in ADK session state that private playbook context has been checked
    tool_context.state["playbook_checked"] = True
    tool_context.state["last_private_query"] = search_query

    return {
        "source": "INTERNAL_PLAYBOOK (Agent Search)",
        "authority_domain": "POLICY_BUDGETS_AND_LIMITS",
        "playbook_context": extracted or "No matching internal runbook found.",
    }


# Tool 2: Public Knowledge Engine (Google Developer Knowledge API)
def search_public_dkp_api(technical_gcp_query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Queries the official Google Developer Knowledge API (developerknowledge.googleapis.com)
    for live, authoritative Google Cloud documentation, valid gcloud CLI flags, and quotas.

    Args:
        technical_gcp_query: A clean, de-jargoned Google Cloud technical question. MUST NOT
            contain private client names (e.g., 'Acme') or internal service names.
    """
    api_key = os.environ.get("DEVELOPERKNOWLEDGE_API_KEY")
    url = f"https://developerknowledge.googleapis.com/v1:answerQuery?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {"query": technical_gcp_query}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        answer_text = data.get("answer", {}).get("answerText", "No official guidance found.")
    except Exception as e:
        answer_text = f"Error querying Developer Knowledge API: {e}"

    tool_context.state["last_dkp_query"] = technical_gcp_query

    return {
        "source": "OFFICIAL_GCP_DOCS (Google Developer Knowledge API)",
        "authority_domain": "GCP_MECHANICS_AND_CLI_SYNTAX",
        "de_jargoned_query_sent": technical_gcp_query,
        "official_guidance": answer_text,
    }


# ADK Deterministic Guardrail Callback: Prevent Private Jargon Leaks to Public DKP API
def enforce_dejargoned_dkp_query(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> Optional[dict[str, Any]]:
    """Intercepts calls to search_public_dkp_api before network execution to ensure
    private client names or internal resource IDs are never leaked to the public API.
    """
    if tool.name == "search_public_dkp_api":
        raw_query = args.get("technical_gcp_query", "")
        forbidden_terms = ["acme", "acme-frontend-prod", "acme-db-prod", "sarah connor"]
        leaked = [t for t in forbidden_terms if t in raw_query.lower()]
        if leaked:
            return {
                "error": (
                    f"GUARDRAIL BLOCKED: Query '{raw_query}' contains private terms {leaked}. "
                    "Rewrite 'technical_gcp_query' using ONLY public Google Cloud product names "
                    "(e.g., 'Cloud Run', 'Cloud SQL', 'Serverless VPC Access') and retry."
                )
            }
    return None


# Define the Google ADK Root Agent
root_agent = LlmAgent(
    name="support_triage_agent",
    model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    description=(
        "Dual-engine Cloud Support Assistant that routes between private runbooks "
        "(Agent Search) and official GCP documentation (Developer Knowledge API)."
    ),
    instruction=SKILL_INSTRUCTIONS,
    tools=[search_private_playbook, search_public_dkp_api],
    before_tool_callback=enforce_dejargoned_dkp_query,
)

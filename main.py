"""Customer support agent built with Amazon Bedrock AgentCore and Strands.

Combines Gateway tools for order and refund operations with knowledge-base
retrieval, customer memory, sandboxed loyalty calculations, and web browsing.
Running this module starts the AgentCore runtime server.
"""

import argparse
import asyncio
import json
import logging
import os
import uuid
from typing import Dict

import boto3
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.code_interpreter_client import code_session
from mcp.client.streamable_http import streamable_http_client
from strands import Agent, tool
from strands.hooks import (
    AfterInvocationEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from strands_tools.browser import AgentCoreBrowser

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")
logger.setLevel(logging.INFO)

app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"

GATEWAY_URL = "https://customersupportgateway-ejwdbxrvaz.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID = "BXE3K82WUZ"
REGION = "us-east-1"
MEMORY_ID = "CustomerSupportMemory-jSmFS72Irc"

model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id)

memory_client = MemoryClient(region_name=REGION)

_bedrock_runtime = boto3.client(
    "bedrock-agent-runtime",
    region_name=REGION,
)


def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)

    return {
        strategy["type"]: strategy["namespaces"][0]
        for strategy in strategies
        if strategy.get("type") and strategy.get("namespaces")
    }


class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id

        self.namespaces = get_namespaces(
            memory_client,
            memory_id,
        )

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""

        message = event.agent.messages[-1]

        if message["role"] != "user":
            return

        content = message["content"][0]

        if "text" not in content or "toolResult" in content:
            return

        query = content["text"]
        memories = []

        # Scope retrieval to this customer across sessions.
        for strategy_type, namespace_template in self.namespaces.items():
            namespace = namespace_template.format(actorId=self.actor_id)

            results = self.memory_client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=namespace,
                query=query,
                top_k=5,
            )

            for result in results:
                text = result["content"]["text"]

                if text:
                    memories.append(f"[{strategy_type}] {text}")

        if memories:
            context = "\n".join(memories)

            message["content"][0]["text"] = f"Customer Context:\n{context}\n\n{query}"

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""

        customer_query = None
        agent_response = None

        # Skip tool exchanges and select the latest text from each participant.
        for message in reversed(event.agent.messages):
            content = message["content"][0]

            if (
                message["role"] == "assistant"
                and agent_response is None
                and "text" in content
            ):
                agent_response = content["text"]

            if (
                message["role"] == "user"
                and customer_query is None
                and "text" in content
                and "toolResult" not in content
            ):
                customer_query = content["text"]

            if customer_query and agent_response:
                break

        if customer_query and agent_response:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[
                    (customer_query, "USER"),
                    (agent_response, "ASSISTANT"),
                ],
            )

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""

        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)

        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """

    if not KB_ID:
        return "Knowledge base not configured."

    response = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID, retrievalQuery={"text": query}
    )

    results = response["retrievalResults"]

    if not results:
        return "No relevant information found."

    return "\n---\n".join(result["content"]["text"] for result in results)


@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs the calculation in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        JSON-encoded interpreter result, or a tier-only discount if execution fails.
    """

    code = f"""
import json
import math

earn_rates = {{
    "standard": 1,
    "device": 2,
    "fresh": 5
}}

tier_rates = {{
    "Silver": 0.00,
    "Gold": 0.10,
    "Platinum": 0.15
}}

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

points_redeemed = (loyalty_points // 500) * 500

max_points = int(order_total * 0.50 * 100)
max_points = (max_points // 500) * 500

points_redeemed = min(
    points_redeemed,
    max_points
)

points_discount = points_redeemed / 100

subtotal = order_total - points_discount

tier_discount = (
    subtotal * tier_rates[tier]
)

final_total = (
    subtotal - tier_discount
)

total_savings = (
    points_discount + tier_discount
)

points_earned = math.floor(
    final_total * earn_rates[product_category]
)

remaining_points = (
    loyalty_points
    - points_redeemed
    + points_earned
)

result = {{
    "points_redeemed": points_redeemed,
    "tier_discount": round(tier_discount, 2),
    "final_total": round(final_total, 2),
    "total_savings": round(total_savings, 2),
    "points_earned": points_earned,
    "remaining_points": remaining_points
}}

print(json.dumps(result))
    """

    try:
        with code_session(REGION) as client:
            response = client.invoke(
                "executeCode",
                {
                    "language": "python",
                    "code": code,
                    "clearContext": True,
                },
            )

            for event in response["stream"]:
                if "result" in event:
                    return json.dumps(event["result"])

    except Exception:
        # If sandbox execution fails, apply only the tier discount.

        tier_rates = {
            "Silver": 0.00,
            "Gold": 0.10,
            "Platinum": 0.15,
        }

        discount = order_total * tier_rates[tier]

        return json.dumps(
            {
                "tier_discount": round(discount, 2),
                "final_total": round(order_total - discount, 2),
            }
        )


@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """

    user_input = payload["prompt"]
    actor_id = payload.get("customer_id", "anonymous")
    session_id = payload.get("session_id", str(uuid.uuid4()))

    memory_hook = MemoryHook(
        actor_id,
        session_id,
        memory_client,
        MEMORY_ID,
    )

    browser = AgentCoreBrowser(region=REGION)

    tools = [
        search_knowledge_base,
        calculate_loyalty_discount,
        browser.browser,
    ]

    # Connection setup can fail before the context manager is entered.
    try:
        mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        # Keep the Gateway connection open while the agent calls its tools.
        with mcp_client:
            gateway_tools = mcp_client.list_tools_sync()
            tools.extend(gateway_tools)
            logger.info(
                "Gateway connected successfully. Loaded %d tools.",
                len(gateway_tools),
            )

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=(
                    "You are a helpful customer support assistant "
                    "for an e-commerce platform. "
                    "Use the available tools when needed."
                ),
            )
            response = agent(user_input)
            return response.message["content"][0]["text"]
    except TimeoutError:
        logger.exception("Gateway request timed out")
        return "The support service is unavailable. Please contact customer support."

    except ConnectionError:
        logger.exception("Gateway connection failed")
        return "The support service is unavailable. Please contact customer support."

    except Exception as exc:
        logger.exception("Gateway or agent request failed: %s", exc)
        return "The support service is unavailable. Please contact customer support."


def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()

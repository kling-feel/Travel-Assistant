"""Model and optional web search configuration without source-code secrets."""

import os

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from config import settings  # Loads the ignored local .env before reading AI settings.


@tool
def search_public_web(query: str) -> str:
    """Search public web pages when a Tavily key has been configured."""
    if not os.getenv("TAVILY_API_KEY"):
        return "未配置公共网页搜索；请使用本地业务数据或说明无法核实。"
    from langchain_community.tools import TavilySearchResults

    return str(TavilySearchResults(max_results=2).invoke({"query": query}))


api_key = os.getenv("AI_API_KEY")
base_url = os.getenv("AI_BASE_URL")
llm = ChatOpenAI(
    model=os.getenv("AI_MODEL", "local-model"),
    api_key=api_key or "demo-not-configured",
    base_url=base_url or ("http://127.0.0.1:6006/v1" if not api_key else None),
    temperature=0,
)
tavily_tool = search_public_web

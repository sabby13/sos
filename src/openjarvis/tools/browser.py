"""Browser tools: open a URL, or run a web search."""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urlparse

from pydantic import BaseModel, Field

from ..errors import ToolError
from .base import RiskLevel, Tool, ToolContext, ToolResult

# Schemes that can execute code or read local files if a page talks Jarvis into one.
_BLOCKED_SCHEMES = {"file", "javascript", "data", "vbscript", "about"}
_LOOKS_LIKE_DOMAIN = re.compile(r"^[\w-]+(\.[\w-]+)+(/.*)?$")


class OpenUrlArgs(BaseModel):
    url: str = Field(
        description="Web address to open. A bare domain like 'github.com' is fine - https "
        "is assumed."
    )


class OpenUrl(Tool[OpenUrlArgs]):
    name = "open_url"
    description = """
    Open a web page in the user's browser.
    Use this when the user names a site or gives a link. If they are asking a question
    rather than naming a destination, use web_search instead.
    """
    risk = RiskLevel.CAUTION
    args_model = OpenUrlArgs

    def preview(self, args: OpenUrlArgs, ctx: ToolContext) -> str:
        return f"Open {args.url} in the browser"

    def run(self, args: OpenUrlArgs, ctx: ToolContext) -> ToolResult:
        url = normalise_url(args.url)
        ok = ctx.platform.open_url(url, ctx.settings.tools.browser)
        if not ok:
            raise ToolError(f"No browser was available to open {url}.")
        return ToolResult.success(f"Opened {urlparse(url).netloc or url}.", {"url": url})


class WebSearchArgs(BaseModel):
    query: str = Field(description="What to search for, in natural language.")


class WebSearch(Tool[WebSearchArgs]):
    name = "web_search"
    description = """
    Open a web search for a query in the user's browser.
    This shows results to the user - it does not read them back to you, so do not use it
    to answer a factual question yourself. Answer from your own knowledge instead, and
    only search when the user wants to *see* results or the topic is recent news.
    """
    risk = RiskLevel.CAUTION
    args_model = WebSearchArgs

    def preview(self, args: WebSearchArgs, ctx: ToolContext) -> str:
        return f"Search the web for: {args.query}"

    def run(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        query = args.query.strip()
        if not query:
            raise ToolError("An empty search query was given.")
        template = ctx.settings.tools.search_engine
        url = template.format(query=quote_plus(query))
        if not ctx.platform.open_url(url, ctx.settings.tools.browser):
            raise ToolError(f"No browser was available to search for {query!r}.")
        return ToolResult.success(f"Searching the web for '{query}'.", {"url": url})


def normalise_url(raw: str) -> str:
    """Add a scheme where a person would omit one, and refuse dangerous schemes."""
    url = raw.strip().strip('"').strip("'")
    if not url:
        raise ToolError("An empty URL was given.")

    parsed = urlparse(url)
    if parsed.scheme:
        if parsed.scheme.lower() in _BLOCKED_SCHEMES:
            raise ToolError(
                f"The '{parsed.scheme}' scheme is not allowed in the browser. "
                "Use open_file for local files."
            )
        return url

    if _LOOKS_LIKE_DOMAIN.match(url) or url.startswith("localhost"):
        return f"https://{url}"
    raise ToolError(f"{raw!r} is not a URL. Use web_search to search for it instead.")


BROWSER_TOOLS = [OpenUrl(), WebSearch()]

"""Browser tools: open a URL, or run a web search."""

from __future__ import annotations

import json
import re
from urllib.error import URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

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


class PlayYouTubeArgs(BaseModel):
    query: str = Field(
        description="Song, artist, or music search to play on YouTube. Include both the "
        "song and artist when the user names a particular song."
    )


class PlayYouTube(Tool[PlayYouTubeArgs]):
    name = "play_youtube"
    description = """
    Play music on YouTube. Use this when the user asks to play or listen to a song,
    artist, or music on YouTube. This finds a YouTube video and opens its watch page
    directly; do not substitute a web search or just open YouTube's search results.
    """
    risk = RiskLevel.CAUTION
    args_model = PlayYouTubeArgs

    def preview(self, args: PlayYouTubeArgs, ctx: ToolContext) -> str:
        return f"Find and play '{args.query}' on YouTube"

    def run(self, args: PlayYouTubeArgs, ctx: ToolContext) -> ToolResult:
        query = args.query.strip()
        if not query:
            raise ToolError("An empty YouTube music query was given.")

        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        request = Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=12) as response:
                page = response.read(2_500_000).decode("utf-8", errors="replace")
        except (OSError, TimeoutError, URLError) as exc:
            raise ToolError(f"Could not search YouTube for {query!r}: {exc}") from exc

        video = _first_youtube_video(page)
        if video is None:
            raise ToolError(f"YouTube returned no playable video for {query!r}.")

        video_id, title, playlist_id = video
        url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
        if playlist_id:
            url = (
                f"https://www.youtube.com/watch?v={video_id}"
                f"&list={playlist_id}&start_radio=1&autoplay=1"
            )
        if not ctx.platform.open_url(url, ctx.settings.tools.browser):
            raise ToolError(f"No browser was available to play {title!r} on YouTube.")

        return ToolResult.success(
            f"Opened '{title}' on YouTube for playback.",
            {"title": title, "url": url},
        )


def _first_youtube_video(page: str) -> tuple[str, str, str | None] | None:
    """Find the first playable video in YouTube's embedded search-result data."""
    for match in re.finditer(r'"videoRenderer"\s*:\s*\{', page):
        start = match.end() - 1
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(page)):
            char = page[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        renderer = json.loads(page[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    video_id = renderer.get("videoId")
                    title_data = renderer.get("title", {})
                    title_runs = title_data.get("runs", []) if isinstance(title_data, dict) else []
                    title = title_runs[0].get("text", "") if title_runs else ""
                    navigation = renderer.get("navigationEndpoint", {})
                    watch = (
                        navigation.get("watchEndpoint", {})
                        if isinstance(navigation, dict)
                        else {}
                    )
                    playlist_id = watch.get("playlistId") if isinstance(watch, dict) else None
                    if (
                        isinstance(video_id, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                        and isinstance(title, str)
                        and title.strip()
                        and not re.search(r"\b(mix|playlist|full album|compilation)\b", title, re.I)
                    ):
                        if not isinstance(playlist_id, str) or not re.fullmatch(
                            r"RD[A-Za-z0-9_-]+", playlist_id
                        ):
                            playlist_id = None
                        return video_id, title.strip(), playlist_id
                    break
    return None


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


BROWSER_TOOLS = [OpenUrl(), WebSearch(), PlayYouTube()]

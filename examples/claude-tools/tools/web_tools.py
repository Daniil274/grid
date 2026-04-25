"""
Web Tools — loading web pages and searching the internet.

web_fetch  — loads a page and converts it to markdown
web_search — web search via Firecrawl API (requires FIRECRAWL_API_KEY)
"""

import asyncio
import ipaddress
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool


# Optional dependencies
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import markdownify
    HAS_MARKDOWNIFY = True
except ImportError:
    HAS_MARKDOWNIFY = False


MAX_CONTENT_LENGTH = 100_000
DEFAULT_TIMEOUT = 30

# Schemes that must never reach the network layer
BLOCKED_SCHEMES = {
    "file", "ftp", "gopher", "dict", "ldap", "tftp",
    "data", "javascript", "vbscript", "jar",
}

# Hostnames always blocked (plus IP checks below)
BLOCKED_HOSTNAMES = {"localhost", "0.0.0.0", ""}


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def _validate_url(url: str) -> Optional[str]:
    """
    Returns an error string if the URL should be blocked, None if it is safe.
    Blocks: non-http(s) schemes, loopback, private, link-local, reserved IPs.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return f"❌ Invalid URL: {url}"

    if not parsed.scheme or not parsed.netloc:
        return f"❌ Invalid URL: {url}"

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        if scheme in BLOCKED_SCHEMES:
            return f"❌ Protocol '{scheme}://' is blocked"
        return f"❌ Only http:// and https:// are supported"

    host = (parsed.hostname or "").lower()
    if host in BLOCKED_HOSTNAMES:
        return f"❌ Access to '{host}' is blocked"

    # Block private / loopback / link-local / reserved IP addresses (SSRF prevention)
    try:
        addr = ipaddress.ip_address(host)
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return f"❌ Access to internal address is blocked: {host}"
    except ValueError:
        pass  # Not an IP address — hostname, proceed

    return None


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

def _run_async(coro, timeout: float = DEFAULT_TIMEOUT + 10):
    """Safely run a coroutine from sync context."""
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=timeout)
    except RuntimeError:
        return asyncio.run(coro)


async def _fetch_url(url: str, timeout: int) -> tuple:
    """Returns (status_code, content, error_message)."""
    if not HAS_AIOHTTP:
        return 0, "", "aiohttp is not installed. Install: pip install aiohttp"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            ct = response.headers.get("Content-Type", "")
            if "text/html" in ct or "text/plain" in ct:
                text = await response.text()
                return response.status, text, ""
            return response.status, "", f"Unsupported Content-Type: {ct}"


def _truncate(content: str, max_len: int = MAX_CONTENT_LENGTH) -> str:
    if len(content) <= max_len:
        return content
    return content[:max_len] + f"\n\n... [truncated {len(content) - max_len} characters] ..."


def _html_to_markdown(html: str) -> str:
    if not HAS_BS4:
        import re
        return re.sub(r"<[^>]+>", "", html)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.find("div", class_="content")
    target = main if main else soup

    if HAS_MARKDOWNIFY:
        try:
            return markdownify.markdownify(str(target), heading_style="ATX")
        except Exception:
            pass

    lines = [line.strip() for line in target.get_text(separator="\n").split("\n") if line.strip()]
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------

@function_tool
def web_fetch(
    url: str,
    render_js: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """
    Loads a web page and converts it to markdown.

    Args:
        url:       URL to load (only http:// and https://)
        render_js: JS rendering via Firecrawl (requires FIRECRAWL_API_KEY)
        timeout:   Timeout in seconds

    Returns:
        Page content in markdown format
    """
    err = _validate_url(url)
    if err:
        return err

    if render_js:
        return _fetch_with_firecrawl(url, timeout)

    if not HAS_AIOHTTP:
        return "❌ aiohttp is not installed. Install: pip install aiohttp"

    try:
        status, content, error = _run_async(_fetch_url(url, timeout), timeout=timeout + 10)
    except Exception as exc:
        return f"❌ Load error: {exc}"

    if error:
        return f"❌ {error}"
    if status != 200:
        return f"❌ HTTP {status} when loading {url}"

    title = ""
    if HAS_BS4:
        soup = BeautifulSoup(content, "html.parser")
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)

    header = f"# {title}\n\n" if title else ""
    markdown = _html_to_markdown(content)
    return _truncate(header + f"Source: {url}\n\n" + markdown)


def _fetch_with_firecrawl(url: str, timeout: int) -> str:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return "❌ FIRECRAWL_API_KEY is required for JS rendering"
    if not HAS_AIOHTTP:
        return "❌ aiohttp is not installed. Install: pip install aiohttp"

    async def _do():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"url": url, "formats": ["markdown"]},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    return f"❌ Firecrawl API error: {response.status}"
                data = await response.json()
                if not data.get("success"):
                    return f"❌ Firecrawl error: {data.get('error', 'Unknown')}"
                result = data.get("data", {})
                title = result.get("metadata", {}).get("title", "")
                md = result.get("markdown", "")
                header = f"# {title}\n\n" if title else ""
                return _truncate(header + f"Source: {url}\n\n" + md)

    try:
        return _run_async(_do(), timeout=timeout + 10)
    except Exception as exc:
        return f"❌ Firecrawl error: {exc}"


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

@function_tool
def web_search(
    query: str,
    max_results: int = 5,
) -> str:
    """
    Performs a web search via the Firecrawl API.

    Requires FIRECRAWL_API_KEY in environment variables.

    Args:
        query:       Search query
        max_results: Number of results (1–10)

    Returns:
        Search results
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return (
            "❌ FIRECRAWL_API_KEY is required for web search.\n"
            "Get a free key at https://firecrawl.dev\n"
            "Then: export FIRECRAWL_API_KEY=your_key"
        )
    if not HAS_AIOHTTP:
        return "❌ aiohttp is not installed. Install: pip install aiohttp"

    max_results = max(1, min(10, max_results))

    async def _do():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.firecrawl.dev/v1/search",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "query": query,
                    "limit": max_results,
                    "scrapeOptions": {"formats": ["markdown"]},
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    return f"❌ Firecrawl API error: {response.status}"
                data = await response.json()
                if not data.get("success"):
                    return f"❌ Firecrawl error: {data.get('error', 'Unknown')}"

                items = data.get("data", [])
                if not items:
                    return f"🔍 Nothing found for query '{query}'"

                lines = [f"🔍 Search results for '{query}'\n"]
                for i, item in enumerate(items, 1):
                    title = item.get("title", "Untitled")
                    item_url = item.get("url", "")
                    desc = item.get("description", "")
                    md = item.get("markdown", "")

                    lines.append(f"\n## {i}. {title}")
                    lines.append(f"URL: {item_url}")
                    if desc:
                        lines.append(f"\n{desc}")
                    if md:
                        snippet = md[:1000] + ("..." if len(md) > 1000 else "")
                        lines.append(f"\n{snippet}")

                return "\n".join(lines)

    try:
        return _run_async(_do(), timeout=70)
    except Exception as exc:
        return f"❌ Search error: {exc}"

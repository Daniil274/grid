"""A real Chromium for agents: pages read as text with element references, acted on by them.

The browser runs on its own persistent profile, so logins survive a restart
of the machine. An agent reads a page as Playwright's AI snapshot - the
accessibility tree with a reference on every element, ``[ref=e12]`` - and
acts on an element by its reference; a model that sees can also take a
screenshot and click by coordinates. A person watching gets the page as a
stream of frames (Chrome's screencast) and can take the mouse and keyboard.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from grid_node.node import LOOK, Content, Driver, ToolSpec, ViewSink, image, text

logger = logging.getLogger("grid.node.browser")

#: The longest snapshot an agent gets in one call; the rest is cut with a note.
MAX_SNAPSHOT = 40_000
VIEWPORT = {"width": 1280, "height": 800}
#: How long one action waits for its element.
ACTION_TIMEOUT_MS = 15_000


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


class BrowserDriver(Driver):
    name = "browser"
    capabilities = ["browser", "screen"]

    def __init__(self, profile: Path, *, headless: bool = False, executable: Optional[str] = None) -> None:
        self.profile = Path(profile)
        self.headless = headless
        self.executable = executable
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._sink: Optional[ViewSink] = None
        self._cdp: Any = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile),
            headless=self.headless,
            executable_path=self.executable,
            viewport=VIEWPORT,
            locale=os.environ.get("NODE_LOCALE") or None,
            timezone_id=os.environ.get("TZ") or None,
            # A browser like a person's: no automation banner or navigator.webdriver.
            ignore_default_args=["--enable-automation"],
            # The container is the sandbox; Chrome's own needs privileges it does not have.
            args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context.on("page", self._adopt)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def stop(self) -> None:
        await self.stop_view()
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()

    def _adopt(self, page: Any) -> None:
        """A tab the page opened (a link to a new window) becomes the one acted on."""
        self._page = page
        if self._sink is not None:
            asyncio.ensure_future(self._restart_view())

    @property
    def page(self) -> Any:
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else None
        return self._page

    async def _ensure_page(self) -> Any:
        if self.page is None:
            self._page = await self._context.new_page()
        return self._page

    async def _where(self) -> str:
        page = await self._ensure_page()
        try:
            title = await page.title()
        except Exception:
            title = ""
        return f"{title or '(no title)'} - {page.url}"

    def _target(self, page: Any, ref: str) -> Any:
        return page.locator(f"aria-ref={ref}")

    # -- tools ------------------------------------------------------------

    def tools(self) -> List[ToolSpec]:
        ref = {"type": "string", "description": "The element's reference from browser_snapshot, e.g. e12"}
        return [
            ToolSpec("browser_navigate", "Open a URL in the current tab.",
                     _schema({"url": {"type": "string"}}, ["url"]), self.navigate),
            ToolSpec("browser_snapshot",
                     "Read the current page as an accessibility tree; every element carries [ref=…] to act on.",
                     _schema({}), self.snapshot, LOOK),
            ToolSpec("browser_screenshot", "Take a screenshot of the current page (for a model that sees).",
                     _schema({"full_page": {"type": "boolean", "default": False}}), self.screenshot, LOOK),
            ToolSpec("browser_click", "Click an element by its ref, or a point by x and y (CSS pixels).",
                     _schema({"ref": ref, "x": {"type": "number"}, "y": {"type": "number"},
                              "double": {"type": "boolean", "default": False}}), self.click),
            ToolSpec("browser_type", "Type text into an element by its ref; submit presses Enter after it.",
                     _schema({"ref": ref, "text": {"type": "string"}, "submit": {"type": "boolean", "default": False},
                              "clear": {"type": "boolean", "default": True}}, ["ref", "text"]), self.type),
            ToolSpec("browser_press", "Press a key or a combination, e.g. Enter, Escape, Control+A.",
                     _schema({"key": {"type": "string"}}, ["key"]), self.press),
            ToolSpec("browser_scroll", "Scroll the page by pixels (positive down/right).",
                     _schema({"dy": {"type": "number", "default": 600}, "dx": {"type": "number", "default": 0}}), self.scroll),
            ToolSpec("browser_history", "Go back or forward in the current tab.",
                     _schema({"direction": {"type": "string", "enum": ["back", "forward"]}}, ["direction"]), self.history),
            ToolSpec("browser_tabs", "List tabs, open a new one, switch to one or close one.",
                     _schema({"action": {"type": "string", "enum": ["list", "new", "select", "close"]},
                              "index": {"type": "integer"}, "url": {"type": "string"}}, ["action"]), self.tabs),
            ToolSpec("browser_wait", "Wait for text to appear on the page, or for some seconds (at most 30).",
                     _schema({"text": {"type": "string"}, "seconds": {"type": "number"}}), self.wait),
        ]

    async def navigate(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        url = str(args["url"]).strip()
        if "://" not in url:
            url = "https://" + url
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        status = f" (HTTP {response.status})" if response is not None and response.status >= 400 else ""
        return [text(f"Opened {await self._where()}{status}. Call browser_snapshot to read it.")]

    async def snapshot(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        tree = await page.aria_snapshot(mode="ai")
        if len(tree) > MAX_SNAPSHOT:
            tree = tree[:MAX_SNAPSHOT] + f"\n… [{len(tree) - MAX_SNAPSHOT} more characters; scroll or narrow down]"
        return [text(f"Page: {await self._where()}\n\n{tree}")]

    async def screenshot(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        data = await page.screenshot(full_page=bool(args.get("full_page")), type="png")
        return [text(f"Screenshot of {await self._where()}"), image(data)]

    async def click(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        count = 2 if args.get("double") else 1
        if args.get("ref"):
            await self._target(page, args["ref"]).click(click_count=count, timeout=ACTION_TIMEOUT_MS)
        elif args.get("x") is not None and args.get("y") is not None:
            await page.mouse.click(float(args["x"]), float(args["y"]), click_count=count)
        else:
            return [text("❌ Give ref, or x and y.")]
        await page.wait_for_load_state("domcontentloaded")
        return [text(f"Clicked. Now: {await self._where()}")]

    async def type(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        target = self._target(page, args["ref"])
        if args.get("clear", True):
            await target.fill(str(args["text"]), timeout=ACTION_TIMEOUT_MS)
        else:
            await target.press_sequentially(str(args["text"]), timeout=ACTION_TIMEOUT_MS)
        if args.get("submit"):
            await target.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
        return [text(f"Typed. Now: {await self._where()}")]

    async def press(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        await page.keyboard.press(str(args["key"]))
        return [text(f"Pressed {args['key']}. Now: {await self._where()}")]

    async def scroll(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        await page.mouse.wheel(float(args.get("dx", 0) or 0), float(args.get("dy", 600) or 0))
        return [text("Scrolled.")]

    async def history(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        if args["direction"] == "back":
            await page.go_back(wait_until="domcontentloaded")
        else:
            await page.go_forward(wait_until="domcontentloaded")
        return [text(f"Now: {await self._where()}")]

    async def tabs(self, args: Dict[str, Any]) -> List[Content]:
        action = args["action"]
        pages = [p for p in self._context.pages if not p.is_closed()]
        if action == "new":
            self._page = await self._context.new_page()
            if args.get("url"):
                return await self.navigate({"url": args["url"]})
        elif action in ("select", "close"):
            index = args.get("index")
            if not isinstance(index, int) or not 0 <= index < len(pages):
                return [text(f"❌ No tab {index}; there are {len(pages)}.")]
            if action == "select":
                self._page = pages[index]
                await self._page.bring_to_front()
            else:
                await pages[index].close()
            if self._sink is not None:
                await self._restart_view()
        lines = []
        for i, page in enumerate(p for p in self._context.pages if not p.is_closed()):
            marker = "*" if page is self.page else " "
            lines.append(f"{marker} {i}: {await page.title() or '(no title)'} - {page.url}")
        return [text("Tabs (* is the current one):\n" + "\n".join(lines))]

    async def wait(self, args: Dict[str, Any]) -> List[Content]:
        page = await self._ensure_page()
        if args.get("text"):
            await page.get_by_text(str(args["text"])).first.wait_for(timeout=30_000)
            return [text(f"\"{args['text']}\" is on the page.")]
        await asyncio.sleep(min(30.0, max(0.0, float(args.get("seconds", 1) or 1))))
        return [text(f"Waited. Now: {await self._where()}")]

    # -- live view ----------------------------------------------------------

    @property
    def has_view(self) -> bool:
        return True

    async def start_view(self, sink: ViewSink) -> None:
        self._sink = sink
        await self._restart_view()

    async def _restart_view(self) -> None:
        async with self._lock:
            await self._close_cdp()
            page = self.page
            if page is None or self._sink is None:
                return
            cdp = await self._context.new_cdp_session(page)
            sink = self._sink

            async def frame(params: Dict[str, Any]) -> None:
                try:
                    await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                    meta = params.get("metadata") or {}
                    await sink({
                        "type": "frame", "mime": "image/jpeg", "data": params["data"],
                        "width": meta.get("deviceWidth"), "height": meta.get("deviceHeight"),
                    })
                except Exception:
                    logger.debug("A screencast frame was not delivered", exc_info=True)

            cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(frame(params)))
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 800})
            self._cdp = cdp

    async def _close_cdp(self) -> None:
        if self._cdp is not None:
            try:
                await self._cdp.send("Page.stopScreencast")
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None

    async def stop_view(self) -> None:
        self._sink = None
        async with self._lock:
            await self._close_cdp()

    async def person_input(self, event: Dict[str, Any]) -> None:
        """A person's mouse and keys from the live view, in page coordinates."""
        page = self.page
        if page is None:
            return
        kind = event.get("kind")
        if kind == "click":
            await page.mouse.click(float(event["x"]), float(event["y"]), button=event.get("button", "left"))
        elif kind == "wheel":
            await page.mouse.wheel(float(event.get("dx", 0)), float(event.get("dy", 0)))
        elif kind == "key":
            await page.keyboard.press(str(event["key"]))
        elif kind == "text":
            await page.keyboard.insert_text(str(event["text"]))
        elif kind == "navigate":
            await self.navigate({"url": event["url"]})


def decode_frame(frame: Dict[str, Any]) -> bytes:
    """The JPEG bytes of a frame :meth:`BrowserDriver.start_view` sent."""
    return base64.b64decode(frame["data"])

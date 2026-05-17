"""
Unified perception: Windows UI Automation tree + annotated screenshot in one call.
Screenshot is saved to the screenshots/ dir; the agent receives the file path
and a visual copy. Use the path with crop_image() to zoom into specific regions.
"""

import io, base64, os, time, uuid
import win32gui
import pyautogui
from pathlib import Path
from PIL import Image, ImageDraw
from agents import function_tool
from agents.tool import ToolOutputImage, ToolOutputText

pyautogui.FAILSAFE = False

_COLORS = ["#E63946", "#457B9D", "#2A9D8F", "#E9C46A", "#F4A261",
           "#8338EC", "#06D6A0", "#FB5607", "#3A86FF", "#FF006E"]

# Per-process session dir: workspace/screenshots/<session_id>/
_SESSION_DIR: Path | None = None


def _session_dir() -> Path:
    global _SESSION_DIR
    if _SESSION_DIR is None:
        base = Path(os.environ.get("GRID_WORKSPACE", "workspace")) / "screenshots"
        sid = os.environ.get("GRID_SESSION_ID", uuid.uuid4().hex[:8])
        _SESSION_DIR = base / sid
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return _SESSION_DIR


def _save(img: Image.Image) -> Path:
    path = _session_dir() / f"screen_{int(time.time() * 1000)}.png"
    img.save(path, format="PNG")
    # Symlink/copy as last_screenshot.png so system crop_image() works without a path arg
    last = _session_dir().parent / "last_screenshot.png"
    try:
        if last.exists() or last.is_symlink():
            last.unlink()
        last.symlink_to(path.resolve())
    except Exception:
        img.save(last, format="PNG")
    return path


def _encode(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _find_hwnd(title: str):
    found = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and title.lower() in win32gui.GetWindowText(hwnd).lower():
            found.append(hwnd)
    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def _annotate(img: Image.Image, elements: list, offset_x=0, offset_y=0):
    draw = ImageDraw.Draw(img)
    for el in elements:
        i = el["i"]
        l = el["l"] - offset_x
        t = el["tp"] - offset_y
        r = l + el["w"]
        b = t + el["h"]
        iw, ih = img.size
        if r <= 0 or b <= 0 or l >= iw or t >= ih:
            continue
        color = _COLORS[(i - 1) % len(_COLORS)]
        draw.rectangle([l, t, r, b], outline=color, width=2)
        bx, by = max(l, 0), max(t - 14, 0)
        bw = max(14, len(str(i)) * 8)
        draw.rectangle([bx, by, bx + bw, by + 14], fill=color)
        draw.text((bx + 2, by + 1), str(i), fill="white")
    return img


@function_tool
def perceive(window: str = None):
    """
    Capture the current screen state as a unified view:
    - Windows UI Automation element list (type, name, exact center coordinates)
    - Screenshot annotated with numbered markers [N] matching each element
    - Screenshot saved to a file — pass the path to crop_image() to zoom in

    Call this at the start of every task step to understand what is on screen.
    Use element names from the list to call click() or type_into().

    Args:
        window: Optional window title substring. Omit to capture full desktop.
    """
    from _ps_uia import get_uia_elements

    offset_x, offset_y = 0, 0
    hwnd = None
    if window:
        hwnd = _find_hwnd(window)
        if hwnd:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            img = pyautogui.screenshot(region=(l, t, r - l, b - t))
            offset_x, offset_y = l, t
        else:
            img = pyautogui.screenshot()
    else:
        img = pyautogui.screenshot()

    elements = get_uia_elements(hwnd=hwnd)
    if elements:
        img = _annotate(img, elements, offset_x, offset_y)

    path = _save(img)

    if elements:
        lines = [
            f"Screenshot: {path} | {len(elements)} UIA elements",
            f"  {'#':>3}  {'Type':<12}  {'Name':<36}  Center",
        ]
        for el in elements[:60]:
            name = (el.get("n") or "")[:35]
            lines.append(f"  [{el['i']:2d}]  {el['t']:<12}  {name:<36}  ({el['x']},{el['y']})")
        if len(elements) > 60:
            lines.append(f"  ... and {len(elements) - 60} more")
        text = "\n".join(lines)
    else:
        text = f"Screenshot: {path} | no UIA elements (canvas/game/RDP — use x,y coordinates)"

    return [ToolOutputText(text=text), ToolOutputImage(image_url=_encode(img))]

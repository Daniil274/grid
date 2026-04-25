"""
Desktop automation helper for generic computer use.

Provides X11 window management, mouse control, keyboard input,
and screenshot capture. Used by all computer-use project tools.

Dependencies:
  - xdotool  (apt install xdotool)
  - ImageMagick  (apt install imagemagick)
"""

import glob
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
LOGS_DIR = Path(__file__).parent.parent / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('desktop_helper')
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    _fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    try:
        fh = logging.FileHandler(LOGS_DIR / 'desktop.log', encoding='utf-8')
        fh.setFormatter(_fmt)
        logger.addHandler(fh)
    except (PermissionError, OSError):
        pass  # not fatal — continue without file logging
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(_fmt)
    logger.addHandler(ch)

# ── Session state (active window) ─────────────────────────────────────────────
SESSION_FILE = Path(__file__).parent.parent / 'workspace' / '.session.json'

# Titles / class names of system helper windows to hide from find_window results
_SYSTEM_TITLES = {
    'mutter guard window', 'gnome-shell', 'gnome shell',
    'nautilus-desktop', 'desktop',
    'vboxclientwnddnd', 'main', 'ibus-x11', 'ibus-xim',
}
_SYSTEM_PREFIXES = ('gsd-', 'qt selection owner', 'qt client leader',
                    'qt net_wm', 'qt clipboard')


# ── X11 environment ───────────────────────────────────────────────────────────

def _x11_env() -> dict:
    """Return environment dict with DISPLAY and XAUTHORITY auto-detected."""
    env = os.environ.copy()

    if not env.get('DISPLAY'):
        sockets = sorted(glob.glob('/tmp/.X11-unix/X*'))
        if sockets:
            num = sockets[0].replace('/tmp/.X11-unix/X', '')
            env['DISPLAY'] = f':{num}'

    if not env.get('XAUTHORITY'):
        candidates = [
            '/run/user/1000/gdm/Xauthority',
            '/home/user/.Xauthority',
            '/root/.Xauthority',
        ] + sorted(glob.glob('/run/user/*/gdm/Xauthority'))
        for p in candidates:
            if os.path.exists(p):
                env['XAUTHORITY'] = p
                break

    return env


def _run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug('run: %s', ' '.join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, env=_x11_env())
    if r.returncode != 0 and check:
        raise RuntimeError(f'{cmd[0]} failed: {r.stderr.strip()}')
    return r


# ── Session helpers ───────────────────────────────────────────────────────────

def get_session() -> dict:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except Exception:
            pass
    return {}


def save_session(**kwargs) -> None:
    data = get_session()
    data.update(kwargs)
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2))


def get_active_wid(window_id: str | None = None) -> str | None:
    """Return explicit window_id, or the one saved in session."""
    if window_id:
        return str(window_id)
    return get_session().get('active_window_id')


def require_wid(window_id: str | None) -> str:
    """Return window_id or raise if none is active."""
    wid = get_active_wid(window_id)
    if not wid:
        raise RuntimeError(
            'No active window. Call find_window() first and note the window id.'
        )
    return wid


# ── Window discovery ──────────────────────────────────────────────────────────

def _is_system_window(title: str, width: int, height: int) -> bool:
    t = title.lower()
    if width < 80 or height < 50:
        return True
    if t in _SYSTEM_TITLES:
        return True
    for prefix in _SYSTEM_PREFIXES:
        if t.startswith(prefix):
            return True
    return False


def list_windows(query: str | None = None) -> list[dict]:
    """
    Return a list of visible application windows.

    Each dict: {'id', 'title', 'x', 'y', 'width', 'height'}

    query: optional substring to filter by title (case-insensitive).
    """
    # Use empty string to match all windows
    search_pat = query or ''
    r = _run(['xdotool', 'search', '--name', search_pat], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return []

    windows = []

    for wid in r.stdout.strip().split('\n'):
        wid = wid.strip()
        if not wid:
            continue

        tr = _run(['xdotool', 'getwindowname', wid], check=False)
        title = tr.stdout.strip()
        if not title:
            continue

        gr = _run(['xdotool', 'getwindowgeometry', wid], check=False)
        x = y = width = height = 0
        if gr.returncode == 0:
            pm = re.search(r'Position:\s*(\d+),(\d+)', gr.stdout)
            gm = re.search(r'Geometry:\s*(\d+)x(\d+)', gr.stdout)
            if pm:
                x, y = int(pm.group(1)), int(pm.group(2))
            if gm:
                width, height = int(gm.group(1)), int(gm.group(2))

        if _is_system_window(title, width, height):
            continue

        if query and query.lower() not in title.lower():
            continue

        windows.append({'id': wid, 'title': title,
                        'x': x, 'y': y, 'width': width, 'height': height})

    windows.sort(key=lambda w: w['width'] * w['height'], reverse=True)
    return windows


def get_window_geometry(wid: str) -> dict:
    r = _run(['xdotool', 'getwindowgeometry', wid])
    pm = re.search(r'Position:\s*(\d+),(\d+)', r.stdout)
    gm = re.search(r'Geometry:\s*(\d+)x(\d+)', r.stdout)
    if not pm or not gm:
        raise RuntimeError(f'Cannot parse geometry: {r.stdout}')
    return {'x': int(pm.group(1)), 'y': int(pm.group(2)),
            'width': int(gm.group(1)), 'height': int(gm.group(2))}


def activate_window(wid: str) -> None:
    _run(['xdotool', 'windowactivate', '--sync', wid], check=False)
    time.sleep(0.05)


# ── Screenshot ────────────────────────────────────────────────────────────────

def capture_window(wid: str, output_path: str) -> dict:
    """Capture window via ImageMagick import. Returns geometry dict."""
    activate_window(wid)
    time.sleep(0.15)
    geom = get_window_geometry(wid)
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    _run(['import', '-window', wid, output_path])
    if not os.path.exists(output_path):
        raise RuntimeError(f'import produced no file: {output_path}')
    logger.info('screenshot: %s (%dx%d)', output_path, geom['width'], geom['height'])
    return geom


def capture_screen(output_path: str) -> dict:
    """Capture the full screen."""
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    r = _run(['xdotool', 'getdisplaygeometry'], check=False)
    w = h = 0
    if r.returncode == 0:
        parts = r.stdout.strip().split()
        if len(parts) == 2:
            w, h = int(parts[0]), int(parts[1])
    _run(['import', '-window', 'root', output_path])
    return {'x': 0, 'y': 0, 'width': w, 'height': h}


# ── Mouse ─────────────────────────────────────────────────────────────────────

MOUSE_BUTTONS = {'left': '1', 'middle': '2', 'right': '3',
                 'scroll_up': '4', 'scroll_down': '5',
                 'scroll_left': '6', 'scroll_right': '7'}


def mouse_move(wid: str, x: int, y: int) -> None:
    """Move mouse to (x, y) relative to window top-left."""
    _run(['xdotool', 'mousemove', '--window', wid, str(x), str(y)])


def mouse_click(wid: str, x: int, y: int,
                button: str = 'left', clicks: int = 1) -> None:
    """Move to (x, y) in window and click."""
    btn = MOUSE_BUTTONS.get(button, '1')
    cmd = ['xdotool', 'windowactivate', '--sync', wid,
           'mousemove', '--window', wid, str(x), str(y)]
    for _ in range(clicks):
        cmd += ['click', btn]
    _run(cmd)


def mouse_drag(wid: str,
               x1: int, y1: int, x2: int, y2: int,
               button: str = 'left') -> None:
    """Drag from (x1, y1) to (x2, y2) within the window."""
    btn = MOUSE_BUTTONS.get(button, '1')
    _run(['xdotool', 'windowactivate', '--sync', wid])
    _run(['xdotool', 'mousemove', '--window', wid, str(x1), str(y1)])
    time.sleep(0.05)
    _run(['xdotool', 'mousedown', btn])
    time.sleep(0.1)
    _run(['xdotool', 'mousemove', '--window', wid, str(x2), str(y2)])
    time.sleep(0.05)
    _run(['xdotool', 'mouseup', btn])


def mouse_scroll(wid: str, x: int, y: int,
                 direction: str = 'down', amount: int = 3) -> None:
    """Scroll at (x, y) in window. direction: 'up'|'down'|'left'|'right'."""
    btn = MOUSE_BUTTONS.get(f'scroll_{direction}', '5')
    cmd = ['xdotool', 'windowactivate', '--sync', wid,
           'mousemove', '--window', wid, str(x), str(y)]
    for _ in range(amount):
        cmd += ['click', btn]
    _run(cmd)


# ── Keyboard ──────────────────────────────────────────────────────────────────

def key_send(wid: str, keys: str) -> None:
    """
    Send key combination to window.

    keys: xdotool key name, e.g. 'Return', 'Escape', 'ctrl+c', 'alt+F4',
          'ctrl+shift+t', 'super+d', 'F5'
    """
    _run(['xdotool', 'key', '--clearmodifiers', '--window', wid, keys])


def type_string(wid: str, text: str, delay_ms: int = 12) -> None:
    """
    Type a text string into the window (simulates keyboard typing).

    delay_ms: inter-character delay in ms (default 12 ms ≈ 80 wpm).
    """
    # Focus window first so text goes to the right place
    activate_window(wid)
    _run(['xdotool', 'type', '--window', wid, '--clearmodifiers',
          '--delay', str(delay_ms), '--', text])

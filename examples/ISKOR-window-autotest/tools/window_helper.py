"""
Window automation helper for GUI testing.
Replaces serial_helper.py — instead of communicating with a physical device,
this module finds the target application window and drives it via xdotool + mss.

Dependencies:
  - xdotool  (apt install xdotool)
  - mss      (pip install mss)
  - Pillow   (pip install Pillow)
"""

import os
import re
import subprocess
import time
import logging
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────────────
LOGS_DIR = Path(__file__).parent.parent / 'logs'
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE = LOGS_DIR / 'window.log'

logger = logging.getLogger('window_helper')
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    try:
        fh = logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except (PermissionError, OSError):
        pass  # not fatal — continue without file logging
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

logger.info('=' * 80)
logger.info('Window Helper Logger Started')
logger.info('=' * 80)

# ── Key mapping: logical name → X11 keysym (xdotool) ────────────────────────
# Same logical names as the serial ISKOR tools, mapped to X11 keysyms.
KEY_MAP: dict[str, str] = {
    '0': '0', '1': '1', '2': '2', '3': '3', '4': '4',
    '5': '5', '6': '6', '7': '7', '8': '8', '9': '9',
    'enter':  'Return',
    'esc':    'Escape',
    'up':     'Up',
    'down':   'Down',
    'left':   'Left',
    'right':  'Right',
    'q':      'q',
    'off':    'q',
}

# ── Defaults from environment (populated by core/config.py from config.yaml) ─
DEFAULT_WINDOW_TITLE    = 'ISKOR'
DEFAULT_KEY_DELAY_SEC   = 0.3
DEFAULT_SCREEN_DELAY_SEC = 0.2


def _x11_env() -> dict:
    """
    Build environment dict with DISPLAY and XAUTHORITY set.

    Priority:
      1. Already set in process environment
      2. Auto-detect: first active X11 socket + known Xauthority paths
    """
    env = os.environ.copy()

    # Resolve DISPLAY
    if not env.get('DISPLAY'):
        import glob as _glob
        sockets = sorted(_glob.glob('/tmp/.X11-unix/X*'))
        if sockets:
            num = sockets[0].replace('/tmp/.X11-unix/X', '')
            env['DISPLAY'] = f':{num}'
            logger.debug(f'auto-detected DISPLAY={env["DISPLAY"]}')

    # Resolve XAUTHORITY
    if not env.get('XAUTHORITY'):
        candidates = [
            '/run/user/1000/gdm/Xauthority',
            '/home/user/.Xauthority',
            '/root/.Xauthority',
        ]
        import glob as _glob
        candidates += sorted(_glob.glob('/run/user/*/gdm/Xauthority'))
        for path in candidates:
            if os.path.exists(path):
                env['XAUTHORITY'] = path
                logger.debug(f'auto-detected XAUTHORITY={path}')
                break

    return env


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command with X11 env, log it, and return the result."""
    logger.debug(f'run: {" ".join(cmd)}')
    result = subprocess.run(cmd, capture_output=True, text=True, env=_x11_env())
    if result.returncode != 0 and check:
        logger.error(f'cmd failed (rc={result.returncode}): {result.stderr.strip()}')
        raise RuntimeError(f'Command {cmd[0]} failed: {result.stderr.strip()}')
    return result


def _get_title_pattern() -> str:
    return os.environ.get('ISKOR_WINDOW_TITLE', DEFAULT_WINDOW_TITLE)


def _get_key_delay() -> float:
    return float(os.environ.get('ISKOR_KEY_DELAY', DEFAULT_KEY_DELAY_SEC))


def _get_screen_delay() -> float:
    return float(os.environ.get('ISKOR_SCREEN_DELAY', DEFAULT_SCREEN_DELAY_SEC))


# ── Core window functions ─────────────────────────────────────────────────────

def find_window_id(title_pattern: str | None = None) -> str:
    """
    Find the target application window using xdotool.

    Search strategy (most-specific first to avoid matching dev tools
    that show the app name in their title bar):
      1. WM_CLASS match  (xdotool --classname) — unambiguous for Qt apps
      2. Exact title match
      3. Title starts-with match
      4. First result of any substring match

    Returns the window ID string (e.g. '12345678').
    Raises RuntimeError if the window is not found.
    """
    pattern = title_pattern or _get_title_pattern()
    logger.info(f'find_window_id: searching for "{pattern}"')

    # 1. Try WM_CLASS / classname (most precise: only the Qt app itself matches)
    r = _run(['xdotool', 'search', '--classname', pattern], check=False)
    if r.returncode == 0 and r.stdout.strip():
        wid = r.stdout.strip().split('\n')[0].strip()
        logger.info(f'find_window_id: classname match → wid={wid}')
        return wid

    # 2. Fall back to title search with priority selection
    r = _run(['xdotool', 'search', '--sync', '--name', pattern], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(
            f'Window matching "{pattern}" not found. '
            'Is the application running? Set ISKOR_WINDOW_TITLE if needed.'
        )

    wids = [w.strip() for w in r.stdout.strip().split('\n') if w.strip()]
    logger.info(f'find_window_id: title search found {len(wids)} candidate(s)')

    if len(wids) == 1:
        logger.info(f'find_window_id: single match wid={wids[0]}')
        return wids[0]

    # Pick most specific title match
    exact = starts = None
    for wid in wids:
        t = _run(['xdotool', 'getwindowname', wid], check=False).stdout.strip()
        logger.debug(f'  wid={wid} title="{t}"')
        if t == pattern and exact is None:
            exact = wid
        if t.startswith(pattern) and starts is None:
            starts = wid

    chosen = exact or starts or wids[0]
    logger.info(f'find_window_id: chosen wid={chosen} (exact={exact}, starts={starts})')
    return chosen


def get_window_geometry(wid: str) -> dict:
    """
    Get window screen position and size.

    Returns dict: {'x': int, 'y': int, 'width': int, 'height': int}
    """
    result = _run(['xdotool', 'getwindowgeometry', wid])
    # Output example:
    #   Window 12345678
    #     Position: 100,200 (screen: 0)
    #     Geometry: 800x600
    pos_m = re.search(r'Position:\s*(\d+),(\d+)', result.stdout)
    geo_m = re.search(r'Geometry:\s*(\d+)x(\d+)', result.stdout)

    if not pos_m or not geo_m:
        raise RuntimeError(f'Cannot parse window geometry from xdotool output:\n{result.stdout}')

    geom = {
        'x':      int(pos_m.group(1)),
        'y':      int(pos_m.group(2)),
        'width':  int(geo_m.group(1)),
        'height': int(geo_m.group(2)),
    }
    logger.debug(f'get_window_geometry: wid={wid} → {geom}')
    return geom


def activate_window(wid: str) -> None:
    """Bring the window to focus (required before screenshot with mss)."""
    logger.debug(f'activate_window: wid={wid}')
    _run(['xdotool', 'windowactivate', '--sync', wid], check=False)
    time.sleep(0.05)  # let the WM process the raise


def send_key(wid: str, key_name: str) -> None:
    """
    Send a single key press to the window.

    key_name: logical key name (see KEY_MAP) or raw X11 keysym.
    Uses xdotool key --window to send without permanently changing focus.
    """
    xsym = KEY_MAP.get(key_name.lower(), key_name)
    logger.info(f'send_key: wid={wid} key="{key_name}" → xsym="{xsym}"')
    _run(['xdotool', 'key', '--clearmodifiers', '--window', wid, xsym])
    delay = _get_key_delay()
    if delay > 0:
        time.sleep(delay)


def hold_key(wid: str, key_name: str, duration_ms: int) -> None:
    """
    Simulate holding a key for duration_ms milliseconds.

    Sends keydown, waits, then keyup — xdotool generates auto-repeat events
    on most X11 servers.
    """
    xsym = KEY_MAP.get(key_name.lower(), key_name)
    logger.info(f'hold_key: wid={wid} key="{key_name}" → xsym="{xsym}" duration={duration_ms}ms')
    _run(['xdotool', 'keydown', '--clearmodifiers', '--window', wid, xsym])
    time.sleep(duration_ms / 1000.0)
    _run(['xdotool', 'keyup', '--window', wid, xsym])
    delay = _get_key_delay()
    if delay > 0:
        time.sleep(delay)


def capture_window(wid: str, output_path: str) -> dict:
    """
    Capture a screenshot of the window using ImageMagick's `import -window`.

    Raises the window to front before capture so it is fully composited.
    Returns the geometry dict {'x', 'y', 'width', 'height'}.
    """
    activate_window(wid)

    # Extra delay so the WM fully renders the window
    screen_delay = _get_screen_delay()
    if screen_delay > 0:
        time.sleep(screen_delay)

    geom = get_window_geometry(wid)

    # Ensure output directory exists
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    logger.info(f'capture_window: wid={wid} geom={geom} → {output_path}')

    _run(['import', '-window', wid, output_path])

    if not os.path.exists(output_path):
        raise RuntimeError(f'import -window produced no output file: {output_path}')

    logger.info(f'capture_window: saved {output_path} ({geom["width"]}x{geom["height"]}px)')
    return geom


def is_window_open(title_pattern: str | None = None) -> bool:
    """Check if the target window is currently open."""
    try:
        find_window_id(title_pattern)
        return True
    except RuntimeError:
        return False

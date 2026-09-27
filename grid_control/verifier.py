"""Verifier: judges one running candidate from the outside, like a user would.

The controller sends this file's source to a separate container built from the
operator's verifier image; it never sees the candidate's files or secrets. It
must therefore use only the standard library and import nothing from Grid or
grid_control. Run as ``PLAN = {...}`` followed by this source.

Checks are HTTP requests with an expected status and JSON value. Scenarios are
chat turns over the web chat WebSocket (``/api/chat/ws/<id>``): the verifier
sends one message and reads the event stream until ``done``, collecting where
the message was routed (``routed``), which tools ran (``step`` of kind
``tool``) and the answer (``final_output`` or the ``token`` stream).
"""

import base64
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.request

SCENARIO_PREFIX = "scenario:"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(base, check):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(base + check["path"], timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.status != check["expected_status"]:
            return False
        pointer = check["json_pointer"]
        if pointer is None:
            return True
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return False
        value = json.loads(body)
        if pointer:
            for key in pointer[1:].split("/"):
                key = key.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
        return type(value) is type(check["expected"]) and value == check["expected"]


class WebSocket:
    """Minimal RFC 6455 client: text frames, fragmentation, ping and close."""

    def __init__(self, host, port, path, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self._fill()
            if len(self.buffer) > 65536:
                raise ConnectionError("Oversized handshake response")
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if not head.startswith(b"HTTP/1.1 101"):
            raise ConnectionError("WebSocket upgrade refused: " + head.split(b"\r\n")[0].decode("latin-1"))

    def settimeout(self, seconds):
        self.sock.settimeout(max(seconds, 0.1))

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("Connection closed by the candidate")
        self.buffer += chunk

    def _take(self, size):
        while len(self.buffer) < size:
            self._fill()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        elif len(payload) < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", len(payload))
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", len(payload))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def receive(self):
        """Next text message, or None when the candidate closed the connection."""
        message = b""
        while True:
            first, second = self._take(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._take(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._take(8))[0]
            if len(message) + length > MAX_MESSAGE_BYTES:
                raise ConnectionError("Oversized WebSocket message")
            mask = self._take(4) if second & 0x80 else b""
            payload = self._take(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return message.decode("utf-8", errors="replace")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


def _tool_matches(title, name):
    # Older builds titled a sub-agent's call "agent › tool"; an MCP tool is "server.tool".
    title = title.rsplit(" › ", 1)[-1]
    return title == name or title.endswith("." + name)


def run_scenario(host, port, scenario):
    """Send one message and judge the turn. Returns ``(passed, explanation)``."""
    deadline = time.monotonic() + scenario["timeout_seconds"]
    # A routing-only scenario is decided by the ``routed`` event: stop the turn
    # there instead of paying for the whole agent run.
    routing_only = not (
        scenario.get("tools_called") or scenario.get("tools_not_called")
        or scenario.get("output_matches")
    )
    routed, tools, tokens, final, error = None, {}, [], None, None
    try:
        socket_ = WebSocket(host, port, "/api/chat/ws/eval-" + os.urandom(8).hex(), 10)
    except (OSError, ConnectionError) as failure:
        return False, f"cannot open chat: {failure}"
    try:
        socket_.send(json.dumps({"message": scenario["message"]}))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, f"no answer within {scenario['timeout_seconds']}s"
            socket_.settimeout(remaining)
            text = socket_.receive()
            if text is None:
                break
            event = json.loads(text)
            kind = event.get("type")
            if kind == "routed":
                routed = (event.get("system"), event.get("agent"))
                if routing_only:
                    socket_.send(json.dumps({"action": "stop"}))
                    break
            elif kind == "step":
                step = event.get("step") or {}
                # A sub-agent's run is an ``agent`` step titled by the agent;
                # ``tool`` names the call either way (absent in older builds).
                if step.get("kind") in {"tool", "agent"}:
                    tools[step.get("id")] = str(step.get("tool") or step.get("title") or "")
            elif kind == "token":
                tokens.append(str(event.get("content") or ""))
            elif kind == "final_output":
                final = str(event.get("content") or "")
            elif kind == "error":
                error = str(event.get("content") or "error")
            elif kind == "done":
                break
    except TimeoutError:
        return False, f"no answer within {scenario['timeout_seconds']}s"
    except (OSError, ConnectionError, ValueError) as failure:
        return False, f"chat failed: {failure}"
    finally:
        socket_.close()

    output = final if final is not None else "".join(tokens)
    called = sorted(set(tools.values()))
    problems = []
    if error:
        problems.append(f"error: {error[:300]}")
    if scenario.get("system") and (routed or (None, None))[0] != scenario["system"]:
        problems.append(f"routed to system {routed[0] if routed else None}, expected {scenario['system']}")
    if scenario.get("agent") and (routed or (None, None))[1] != scenario["agent"]:
        problems.append(f"routed to agent {routed[1] if routed else None}, expected {scenario['agent']}")
    for name in scenario.get("tools_called") or ():
        if not any(_tool_matches(title, name) for title in called):
            problems.append(f"tool {name} was not called")
    for name in scenario.get("tools_not_called") or ():
        if any(_tool_matches(title, name) for title in called):
            problems.append(f"tool {name} was called")
    pattern = scenario.get("output_matches")
    if pattern and not re.search(pattern, output):
        problems.append(f"answer does not match /{pattern}/")
    summary = (
        f"routed={routed[0] + '/' + routed[1] if routed else 'none'}; "
        f"tools={','.join(called) or 'none'}; answer={output[:200]!r}"
    )
    return not problems, ("; ".join(problems) + " | " if problems else "ok | ") + summary


def run(plan):
    host, port = plan.get("host", "candidate"), plan.get("port", 8000)
    base = f"http://{host}:{port}"
    started = time.monotonic()
    deadline = started + plan["timeout_seconds"] - 5
    ready = False
    while time.monotonic() < deadline:
        try:
            ready = request(base, {"path": "/", "expected_status": 200, "json_pointer": None})
        except Exception:
            pass
        if ready:
            break
        time.sleep(0.25)
    results, details = {}, {}
    for check in plan["checks"]:
        try:
            results[check["name"]] = ready and request(base, check)
        except Exception:
            results[check["name"]] = False
    for scenario in plan.get("scenarios", ()):
        key = SCENARIO_PREFIX + scenario["name"]
        if not ready:
            results[key], details[key] = False, "candidate never became ready"
            continue
        results[key], details[key] = run_scenario(host, port, scenario)
    return {"checks": results, "details": details, "elapsed_seconds": time.monotonic() - started}


if __name__ == "__main__":
    print(json.dumps(run(PLAN)))  # noqa: F821 - PLAN is prepended by the controller

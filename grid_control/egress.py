"""Egress proxy: the candidate's only way out of its internal network.

The controller runs this file's source in its own container from the verifier
image, attached to the default bridge and to the trial's internal network as
``egress``. The candidate gets ``HTTPS_PROXY=http://egress:3128``. Only
``CONNECT host:443`` to hosts in the policy's ``egress_hosts`` is tunnelled;
plain HTTP and every other destination are refused. Standard library only; run
as ``PLAN = {...}`` followed by this source.
"""

import asyncio
import json
import sys

PORT = 3128
MAX_HEADER_BYTES = 8192


def allowed(host, hosts):
    host = host.lower().rstrip(".")
    return any(
        host == entry or (entry.startswith("*.") and host.endswith(entry[1:]))
        for entry in hosts
    )


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _refuse(writer, status):
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    writer.close()


async def handle(reader, writer, hosts):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
    if len(parts) != 3 or parts[0] != "CONNECT":
        await _refuse(writer, "405 Method Not Allowed")
        return
    host, _, port = parts[1].rpartition(":")
    if port != "443" or not allowed(host, hosts):
        print(json.dumps({"refused": parts[1]}), file=sys.stderr, flush=True)
        await _refuse(writer, "403 Forbidden")
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443), 15
        )
    except (OSError, asyncio.TimeoutError):
        await _refuse(writer, "502 Bad Gateway")
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


async def serve(hosts, port=PORT):
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, hosts), "0.0.0.0", port, limit=MAX_HEADER_BYTES
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve(PLAN["hosts"]))  # noqa: F821 - PLAN is prepended by the controller

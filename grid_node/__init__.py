"""grid_node: a device's side of Grid - a browser machine, a PC, a phone.

It runs on the device, serves the device's tools to the Grid server as an MCP
server over one WebSocket (grid_node.tunnel), and streams the screen to the
person watching it. It depends on nothing of Grid itself, so it installs on
a user's PC on its own.
"""

__version__ = "0.1.0"

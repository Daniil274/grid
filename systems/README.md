# Created systems

Systems made on the web chat's systems page (`/systems`), one directory each:
`config.yaml` (the Grid config), `system.yaml` (name, router description,
status: draft, published or archived) and the `skills/` their agents use.

The catalogs name this directory in `routing.systems_dir`; the router offers
the published systems next to the ones the catalogs list. See
`core/system_store.py` and `web_chat/system_hub.py`.

"""Standalone Context Inspector for Grid session buckets."""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    from context_inspector.server import create_app as _create_app

    return _create_app(*args, **kwargs)

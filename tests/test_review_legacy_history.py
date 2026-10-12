"""Legacy migration must fail closed rather than destroy unproven tool data."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from core.factory.sessions import SessionUpkeep


@pytest.mark.asyncio
async def test_legacy_hook_does_not_read_or_mutate_persistent_history():
    items = [
        {"type": "function_call_output", "call_id": "file-read", "output": "real file\nContext ID: ctx-12345678"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "quotation\nContext ID: ctx-12345678"},
                                          {"type": "output_text", "text": "more text"}]},
    ]
    session = SimpleNamespace(get_items=AsyncMock(return_value=items),
                              clear_session=AsyncMock(side_effect=AssertionError("destructive clear")),
                              add_items=AsyncMock(side_effect=AssertionError("non-atomic write")))
    await SessionUpkeep()._strip_legacy_context_footers(session)
    session.get_items.assert_not_awaited()
    session.clear_session.assert_not_awaited()
    session.add_items.assert_not_awaited()
    assert items[0]["output"].endswith("Context ID: ctx-12345678")

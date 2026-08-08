from __future__ import annotations

import pytest

from plugins.life_engine.storage.multi_writer_protocol import (
    MultiWriterProtocolConfig,
    MultiWriterProtocolError,
    validate_multi_writer_readiness,
)


def test_multi_writer_readiness_requires_retired_global_singleton() -> None:
    with pytest.raises(MultiWriterProtocolError, match="singleton writer"):
        validate_multi_writer_readiness(
            config=MultiWriterProtocolConfig(),
            generation_schema_version=3,
            observed_protocol_version=1,
            singleton_retired=False,
        )


def test_multi_writer_readiness_is_content_free_and_explicit() -> None:
    result = validate_multi_writer_readiness(
        config=MultiWriterProtocolConfig(),
        generation_schema_version=3,
        observed_protocol_version=1,
        singleton_retired=True,
    )
    assert result == {
        "status": "ready",
        "protocol_version": 1,
        "schema_version": 3,
        "singleton_retired": True,
    }
    assert "token" not in result


def test_multi_writer_rejects_incompatible_protocol_or_schema() -> None:
    with pytest.raises(MultiWriterProtocolError, match="protocol version"):
        validate_multi_writer_readiness(
            config=MultiWriterProtocolConfig(),
            generation_schema_version=3,
            observed_protocol_version=2,
            singleton_retired=True,
        )
    with pytest.raises(MultiWriterProtocolError, match="schema"):
        validate_multi_writer_readiness(
            config=MultiWriterProtocolConfig(),
            generation_schema_version=2,
            observed_protocol_version=1,
            singleton_retired=True,
        )

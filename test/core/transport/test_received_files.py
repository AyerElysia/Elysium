from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.core.transport.received_files import persist_received_file


async def test_received_file_is_content_addressed_and_idempotent(
    tmp_path: Path,
) -> None:
    body = "爱莉的附件".encode()

    first = await persist_received_file(
        body,
        filename="../珍贵 记录.txt",
        platform="Feishu",
        root=tmp_path,
    )
    second = await persist_received_file(
        body,
        filename="../珍贵 记录.txt",
        platform="Feishu",
        root=tmp_path,
    )

    assert first == second
    assert first.path.read_bytes() == body
    assert first.path.is_relative_to(tmp_path)
    assert first.filename == "珍贵_记录.txt"
    assert first.sha256 == hashlib.sha256(body).hexdigest()
    assert first.storage_key.startswith("feishu/")
    assert ".." not in first.storage_key


async def test_received_file_rejects_oversized_body(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exceeds configured byte limit"):
        await persist_received_file(
            b"too-large",
            filename="payload.bin",
            platform="qq",
            root=tmp_path,
            max_bytes=2,
        )


async def test_received_file_does_not_expose_body_in_reference(tmp_path: Path) -> None:
    secret = b"private attachment body"

    reference = await persist_received_file(
        secret,
        filename="private.bin",
        platform="qq",
        root=tmp_path,
    )

    assert not hasattr(reference, "data")
    assert secret.decode() not in repr(reference)


async def test_received_file_repairs_corrupt_content_addressed_target(
    tmp_path: Path,
) -> None:
    expected = b"verified-body"
    reference = await persist_received_file(
        expected,
        filename="proof.bin",
        platform="qq",
        root=tmp_path,
    )
    reference.path.write_bytes(b"corrupt-body")

    repaired = await persist_received_file(
        expected,
        filename="proof.bin",
        platform="qq",
        root=tmp_path,
    )

    assert repaired == reference
    assert repaired.path.read_bytes() == expected


async def test_received_file_rejects_symlinked_platform_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "received"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "qq").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        await persist_received_file(
            b"must-stay-inside",
            filename="escape.bin",
            platform="qq",
            root=root,
        )

    assert list(outside.iterdir()) == []

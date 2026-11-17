"""Length-prefixed local pipe messages shared by the parser and its owner."""

import asyncio
import struct
from typing import BinaryIO


def frame(payload: bytes) -> bytes:
    return struct.pack("!Q", len(payload)) + payload


def read_exactly(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("Parser pipe ended within a message")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(stream: BinaryIO) -> bytes | None:
    first = stream.read(1)
    if not first:
        return None
    size = struct.unpack("!Q", first + read_exactly(stream, 7))[0]
    return read_exactly(stream, size)


async def read_response(stream: asyncio.StreamReader) -> bytes:
    size = struct.unpack("!Q", await stream.readexactly(8))[0]
    return await stream.readexactly(size)


def write_frame(stream: BinaryIO, payload: bytes) -> None:
    stream.write(frame(payload))
    stream.flush()

"""Bounded SSE observation before the transport releases a complete frame."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import NoReturn

import httpx

from loom.session_protection import (
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
    observe_response,
    refuse_response,
)

_MAX_FRAME_BYTES = 4 * 1024 * 1024


class _Frames:
    def __init__(self, roots: tuple[SessionProtection, ...]) -> None:
        self.roots = roots
        self.frame = bytearray()
        self.line = bytearray()
        self.pending_cr = False
        self.started = False
        self.complete = False

    def refuse(self) -> NoReturn:
        with observation_scope(self.roots):
            refuse_response(None)

    def _observe(self, frame: bytes) -> None:
        try:
            text = frame.decode("utf8")
            if not self.started:
                text = text.removeprefix("\ufeff")
                self.started = True
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            data = []
            for line in lines:
                field, _, value = line.partition(":")
                if field == "data":
                    data.append(value.removeprefix(" "))
            if not data:
                return
            text = "\n".join(data)
            if text == "[DONE]":
                return
            value = json.loads(text)
            if not isinstance(value, dict):
                self.refuse()
        except (ValueError, UnicodeError, RecursionError):
            self.refuse()
        with observation_scope(self.roots):
            observe_response(value, None)

    def _newline(self) -> bytes | None:
        if self.line:
            self.line.clear()
            return None
        frame = bytes(self.frame)
        self.frame.clear()
        self._observe(frame)
        return frame

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        for byte in chunk:
            if self.pending_cr:
                self.pending_cr = False
                if byte == 10:
                    self.frame.append(byte)
                    if len(self.frame) > _MAX_FRAME_BYTES:
                        self.refuse()
                    frame = self._newline()
                    if frame is not None:
                        yield frame
                    continue
                frame = self._newline()
                if frame is not None:
                    yield frame
            self.frame.append(byte)
            if len(self.frame) > _MAX_FRAME_BYTES:
                self.refuse()
            if byte == 13:
                self.pending_cr = True
            elif byte == 10:
                frame = self._newline()
                if frame is not None:
                    yield frame
            else:
                self.line.append(byte)

    def finish(self) -> Iterator[bytes]:
        if self.pending_cr:
            self.pending_cr = False
            frame = self._newline()
            if frame is not None:
                yield frame
        if self.frame:
            self.refuse()
        self.complete = True


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, frames: _Frames) -> None:
        self.stream = stream
        self.frames = frames

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.stream:
                for frame in self.frames.feed(chunk):
                    yield frame
            for frame in self.frames.finish():
                yield frame
        except SessionProtectionUnavailable:
            raise
        except BaseException:
            # An interrupted observation protects future callbacks and keeps its primary failure.
            with contextlib.suppress(SessionProtectionUnavailable):
                self.frames.refuse()
            raise

    async def aclose(self) -> None:
        try:
            if not self.frames.complete:
                with contextlib.suppress(SessionProtectionUnavailable):
                    self.frames.refuse()
        finally:
            await self.stream.aclose()


def protect_event_stream(response: httpx.Response, roots: tuple[SessionProtection, ...]) -> None:
    """Keep original frame bytes, with no observer context held across caller yields."""
    if not roots or response.extensions.get("_loom_observed_events"):
        return
    frames = _Frames(roots)
    # Encoded bytes cannot be safely inspected before HTTPX's decoder releases them.
    if response.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
        frames.refuse()
    if "text/event-stream" not in response.headers.get("content-type", "").lower():
        frames.refuse()
    if response.is_stream_consumed:
        # HTTPX bypasses response.stream when a custom transport already cached content.
        try:
            content = response.content
        except httpx.ResponseNotRead:
            frames.refuse()
        for _ in frames.feed(content):
            pass
        for _ in frames.finish():
            pass
    elif isinstance(response.stream, httpx.AsyncByteStream):
        response.stream = _ObservedStream(response.stream, frames)
    else:
        frames.refuse()
    response.extensions["_loom_observed_events"] = True

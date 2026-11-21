"""Private fixed-target TLS bridge; run only after separate runtime admission."""

from __future__ import annotations

import argparse
import asyncio
import signal
import ssl
import sys


class Bridge:
    def __init__(self, upstream_port: int) -> None:
        self.port = upstream_port
        self.tasks: set[asyncio.Task] = set()
        self.stopping = asyncio.Event()
        self.failure: BaseException | None = None

    def fail(self, error: BaseException) -> None:
        if self.failure is None:
            self.failure = error
        self.stopping.set()

    async def copy(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await asyncio.wait_for(reader.read(65536), 30):
            writer.write(data)
            await asyncio.wait_for(writer.drain(), 30)

    async def finish(self, pumps: list[asyncio.Task], endpoints: tuple) -> BaseException | None:
        first = None
        for pump in pumps:
            if not pump.done():
                pump.cancel()
        try:
            outcomes = await asyncio.gather(*pumps, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError
                ):
                    first = outcome if first is None else first
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            first = error if first is None else first
        for endpoint in endpoints:
            if endpoint is None:
                continue
            try:
                endpoint.close()
                await asyncio.wait_for(endpoint.wait_closed(), 5)
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                first = error if first is None else first
        return first

    async def drain(self, pumps: list[asyncio.Task], endpoints: tuple, first=None):
        closing = asyncio.create_task(self.finish(pumps, endpoints))
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                first = error if first is None else first
        try:
            outcome = closing.result()
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            outcome = error
        return outcome if first is None else first

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        if len(self.tasks) >= 8 or self.stopping.is_set():
            error = await self.drain([], (writer,))
            if error is not None:
                self.fail(error)
            return
        self.tasks.add(task)
        upstream = None
        pumps: list[asyncio.Task] = []
        first = None
        try:
            async with asyncio.timeout(120):
                source, upstream = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self.port, limit=65536), 5
                )
                pumps = [
                    asyncio.create_task(self.copy(reader, upstream)),
                    asyncio.create_task(self.copy(source, writer)),
                ]
                done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                for pump in done:
                    pump.result()
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            first = error
        finally:
            first = await self.drain(pumps, (upstream, writer), first)
            self.tasks.discard(task)
        if first is not None and not (
            self.stopping.is_set() and isinstance(first, asyncio.CancelledError)
        ):
            self.fail(first)

    async def serve(self, port: int, certificate: str, key: str) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])
        context.load_cert_chain(certificate, key)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, self.stopping.set)
        server = await asyncio.start_server(
            self.connection,
            "127.0.0.1",
            port,
            ssl=context,
            limit=65536,
            ssl_handshake_timeout=5,
            ssl_shutdown_timeout=5,
        )
        try:
            await self.stopping.wait()
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            self.fail(error)
        finally:
            closing = asyncio.create_task(self.shutdown(server))
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    self.fail(error)
            error = closing.result()
            if error is not None:
                self.fail(error)
        if self.failure is not None:
            raise self.failure

    async def shutdown(self, server) -> BaseException | None:
        first = None
        try:
            server.close()
            await server.wait_closed()
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            first = error
        for task in tuple(self.tasks):
            task.cancel()
        result = await self.finish(list(self.tasks), ())
        return result if first is None else first


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--upstream-port", required=True, type=int)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    if any(not 0 < port < 65536 for port in (args.listen_port, args.upstream_port)):
        return 2
    if args.listen_port == args.upstream_port:
        return 2
    try:
        asyncio.run(Bridge(args.upstream_port).serve(args.listen_port, args.certificate, args.key))
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        # Error text may contain request bytes or private file names.
        print(type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

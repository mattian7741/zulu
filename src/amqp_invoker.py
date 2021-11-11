"""Summary."""
import functools
import aio_pika
import aiomisc
import asyncio
import json

from retry import retry
from typing import Awaitable, Callable, Iterable, Tuple
from urllib.parse import urlparse

from src.function_invocable import FunctionInvocable
from src.invoker import Invoker
from src.types import TYPE_PAYLOAD

# content_type: application/json
# {"x":5,"y":7}

# TODO(ahuman-bean): requires fine-tuning
MAX_THREADS = 2


def set_param(host: str, param_key: str, param_val: str) -> str:
    """Overwrite a param in a host string w a new value."""
    uri, new_param = urlparse(host), f'{param_key}={param_val}'
    params = [p for p in uri.query.split('&') if param_key not in p] + [new_param]
    return uri._replace(query='&'.join(params)).geturl()


class AmqpInvoker(Invoker):
    """Summary."""

    def __init__(self, invocable: FunctionInvocable) -> None:
        super().__init__(invocable)

        host = self._invocable.config.host
        heartbeat = self._invocable.config.heartbeat

        self.url = set_param(host, 'heartbeat', str(heartbeat)) if heartbeat else host
        self.exchange_name = self._invocable.config.exchange
        self.queue_name = self._invocable.config.func

    def start(self) -> int:
        """
        Starts a new event loop that maintains a persistent AMQP connection.
        The underlying execution context is an `aiomisc.ThreadPoolExecutor` of size `MAX_THREADS`.

        Returns:
            exit_code
        """
        loop = aiomisc.new_event_loop(pool_size=MAX_THREADS)
        connection = loop.run_until_complete(self.run(loop))

        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(connection.close())

        return 0

    @retry(aio_pika.exceptions.AMQPConnectionError, jitter=(1, 3), backoff=2)
    async def run(self, loop: asyncio.AbstractEventLoop) -> aio_pika.RobustConnection:
        connection = await aio_pika.connect_robust(url=self.url, loop=loop)

        async def get_channel() -> aio_pika.Channel:
            return await connection.channel()

        # Separate channels for consuming/publishing
        channel_pool = aio_pika.pool.Pool(get_channel, max_size=2, loop=loop)

        async with connection, channel_pool:
            async for data_in in self.consume(channel_pool):
                try:
                    async for data_out in self.do_work(data_in):
                        await self.publish(message=aio_pika.Message(body=json.dumps(data_out).encode()), routing_key=str(self._invocable.config.pubtopic))

                except Exception as err:  # pylint: disable=broad-except
                    data_in['error'] = str(err)
                    # TODO(ahuman-bean): cleaner error messages
                    await self.publish(message=aio_pika.Message(body=json.dumps(data_in).encode()), routing_key=f'{self.queue_name}_error')

        return connection

    async def consume(self, channel_pool: aio_pika.pool.Pool[aio_pika.RobustChannel]) -> None:
        async with channel_pool.acquire() as channel:
            exchange = await self.get_exchange(channel)
            queue = await channel.declare_queue(name=self.queue_name)
            queue_error = await channel.declare_queue(name=f'{self.queue_name}_error')

            await queue.bind(exchange=exchange, routing_key=str(self._invocable.config.subtopic))
            await queue_error.bind(exchange=exchange, routing_key=f'{self.queue_name}_error')

            async for message in queue:
                async with message.process():
                    yield dict(json.loads(message.body.decode('utf-8')))

    async def publish(self, channel_pool: aio_pika.pool.Pool[aio_pika.RobustChannel], message: aio_pika.Message, routing_key: str) -> None:
        async with channel_pool.acquire() as channel:
            exchange = await channel.declare_exchange(
                name=self.exchange_name,
                type=aio_pika.ExchangeType.TOPIC,
                passive=False,
                durable=True,
                auto_delete=False,
                internal=False,
                arguments=None
            )
            exchange.publish(message, routing_key)

    @aiomisc.threaded_iterable_separate
    def do_work(self, data_in: TYPE_PAYLOAD) -> Iterable[TYPE_PAYLOAD]:
        """
        Performs the potentially CPU-intensive work of `self._invocable.invoke` in a separate thread
        outside the constraints of the underlying execution context.

        Parameters:
            data_in: Raw event data

        Yields:
            payload: Lazily-evaluable wrapper around return values from `self._invocable.invoke` plus metadata
        """
        for data_out in self._invocable.invoke(data_in["data"]):
            yield {
                "data": data_out,
                "key": str(self._invocable.config.pubtopic),
                "log": data_in.get("log", [])
            }

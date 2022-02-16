import asyncio
from typing import AsyncGenerator, Dict, Tuple

import aio_pika
import aiomisc
from quart import Quart, request

from ergo.amqp_invoker import set_param
from ergo.config import Config
from ergo.message import Message, decode, decodes, encodes
from ergo.topic import PubTopic, SubTopic
from ergo.util import instance_id, uniqueid

MAX_THREADS = 8


class QuartHttpGateway:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._port = 80
        self._loop = aiomisc.new_event_loop(pool_size=MAX_THREADS)
        self._exchange, self._queue = self._loop.run_until_complete(self._setup(config))
        self._publishers: Dict[str, asyncio.Condition] = {}
        self._inbox: Dict[str, Message] = {}

    def start(self) -> int:
        app = Quart(__name__)

        @app.route("/health", methods=["GET"])
        def health():
            return {"status": "up"}

        @app.route("/<path:path>", methods=["GET", "POST"])
        async def handler(path: str):
            topic = path.replace("/", ".")
            # TODO what do we do if multiple results are being yielded by a generator?
            #  We're not injecting the handler, so we can't inspect for that.
            message_out = await self._rpc(topic, request.args).__anext__()
            return encodes(message_out)

        consumer_loop = self._loop.create_task(self._run_consumer())
        app.run(host="0.0.0.0", port=self._port, loop=self._loop)
        consumer_loop.cancel()
        return 0

    async def _rpc(self, topic: str, data: dict) -> AsyncGenerator[Message, None]:
        message = decode(**data)
        message.key = topic

        correlation_id = uniqueid()
        self._publishers[correlation_id] = asyncio.Condition()
        try:
            message.scope.reply_to = f"{instance_id()}.{correlation_id}"
            amqp_message = aio_pika.Message(
                body=encodes(message).encode("utf-8"), correlation_id=correlation_id
            )
            routing_key = str(PubTopic(message.key))
            await self._exchange.publish(amqp_message, routing_key)
            async with self._publishers[correlation_id]:
                await self._publishers[correlation_id].wait()
            yield self._inbox.pop(correlation_id)
        finally:
            del self._publishers[correlation_id]

    async def _run_consumer(self):
        async for amqp_message in self._queue:
            try:
                amqp_message.ack()
                ergo_message = decodes(amqp_message.body.decode("utf-8"))
                self._inbox[amqp_message.correlation_id] = ergo_message
            finally:
                async with self._publishers[amqp_message.correlation_id]:
                    self._publishers[amqp_message.correlation_id].notify()

    async def _setup(self, config: Config) -> Tuple[aio_pika.Exchange, aio_pika.Queue]:
        host = self._config.host
        heartbeat = self._config.heartbeat
        broker_url = set_param(host, "heartbeat", str(heartbeat)) if heartbeat else host

        connection: aio_pika.Connection = await aio_pika.connect_robust(broker_url)
        channel: aio_pika.Channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        exchange: aio_pika.Exchange = await channel.declare_exchange(
            name=config.exchange,
            type=aio_pika.ExchangeType.TOPIC,
            passive=False,
            durable=True,
            auto_delete=False,
            internal=False,
            arguments=None,
        )
        queue: aio_pika.Queue = await channel.declare_queue(
            name=f"rpc/{instance_id()}", exclusive=True
        )
        await queue.bind(exchange=exchange, routing_key=str(SubTopic(instance_id())))
        return exchange, queue
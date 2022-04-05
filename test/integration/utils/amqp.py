from __future__ import annotations

import contextlib
import json
import pathlib
import time
from functools import lru_cache
from test.integration.utils import FunctionComponent
from typing import Callable, Dict, List, Optional

import amqp.exceptions
import kombu
import kombu.pools
import kombu.simple
from amqp import Channel

from ergo.topic import SubTopic

try:
    from collections.abc import Generator
except ImportError:
    from typing import Generator

from ergo.message import Message, decodes
from ergo.topic import PubTopic

AMQP_HOST = "amqp://guest:guest@localhost:5672/%2F"
CONNECTION = kombu.Connection(AMQP_HOST)
EXCHANGE = "amq.topic"  # use a pre-declared exchange that we kind bind to while the ergo runtime is booting
SHORT_TIMEOUT = 0.01
LONG_TIMEOUT = 5


class AMQPComponent(FunctionComponent):
    protocol = "amqp"
    instances: List[AMQPComponent] = []

    def __init__(
        self,
        func: Callable,
        subtopic: Optional[str] = None,
        pubtopic: Optional[str] = None,
        **manifest
    ):
        super().__init__(func, **manifest)
        self.queue_name = f"{self.handler_path.replace('/', ':')[1:]}:{self.handler_name}"
        self.error_queue_name = f"{self.queue_name}:error"
        handler_module = pathlib.Path(self.handler_path).with_suffix("").name
        self.subtopic = subtopic or f"{handler_module}_{self.handler_name}_sub"
        self.pubtopic = pubtopic or f"{handler_module}_{self.handler_name}_pub"
        self._component_queue = kombu.Queue(name=self.queue_name, exchange=EXCHANGE, routing_key=str(SubTopic(self.subtopic)))

    @property
    def namespace(self):
        ns = {
            "protocol": "amqp",
            "host": AMQP_HOST,
            "exchange": EXCHANGE,
            "subtopic": self.subtopic,
        }
        if self.pubtopic:
            ns["pubtopic"] = self.pubtopic
        return ns

    def rpc(self, payload: Dict, timeout=LONG_TIMEOUT):
        self.send(payload)
        return self.consume(timeout=timeout)

    def send(self, payload: Dict):
        publish(payload, self.subtopic)

    def consume(self, timeout=LONG_TIMEOUT) -> Message:
        return self._subscription.consume(timeout=timeout)

    def __enter__(self):
        super().__enter__()

        self.instances.append(self)
        self._subscription = Queue(self.pubtopic, name=f"test:subscription:{self.pubtopic}")
        self._subscription.__enter__()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        super().__exit__(exc_type, exc_val, exc_tb)

        self.instances.pop()
        with CONNECTION.channel() as channel:
            channel.queue_delete(self.queue_name)
            channel.queue_delete(self.error_queue_name)
        self._subscription.__exit__()


def publish(payload: dict, routing_key: str):
    with CONNECTION.channel() as channel:
        _await_components(channel)
        with kombu.Producer(channel, serializer="raw") as producer:
            producer.publish(json.dumps(payload), exchange=EXCHANGE, routing_key=str(PubTopic(routing_key)))


@lru_cache()
def _await_components(channel: Channel):
    queue_names = {instance.queue_name for instance in AMQPComponent.instances}
    for queue_name in queue_names:
        _await_queue(channel, queue_name)


def _await_queue(channel: Channel, queue_name):
    while True:
        try:
            channel.queue_declare(queue_name, passive=True)
            break
        except amqp.exceptions.NotFound:
            time.sleep(SHORT_TIMEOUT)

    channel.queue_purge(queue_name)


class ComponentFailure(Exception):
    pass


class Queue:
    def __init__(self, routing_key, name: Optional[str] = None, **kombu_opts):
        self.name = name or f"test:{routing_key}"
        self.routing_key = routing_key
        self._kombu_opts = {"auto_delete": True, "durable": False, **kombu_opts}

    def consume(self, block=True, timeout=LONG_TIMEOUT) -> Message:
        amqp_message = self._queue.get(block=block, timeout=timeout)
        return decodes(amqp_message.body)

    def __enter__(self):
        self._channel: Channel = CONNECTION.channel()
        exchange = kombu.Exchange(EXCHANGE, type="topic", channel=self._channel)
        self._spec = kombu.Queue(self.name, exchange=exchange, routing_key=str(SubTopic(self.routing_key)), no_ack=True, **self._kombu_opts)
        self._queue = kombu.simple.SimpleQueue(self._channel, self._spec, serializer="raw")
        return self

    def __exit__(self, *exc_info):
        self._channel.queue_delete(self.name)
        self._channel.__exit__()


class ComponentQueue(Queue):
    def __init__(self, routing_key):
        super().__init__(routing_key, auto_delete=False, durable=True)


class propagate_errors(contextlib.ContextDecorator):
    def __init__(self):
        self._queue = kombu.Queue("test:propagate_errors_queue", exchange=EXCHANGE, routing_key="#", auto_delete=True, no_ack=True)

    def __enter__(self):
        self._channel: Channel = CONNECTION.channel()
        self._consumer = kombu.Consumer(self._channel, queues=[self._queue], callbacks=[self._handle_message])
        self._consumer.consume()
        return self

    def __exit__(self, *exc_info):
        self._consumer.close()
        self._channel.close()

    @staticmethod
    def _handle_message(body: str, _):
        ergo_msg = decodes(body)
        if ergo_msg.error:
            raise ComponentFailure(ergo_msg.traceback)

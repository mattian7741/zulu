from __future__ import annotations

import json
import pathlib
from test.integration.utils import Component, retries
from typing import Callable, Dict, Optional, List
from functools import wraps
import inspect
import pytest

import pika
import pika.exceptions
from pika.adapters.blocking_connection import BlockingChannel

from ergo.topic import SubTopic

try:
    from collections.abc import Generator
except ImportError:
    from typing import Generator

from collections import defaultdict

from ergo.topic import PubTopic

AMQP_HOST = "amqp://guest:guest@localhost:5672/%2F"
EXCHANGE = "amq.topic"  # use a pre-declared exchange that we kind bind to while the ergo runtime is booting
SHORT_TIMEOUT = 0.01
LONG_TIMEOUT = 5
_LIVE_INSTANCES: Dict = defaultdict(int)


class AMQPComponent(Component):
    protocol = "amqp"
    host = AMQP_HOST
    instances: List[AMQPComponent] = []

    def __init__(self, func: Callable, subtopic: Optional[str] = None, pubtopic: Optional[str] = None):
        super().__init__(func)
        self.queue_name = f"{self.handler_path}:{self.handler_name}"
        self.error_queue_name = f"{self.queue_name}_error"
        handler_module = pathlib.Path(self.handler_path).with_suffix("").name
        self.subtopic = subtopic or f"{handler_module}_{self.handler_name}_sub"
        self.pubtopic = pubtopic or f"{handler_module}_{self.handler_name}_pub"

    @property
    def namespace(self):
        ns = super().namespace
        ns["exchange"] = EXCHANGE
        ns["subtopic"] = self.subtopic
        if self.pubtopic:
            ns["pubtopic"] = self.pubtopic
        return ns

    def rpc(self, inactivity_timeout=LONG_TIMEOUT, **message):
        self.send(**message)
        return self.consume(inactivity_timeout=inactivity_timeout)

    def send(self, **message):
        publish(str(PubTopic(self.subtopic)), message, channel=self.channel, exchange=EXCHANGE)

    def consume(self, inactivity_timeout=LONG_TIMEOUT):
        attempt = 0
        while True:
            value = consume(self._subscription_queue, channel=self.channel, inactivity_timeout=SHORT_TIMEOUT)
            if value:
                return value
            self.propagate_error(inactivity_timeout=SHORT_TIMEOUT)
            attempt += 1
            if inactivity_timeout and attempt >= inactivity_timeout * 20:
                return None

    def propagate_error(self, inactivity_timeout=None):
        body = consume(self.error_queue_name, inactivity_timeout=inactivity_timeout)
        if body:
            raise ComponentFailure(body["traceback"])

    def setup_component(self):
        for retry in retries(200, SHORT_TIMEOUT, pika.exceptions.ChannelClosedByBroker):
            with retry():
                channel = new_channel()
                channel.queue_declare(self.queue_name, passive=True)
        purge_queue(self.error_queue_name)
        purge_queue(self.queue_name)

    def setup_instance(self):
        self.channel = new_channel()
        self._subscription_queue = self.channel.queue_declare(
            queue="",
            exclusive=True,
        ).method.queue
        self.channel.queue_bind(self._subscription_queue, EXCHANGE, routing_key=str(SubTopic(self.pubtopic)))
        return self

    def teardown_component(self):
        try:
            channel = new_channel()
            channel.queue_delete(self.queue_name)
            channel.queue_delete(self.error_queue_name)
        except pika.exceptions.ChannelClosedByBroker:
            pass

    def __call__(self, test):
        params = inspect.signature(test).parameters

        if "component" in params:
            @wraps(test)
            @pytest.mark.parametrize("component", [self])
            def test_with_component(*args, component=None, **kwargs):
                with self:
                    return test(*args, component=component, **kwargs)
            return test_with_component
        if "components" in params:
            @wraps(test)
            @pytest.mark.parametrize("components", [AMQPComponent.instances])
            def test_with_component(*args, components=None, **kwargs):
                with self:
                    return test(*args, components=components, **kwargs)
            return test_with_component
        return test

    def __enter__(self):
        self.instances.append(self)
        super().__enter__()
        if not _LIVE_INSTANCES[self.func]:
            self.setup_component()
        _LIVE_INSTANCES[self.func] += 1
        self.setup_instance()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.instances.pop()
        _LIVE_INSTANCES[self.func] -= 1
        if not _LIVE_INSTANCES[self.func]:
            self.teardown_component()
        super().__exit__(exc_type, exc_val, exc_tb)


amqp_component = AMQPComponent


def publish(routing_key, message, channel, exchange):
    channel.confirm_delivery()
    for retry in retries(200, SHORT_TIMEOUT, pika.exceptions.UnroutableError):
        with retry():
            body = json.dumps(message).encode()
            channel.basic_publish(exchange=exchange, routing_key=routing_key, body=body, mandatory=True)


def consume(queue_name, inactivity_timeout=None, channel=None):
    channel = channel or new_channel()
    method, _, body = next(channel.consume(queue_name, inactivity_timeout=inactivity_timeout))
    if body:
        channel.basic_ack(method.delivery_tag)
        return json.loads(body)
    return None


def purge_queue(queue_name: str):
    channel = new_channel()
    try:
        channel.queue_purge(queue_name)
    except pika.exceptions.ChannelClosedByBroker as exc:
        if "no queue" not in str(exc):
            raise


def new_channel() -> BlockingChannel:
    connection = get_connection()
    return connection.channel()


def get_connection() -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.URLParameters(AMQP_HOST))


class ComponentFailure(BaseException):
    pass

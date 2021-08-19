"""Summary."""
import json
from typing import Tuple
from urllib.parse import urlparse

import pika

from src.invoker import Invoker
from src.types import TYPE_PAYLOAD

# content_type: application/json
# {"x":5,"y":7}


def overwrite_heartbeat(host: str, heartbeat: int) -> str:
    """Overwrite the heartbeat in a rabbitmq url."""
    uri, new_param = urlparse(host), f'heartbeat={heartbeat}'
    found, params = False, uri.query.split('&')
    for i, old_param in enumerate(params):
        if 'heartbeat' in old_param:
            found = True
            params[i] = new_param
    if not found:
        params.append(new_param)
    return uri._replace(query='&'.join(params)).geturl()


class AmqpInvoker(Invoker):
    """Summary."""

    def connect(self) -> Tuple[pika.adapters.blocking_connection.BlockingChannel, str, str]:
        """Connect to a rabbit broker."""
        heartbeat = self._invocable.config.heartbeat
        host = overwrite_heartbeat(self._invocable.config.host, heartbeat) if heartbeat else self._invocable.config.host
        parameters = pika.URLParameters(host)
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        queue_name = self._invocable.config.func
        queue_name_error = f'{queue_name}_error'
        exchange_name = self._invocable.config.exchange
        channel.queue_declare(queue=queue_name)
        channel.queue_declare(queue=queue_name_error)
        assert isinstance(exchange_name, str)
        channel.exchange_declare(exchange_name, exchange_type='topic', passive=False, durable=True, auto_delete=False, internal=False, arguments=None)

        channel.queue_bind(exchange=exchange_name, queue=queue_name, routing_key=str(self._invocable.config.subtopic))
        return channel, queue_name, queue_name_error

    def start(self) -> int:
        """Summary."""
        channel, queue_name, queue_name_error = self.connect()

        def handler(channel, method, properties, body) -> None:  # type: ignore
            """Summary.

            Args:
                channel (TYPE): Description
                method (TYPE): Description
                properties (TYPE): Description
                body (TYPE): Description
            """
            data_in: TYPE_PAYLOAD = dict(json.loads(body.decode('utf-8')))
            data_in['key'] = str(self._invocable.config.subtopic)
            try:
                for data_out in self._invocable.invoke(data_in):
                    data_out['key'] = str(self._invocable.config.pubtopic)
                    channel.basic_publish(exchange=self._invocable.config.exchange, routing_key=str(self._invocable.config.pubtopic), body=json.dumps(data_out))
            except Exception as err:  # pylint: disable=broad-except
                data_in['error'] = str(err)
                channel.basic_publish(exchange='', routing_key=queue_name_error, body=json.dumps(data_in))

        channel.basic_consume(queue=queue_name, auto_ack=True, on_message_callback=handler)

        channel.start_consuming()

        return 0

from test.integration.gateway.utils import gateway_component
from test.integration.utils.amqp import amqp_component
from test.integration.utils.http import http_session
from functools import partial
from multiprocessing.pool import ThreadPool
import time
import requests


"""
test_double
"""


def product(x, y):
    time.sleep(1)
    return float(x) * float(y)


def double(sesh, x: int):
    resp = sesh.get("http://0.0.0.0/product", params={"x": x, "y": 2})
    return {x: resp.json()["data"]}


@gateway_component()
@amqp_component(product, subtopic="product")
def test_double(components):
    pool = ThreadPool(20)
    actual = pool.map(partial(double, http_session()), range(20))
    expected = [
        {0: 0.0},
        {1: 2.0},
        {2: 4.0},
        {3: 6.0},
        {4: 8.0},
        {5: 10.0},
        {6: 12.0},
        {7: 14.0},
        {8: 16.0},
        {9: 18.0},
        {10: 20.0},
        {11: 22.0},
        {12: 24.0},
        {13: 26.0},
        {14: 28.0},
        {15: 30.0},
        {16: 32.0},
        {17: 34.0},
        {18: 36.0},
        {19: 38.0},
    ]
    assert actual == expected


"""
test_yield_twice

Assert that a gateway request fulfilled by a generator component returns the first item yielded.
"""


def yield_twice():
    yield 1
    yield 2


@gateway_component()
@amqp_component(yield_twice, subtopic="yield_twice")
def test_yield_twice(components):
    response = http_session().get("http://0.0.0.0/yield_twice")
    assert response.json()["data"] == 1
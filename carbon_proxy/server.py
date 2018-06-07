import asyncio
import logging
import os
import sys
import uuid
from collections import deque
from functools import partial
from http import HTTPStatus

import forkme
import msgpack
from aiohttp.web import (
    Application,
    Request,
    Response,
    run_app,
    HTTPBadRequest,
    GracefulExit,
    HTTPForbidden,
    HTTPUnauthorized,
)
from aiohttp.web_urldispatcher import UrlDispatcher  # NOQA
from configargparse import ArgumentParser
from setproctitle import setproctitle

from aiomisc.utils import chunk_list, bind_socket, new_event_loop
from aiomisc.log import basic_config, LogFormat


log = logging.getLogger()
parser = ArgumentParser(auto_env_var_prefix="APP_")

parser.add_argument('-f', '--forks', type=int, default=4)
parser.add_argument('-D', '--debug', action='store_true')


parser.add_argument('--log-level', default='info',
                    choices=('debug', 'info', 'warning', 'error', 'fatal'))

parser.add_argument('--log-format', choices=LogFormat.choices(),
                    default='color')

parser.add_argument('--pool-size', default=4, type=int)


group = parser.add_argument_group('HTTP settings')
group.add_argument('--http-address', type=str, default='0.0.0.0')
group.add_argument('--http-port', type=int, default=8081)
group.add_argument('-S', '--http-secret', type=str, required=True)

group = parser.add_argument_group('Carbon settings')
group.add_argument('-H', '--carbon-host', type=str, required=True,
                   help="TCP protocol host")

group.add_argument('-P', '--carbon-port', type=int, default=2003,
                   help="TCP protocol port")


QUEUE = deque()

# Should be rewrite
SECRET = str(uuid.uuid4())


async def send_data(data, host, port, loop):
    if not data:
        return

    while True:
        try:
            reader, writer = await asyncio.open_connection(
                host, port, loop=loop
            )

            for name, value, timestamp in data:
                d = "%s %s %s\n" % (name, value, timestamp)
                writer.write(d.encode())

            await writer.drain()
            writer.close()
            reader.feed_eof()
        except:
            log.exception("Failed to send data")
            await asyncio.sleep(1, loop=loop)
        else:
            log.debug("Sent %d items to %s:%d", len(data), host, port)
            break


async def sender(host, port, loop):
    log.info("Starting sender endpoint %s:%d", host, port)
    try:
        while True:
            if not QUEUE:
                await asyncio.sleep(1, loop=loop)
                continue

            metrics = []

            while QUEUE:
                metrics.append(QUEUE.popleft())

            # switch context
            await asyncio.sleep(0, loop=loop)

            for chunk in chunk_list(metrics, 1000):
                await send_data(chunk, host, port, loop)

            await asyncio.sleep(1, loop=loop)
    finally:
        metrics = [m for m in QUEUE]
        QUEUE.clear()
        await send_data(metrics, host, port, loop)


async def ping(*_):
    return Response(content_type='text/plain', status=HTTPStatus.OK)


async def statistic_receiver(request: Request):

    auth = request.headers.get('Authorization')

    if not auth:
        raise HTTPUnauthorized()

    secret = auth.replace("Bearer ", '')

    if secret != SECRET:
        raise HTTPForbidden()

    payload = msgpack.unpackb(await request.read(), encoding='utf-8')

    if not isinstance(payload, list):
        raise HTTPBadRequest()

    for metric in payload:
        try:
            name, ts_value = metric
            ts, value = ts_value
            ts = float(ts)
            assert isinstance(value, (int, float, type(None)))
        except:
            log.exception("Invalid data in %r", metric)
            raise HTTPBadRequest()

        QUEUE.append((name, value, ts))

    return Response(content_type='text/plain', status=HTTPStatus.ACCEPTED)


async def setup_routes(app: Application):
    router = app.router     # type: UrlDispatcher
    router.add_get('/ping', ping)
    router.add_post('/stat', statistic_receiver)


async def setup_sender(app: Application, *, arguments):
    task = app.loop.create_task(
        sender(
            arguments.carbon_host,
            arguments.carbon_port,
            app.loop,
        )
    )

    async def cleanup(*_):
        task.cancel()
        await asyncio.wait([task], loop=app.loop)

    app.on_cleanup.append(cleanup)


def make_app(arguments, **kwargs) -> Application:
    app = Application(debug=arguments.debug, **kwargs)
    return app


def main():
    global SECRET

    arguments = parser.parse_args()
    basic_config(level=arguments.log_level,
                 log_format=arguments.log_format,
                 buffered=False)

    setproctitle(os.path.basename("[Master] %s" % sys.argv[0]))

    sock = bind_socket(address=arguments.http_address, port=arguments.http_port)

    forkme.fork(arguments.forks)

    setproctitle(os.path.basename("[Worker] %s" % sys.argv[0]))

    SECRET = arguments.http_secret

    loop = new_event_loop(arguments.pool_size)
    loop.set_debug(arguments.debug)

    app = make_app(arguments)
    app._debug = arguments.debug

    app.on_startup.append(setup_routes)
    app.on_startup.append(partial(setup_sender, arguments=arguments))

    log.info(
        "Starting application http://%s:%d",
        arguments.http_address, arguments.http_port
    )

    basic_config(level=arguments.log_level,
                 log_format=arguments.log_format,
                 buffered=True, loop=loop)

    try:
        run_app(sock=sock, loop=loop, app=app, print=log.debug)
    except GracefulExit:
        log.info("Exiting")


if __name__ == '__main__':
    main()

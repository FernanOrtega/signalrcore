import websocket
import threading
import requests

import uuid
import time
import ssl
from signalrcore.messages.message_type import MessageType
from signalrcore.messages.stream_invocation_message\
    import StreamInvocationMessage
from .reconnection import ConnectionStateChecker
from signalrcore.messages.ping_message import PingMessage
from .connection_state import ConnectionState
from .errors import UnAuthorizedHubError, HubError
from signalrcore.helpers import Helpers


class StreamHandler(object):
    def __init__(self, event, invocation_id):
        self.event = event
        self.invocation_id = invocation_id
        self.next_callback = None
        self.complete_callback = None
        self.error_callback = None

    def subscribe(self, subscribe_callbacks):
        if subscribe_callbacks is None:
            raise ValueError(" subscribe object must be {0}".format({
                "next": None,
                "complete": None,
                "error": None
                }))
        self.next_callback = subscribe_callbacks["next"]
        self.complete_callback = subscribe_callbacks["complete"]
        self.error_callback = subscribe_callbacks["error"]


class BaseHubConnection(object):
    def __init__(
            self,
            url,
            protocol,
            headers={},
            keep_alive_interval=15,
            reconnection_handler=None,
            verify_ssl=False,
            skip_negotiation=False):
        self.skip_negotiation = skip_negotiation
        self.logger = Helpers.get_logger()
        self.url = url
        self.protocol = protocol
        self.headers = headers
        self.handshake_received = False
        self.token = None # auth
        self.state = ConnectionState.disconnected
        self.connection_alive = False
        self.handlers = []
        self.stream_handlers = []
        self._thread = None
        self._ws = None
        self.verify_ssl = verify_ssl
        self.connection_checker = ConnectionStateChecker(
            lambda: self.send(PingMessage()),
            keep_alive_interval
        )
        self.reconnection_handler = reconnection_handler
        self.on_connect = None
        self.on_disconnect = None

    def negotiate(self):
        negotiate_url = Helpers.get_negotiate_url(self.url)
        self.logger.debug("Negotiate url:{0}".format(negotiate_url))

        response = requests.post(negotiate_url, headers=self.headers, verify=self.verify_ssl)
        self.logger.debug("Response status code{0}".format(response.status_code))

        if response.status_code != 200:
            raise HubError(response.status_code) if response.status_code != 401 else UnAuthorizedHubError()
        data = response.json()
        if "connectionId" in data.keys():
            self.url = Helpers.encode_connection_id(self.url, data["connectionId"])

        # Azure
        if 'url' in data.keys() and 'accessToken' in data.keys():
            Helpers.get_logger().debug("Azure url, reformat headers, token and url {0}".format(data))
            self.url = data["url"] if data["url"].startswith("ws") else Helpers.http_to_websocket(data["url"])
            self.token = data["accessToken"]
            self.headers = {"Authorization": "Bearer " + self.token}

    def enable_trace(self, traceable):
        if len(self.logger.handlers) > 0:
            websocket.enableTrace(traceable, self.logger.handlers[0])

    def start(self):
        if not self.skip_negotiation:
            self.negotiate()
        self.logger.debug("Connection started")
        if self.state == ConnectionState.connected:
            self.logger.warning("Already connected unable to start")
            return
        self.state = ConnectionState.connecting
        self.logger.debug("start url:" + self.url)
        self._ws = websocket.WebSocketApp(
            self.url,
            header=self.headers,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open,
            )
        self._thread = threading.Thread(
            target=lambda: self._ws.run_forever(
                sslopt={"cert_reqs": ssl.CERT_NONE} if not self.verify_ssl else {}
            ))
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self.logger.debug("Connection stop")
        if self.state == ConnectionState.connected:
            self._ws.close()
            self.connection_checker.stop()
            self.state == ConnectionState.disconnected

    def register_handler(self, event, callback):
        self.logger.debug("Handler registered started {0}".format(event))
        self.handlers.append((event, callback))

    def evaluate_handshake(self, message):
        self.logger.debug("Evaluating handshake {0}".format(message))
        msg = self.protocol.decode_handshake(message)
        if msg.error is None or msg.error == "":
            self.handshake_received = True
            self.state = ConnectionState.connected
            if self.reconnection_handler is not None:
                self.reconnection_handler.reconnecting = False
                if not self.connection_checker.running:
                    self.connection_checker.start()
        else:
            self.logger.error(msg.error)
            raise ValueError("Handshake error {0}".format(msg.error))

    def on_open(self):
        self.logger.debug("-- web socket open --")
        msg = self.protocol.handshake_message()
        self.send(msg)

    def on_close(self):
        self.logger.debug("-- web socket close --")
        if self.on_disconnect is not None and callable(self.on_disconnect):
            self.on_disconnect()

    def on_error(self, error):
        self.logger.debug("-- web socket error --")
        self.logger.error("{0} {1}".format(error, type(error)))

    def on_message(self, raw_message):
        self.logger.debug("Message received{0}".format(raw_message))
        self.connection_checker.last_message = time.time()
        if not self.handshake_received:
            self.evaluate_handshake(raw_message)
            if self.on_connect is not None and callable(self.on_connect):
                self.state = ConnectionState.connected
                self.on_connect()
            return

        messages = self.protocol.parse_messages(raw_message)
        for message in messages:
            if message.type == MessageType.invocation_binding_failure:
                self.logger.error(message)
                continue
            if message.type == MessageType.ping:
                continue

            if message.type == MessageType.invocation:
                fired_handlers = list(
                    filter(
                        lambda h: h[0] == message.target,
                        self.handlers))
                if len(fired_handlers) == 0:
                    self.logger.warning(
                        "event '{0}' hasn't fire any handler".format(
                            message.target))
                for _, handler in fired_handlers:
                    handler(message.arguments)

            if message.type == MessageType.close:
                self.logger.info("Close message received from server")
                self.stop()
                return

            if message.type == MessageType.completion:
                fired_handlers = list(
                    filter(
                        lambda h: h.invocation_id == message.invocation_id,
                        self.stream_handlers))
                for handler in fired_handlers:
                    handler.complete_callback(message)

                # unregister handler
                self.stream_handlers = list(
                    filter(
                        lambda h: h.invocation_id != message.invocation_id,
                        self.stream_handlers))

            if message.type == MessageType.stream_item:
                fired_handlers = list(
                    filter(
                        lambda h: h.invocation_id == message.invocation_id,
                        self.stream_handlers))
                if len(fired_handlers) == 0:
                    self.logger.warning(
                        "id '{0}' hasn't fire any stream handler".format(
                            message.invocation_id))
                for handler in fired_handlers:
                    handler.next_callback(message.item)

            if message.type == MessageType.stream_invocation:
                pass

            if message.type == MessageType.cancel_invocation:
                fired_handlers = list(
                    filter(
                        lambda h: h.invocation_id == message.invocation_id,
                        self.stream_handlers))
                if len(fired_handlers) == 0:
                    self.logger.warning(
                        "id '{0}' hasn't fire any stream handler".format(
                            message.invocation_id))

                for handler in fired_handlers:
                    handler.error_callback(message)

                # unregister handler
                self.stream_handlers = list(
                    filter(
                        lambda h: h.invocation_id != message.invocation_id,
                        self.stream_handlers))

    def send(self, message):
        self.logger.debug("Sending message {0}".format(message))
        try:
            self._ws.send(self.protocol.encode(message))
            self.connection_checker.last_message = time.time()
            if self.reconnection_handler is not None:
                self.reconnection_handler.reset()
        except (
                websocket._exceptions.WebSocketConnectionClosedException,
                OSError) as ex:
            self.handshake_received = False
            self.logger.error("Connection closed {0}".format(ex))
            self.state = ConnectionState.disconnected
            if self.reconnection_handler is None:
                if self.on_disconnect is not None and callable(self.on_disconnect):
                    self.on_disconnect()
                raise ValueError(str(ex))
            # Connection closed
            self.handle_reconnect()
        except Exception as ex:
            raise ex

    def handle_reconnect(self):
        self.reconnection_handler.reconnecting = True
        try:
            self.stop()
            self.start()
        except Exception as ex:
            self.logger.error(ex)
            sleep_time = self.reconnection_handler.next()
            threading.Thread(
                target=self.deferred_reconnect,
                args=(sleep_time,)
            )

    def deferred_reconnect(self, sleep_time):
        time.sleep(sleep_time)
        try:
            if not self.connection_alive:
                self._send_ping()
        except Exception as ex:
            self.reconnection_handler.reconnecting = False
            self.connection_alive = False

    def stream(self, event, event_params):
        invocation_id = str(uuid.uuid4())
        stream_obj = StreamHandler(event, invocation_id)
        self.stream_handlers.append(stream_obj)
        self.send(
            StreamInvocationMessage(
                {},
                invocation_id,
                event,
                event_params))
        return stream_obj

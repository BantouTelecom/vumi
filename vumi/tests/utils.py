# -*- test-case-name: vumi.tests.test_testutils -*-

import re
import json
from datetime import datetime, timedelta
from collections import namedtuple
import warnings
from functools import wraps

import pytz
from twisted.trial.unittest import TestCase, SkipTest
from twisted.internet import defer, reactor
from twisted.internet.error import ConnectionRefusedError
from twisted.web.resource import Resource
from twisted.internet.defer import DeferredQueue, inlineCallbacks, returnValue
from twisted.python import log

from vumi.utils import (vumi_resource_path, import_module, flatten_generator,
                        LogFilterSite)
from vumi.service import get_spec, Worker, WorkerCreator
from vumi.message import TransportUserMessage, TransportEvent
from vumi.tests.fake_amqp import FakeAMQPBroker, FakeAMQClient


def import_skip(exc, *expected):
    msg = exc.args[0]
    module = msg.split()[-1]
    if expected and (module not in expected):
        raise
    raise SkipTest("Failed to import '%s'." % (module,))


class UTCNearNow(object):
    def __init__(self, offset=10):
        self.now = datetime.utcnow()
        self.utcnow = self.now.replace(tzinfo=pytz.UTC)
        self.offset = timedelta(offset)

    def __eq__(self, other):
        now = self.now
        if other.tzinfo:
            now = self.utcnow
        return (now - self.offset) < other < (now + self.offset)


class RegexMatcher(object):
    def __init__(self, regex):
        self.regex = re.compile(regex)

    def __eq__(self, other):
        return self.regex.match(other)


class Mocking(object):

    class HistoryItem(object):
        def __init__(self, args, kwargs):
            self.args = args
            self.kwargs = kwargs

        def __repr__(self):
            return '<%r object at %r [args: %r, kw: %r]>' % (
                self.__class__.__name__, id(self), self.args, self.kwargs)

    def __init__(self, function):
        """Mock a function"""
        self.function = function
        self.called = 0
        self.history = []
        self.return_value = None

    def __enter__(self):
        """Overwrite whatever module the function is part of"""
        self.mod = import_module(self.function.__module__)
        setattr(self.mod, self.function.__name__, self)
        return self

    def __exit__(self, *exc_info):
        """Reset to whatever the function was originally when done"""
        setattr(self.mod, self.function.__name__, self.function)

    def __call__(self, *args, **kwargs):
        """Return the return value when called, store the args & kwargs
        for testing later, called is a counter and evaluates to True
        if ever called."""
        self.args = args
        self.kwargs = kwargs
        self.called += 1
        self.history.append(self.HistoryItem(args, kwargs))
        return self.return_value

    def to_return(self, *args):
        """Specify the return value"""
        self.return_value = args if len(args) > 1 else list(args).pop()
        return self


def mocking(fn):
    return Mocking(fn)


def fake_amq_message(dictionary, delivery_tag='delivery_tag'):
    Content = namedtuple('Content', ['body'])
    Message = namedtuple('Message', ['content', 'delivery_tag'])
    return Message(delivery_tag=delivery_tag,
                   content=Content(body=json.dumps(dictionary)))


def get_fake_amq_client(broker=None):
    spec = get_spec(vumi_resource_path("amqp-spec-0-8.xml"))
    return FakeAMQClient(spec, {}, broker)


def get_stubbed_worker(worker_class, config=None, broker=None):
    worker = worker_class({}, config)
    worker._amqp_client = get_fake_amq_client(broker)
    return worker


class StubbedWorkerCreator(WorkerCreator):
    broker = None

    def _connect(self, worker, timeout, bindAddress):
        amq_client = get_fake_amq_client(self.broker)
        self.broker = amq_client.broker  # So we use the same broker for all.
        reactor.callLater(0, worker._amqp_connected, amq_client)


def get_stubbed_channel(broker=None, id=0):
    spec = get_spec(vumi_resource_path("amqp-spec-0-8.xml"))
    amq_client = FakeAMQClient(spec, {}, broker)
    return amq_client.channel(id)


class TestResourceWorker(Worker):
    port = 9999
    _resources = ()

    def set_resources(self, resources):
        self._resources = resources

    def startWorker(self):
        resources = [(cls(*args), path) for path, cls, args in self._resources]
        self.resources = self.start_web_resources(resources, self.port)
        return defer.succeed(None)

    def stopWorker(self):
        if self.resources:
            self.resources.stopListening()


def FakeRedis():
    warnings.warn("Use of FakeRedis is deprecated. "
                  "Use persist.tests.fake_redis instead.",
                  category=DeprecationWarning)
    from vumi.persist import fake_redis
    return fake_redis.FakeRedis()


class LogCatcher(object):
    """Context manager for gathering logs in tests.

    :param str system:
        Only log events whose 'system' value contains the given
        regular expression pattern will be gathered. Default: None
        (i.e. keep all log events).

    :param str message:
        Only log events whose message contains the given regular
        expression pattern will be gathered. The message is
        constructed by joining the elements in the 'message' value
        with a space (the same way Twisted does). Default: None
        (i.e. keep all log events).
    """

    def __init__(self, system=None, message=None):
        self.logs = []
        self.system = re.compile(system) if system is not None else None
        self.message = re.compile(message) if message is not None else None

    @property
    def errors(self):
        return [ev for ev in self.logs if ev["isError"]]

    def messages(self):
        return [" ".join(msg['message']) for msg in self.logs
                if not msg["isError"]]

    def _keep_log(self, event_dict):
        if self.system is not None:
            if not self.system.search(event_dict.get('system', '-')):
                return False
        if self.message is not None:
            log_message = " ".join(event_dict.get('message', []))
            if not self.message.search(log_message):
                return False
        return True

    def _gather_logs(self, event_dict):
        if self._keep_log(event_dict):
            self.logs.append(event_dict)

    def __enter__(self):
        log.theLogPublisher.addObserver(self._gather_logs)
        return self

    def __exit__(self, *exc_info):
        log.theLogPublisher.removeObserver(self._gather_logs)


class MockResource(Resource):
    isLeaf = True

    def __init__(self, handler):
        Resource.__init__(self)
        self.handler = handler

    def render_GET(self, request):
        return self.handler(request)

    def render_POST(self, request):
        return self.handler(request)


class MockHttpServer(object):

    def __init__(self, handler=None):
        self.queue = DeferredQueue()
        self._handler = handler or self.handle_request
        self._webserver = None
        self.addr = None
        self.url = None

    def handle_request(self, request):
        self.queue.put(request)

    @inlineCallbacks
    def start(self):
        root = MockResource(self._handler)
        site_factory = LogFilterSite(root)
        self._webserver = yield reactor.listenTCP(0, site_factory)
        self.addr = self._webserver.getHost()
        self.url = "http://%s:%s/" % (self.addr.host, self.addr.port)

    @inlineCallbacks
    def stop(self):
        yield self._webserver.loseConnection()


def maybe_async(sync_attr):
    """Decorate a method that may be sync or async.

    This redecorates with the either @inlineCallbacks or @flatten_generator,
    depending on the `sync_attr`.
    """
    if callable(sync_attr):
        # If we don't get a sync attribute name, default to 'is_sync'.
        return maybe_async('is_sync')(sync_attr)

    def redecorate(func):
        @wraps(func)
        def wrapper(self, *args, **kw):
            if getattr(self, sync_attr):
                return flatten_generator(func)(self, *args, **kw)
            return inlineCallbacks(func)(self, *args, **kw)
        return wrapper

    return redecorate


class RiakDisabledForTest(object):
    """Placeholder object for a disabled riak config.

    This class exists to throw a meaningful error when trying to use Riak in
    a test that disallows it. We can't do this from inside the Riak setup
    infrastructure, because that would be very invasive for something that
    only really matters for tests.
    """
    def __getattr__(self, name):
        raise RuntimeError(
            "Use of Riak has been disabled for this test. Please set "
            "'use_riak = True' on the test class to enable it.")


class PersistenceMixin(object):
    sync_persistence = False
    use_riak = False

    sync_or_async = staticmethod(maybe_async('sync_persistence'))

    def _persist_setUp(self):
        self._persist_riak_managers = []
        self._persist_redis_managers = []
        self._persist_config = {
            'redis_manager': {
                'FAKE_REDIS': 'yes',
                'key_prefix': type(self).__module__,
                },
            'riak_manager': {
                'bucket_prefix': type(self).__module__,
                },
            }
        if not self.use_riak:
            self._persist_config['riak_manager'] = RiakDisabledForTest()

    def mk_config(self, config):
        return dict(self._persist_config, **config)

    @maybe_async('sync_persistence')
    def _persist_tearDown(self):
        # We don't always have all the managers we want to clean up.
        # Therefore, we create one of each with the base configuration.
        # We may not have all the bits necessary here, so we ignore certain
        # exceptions.
        try:
            yield self.get_redis_manager()
        except (SkipTest, ConnectionRefusedError):
            pass
        try:
            if self.use_riak:
                yield self.get_riak_manager()
        except SkipTest:
            pass

        for manager in self._persist_riak_managers:
            try:
                yield self._persist_purge_riak(manager)
            except ConnectionRefusedError:
                pass
        for manager in self._persist_redis_managers:
            yield self._persist_purge_redis(manager)

    def _persist_purge_riak(self, manager):
        "This is a separate method to allow easy overriding."
        return manager.purge_all()

    @maybe_async('sync_persistence')
    def _persist_purge_redis(self, manager):
        "This is a separate method to allow easy overriding."
        try:
            yield manager._purge_all()
        except RuntimeError, e:
            # Ignore managers that are already closed.
            if e.args[0] != 'Not connected':
                raise
        yield manager.close_manager()

    def get_riak_manager(self, config=None):
        if config is None:
            config = self._persist_config['riak_manager'].copy()

        if self.sync_persistence:
            return self._get_sync_riak_manager(config)
        return self._get_async_riak_manager(config)

    def _get_async_riak_manager(self, config):
        try:
            from vumi.persist.txriak_manager import TxRiakManager
        except ImportError, e:
            import_skip(e, 'riakasaurus', 'riakasaurus.riak')

        riak_manager = TxRiakManager.from_config(config)
        self._persist_riak_managers.append(riak_manager)
        return riak_manager

    def _get_sync_riak_manager(self, config):
        try:
            from vumi.persist.riak_manager import RiakManager
        except ImportError, e:
            import_skip(e, 'riak')

        riak_manager = RiakManager.from_config(config)
        self._persist_riak_managers.append(riak_manager)
        return riak_manager

    def get_redis_manager(self, config=None):
        if config is None:
            config = self._persist_config['redis_manager'].copy()

        if self.sync_persistence:
            return self._get_sync_redis_manager(config)
        return self._get_async_redis_manager(config)

    def _get_async_redis_manager(self, config):
        from vumi.persist.txredis_manager import TxRedisManager

        d = TxRedisManager.from_config(config)

        def add_to_self(redis_manager):
            self._persist_redis_managers.append(redis_manager)
            return redis_manager

        return d.addCallback(add_to_self)

    def _get_sync_redis_manager(self, config):
        try:
            from vumi.persist.redis_manager import RedisManager
        except ImportError, e:
            import_skip(e, 'redis')

        redis_manager = RedisManager.from_config(config)
        self._persist_redis_managers.append(redis_manager)
        return redis_manager


class VumiWorkerTestCase(TestCase):
    """Base test class for vumi workers.

    This (or a subclass of this) should be the starting point for any test
    cases that involve vumi workers.
    """
    timeout = 5

    transport_name = "sphex"
    transport_type = None

    MSG_ID_MATCHER = RegexMatcher(r'^[0-9a-fA-F]{32}$')

    def setUp(self):
        self._workers = []
        self._amqp = FakeAMQPBroker()

    @inlineCallbacks
    def tearDown(self):
        for worker in self._workers:
            yield worker.stopWorker()

    def rkey(self, name):
        return "%s.%s" % (self.transport_name, name)

    def _rkey(self, name, connector_name=None):
        if connector_name is None:
            return self.rkey(name)
        return "%s.%s" % (connector_name, name)

    @inlineCallbacks
    def get_worker(self, config, cls, start=True):
        """Create and return an instance of a vumi worker.

        :param config: Config dict.
        :param cls: The worker class to instantiate.
        :param start: True to start the worker (default), False otherwise.
        """

        # When possible, always try and enable heartbeat setup in tests.
        # so make sure worker_name is set
        if (config is not None) and ('worker_name' not in config):
            config['worker_name'] = "unnamed"

        worker = get_stubbed_worker(cls, config, self._amqp)
        self._workers.append(worker)
        if start:
            yield worker.startWorker()
        returnValue(worker)

    def mkmsg_ack(self, user_message_id='1', sent_message_id='abc',
                  transport_metadata=None, transport_name=None):
        if transport_metadata is None:
            transport_metadata = {}
        if transport_name is None:
            transport_name = self.transport_name
        return TransportEvent(
            event_type='ack',
            user_message_id=user_message_id,
            sent_message_id=sent_message_id,
            transport_name=transport_name,
            transport_metadata=transport_metadata,
            )

    def mkmsg_nack(self, user_message_id='1', transport_metadata=None,
                    transport_name=None, nack_reason='unknown'):
        if transport_metadata is None:
            transport_metadata = {}
        if transport_name is None:
            transport_name = self.transport_name
        return TransportEvent(
            event_type='nack',
            nack_reason=nack_reason,
            user_message_id=user_message_id,
            transport_name=transport_name,
            transport_metadata=transport_metadata,
            )

    def mkmsg_delivery(self, status='delivered', user_message_id='abc',
                       transport_metadata=None, transport_name=None):
        if transport_metadata is None:
            transport_metadata = {}
        if transport_name is None:
            transport_name = self.transport_name
        return TransportEvent(
            event_type='delivery_report',
            transport_name=transport_name,
            user_message_id=user_message_id,
            delivery_status=status,
            to_addr='+41791234567',
            transport_metadata=transport_metadata,
            )

    def mkmsg_in(self, content='hello world', message_id='abc',
                 to_addr='9292', from_addr='+41791234567', group=None,
                 session_event=None, transport_type=None,
                 helper_metadata=None, transport_metadata=None,
                 transport_name=None):
        if transport_type is None:
            transport_type = self.transport_type
        if helper_metadata is None:
            helper_metadata = {}
        if transport_metadata is None:
            transport_metadata = {}
        if transport_name is None:
            transport_name = self.transport_name
        return TransportUserMessage(
            from_addr=from_addr,
            to_addr=to_addr,
            group=group,
            message_id=message_id,
            transport_name=transport_name,
            transport_type=transport_type,
            transport_metadata=transport_metadata,
            helper_metadata=helper_metadata,
            content=content,
            session_event=session_event,
            timestamp=datetime.now(),
            )

    def mkmsg_out(self, content='hello world', message_id='1',
                  to_addr='+41791234567', from_addr='9292', group=None,
                  session_event=None, in_reply_to=None,
                  transport_type=None, transport_metadata=None,
                  transport_name=None, helper_metadata=None,
                  ):
        if transport_type is None:
            transport_type = self.transport_type
        if transport_metadata is None:
            transport_metadata = {}
        if transport_name is None:
            transport_name = self.transport_name
        if helper_metadata is None:
            helper_metadata = {}
        params = dict(
            to_addr=to_addr,
            from_addr=from_addr,
            group=group,
            message_id=message_id,
            transport_name=transport_name,
            transport_type=transport_type,
            transport_metadata=transport_metadata,
            content=content,
            session_event=session_event,
            in_reply_to=in_reply_to,
            helper_metadata=helper_metadata,
            )
        return TransportUserMessage(**params)

    def _make_matcher(self, msg, *id_fields):
        msg['timestamp'] = UTCNearNow()
        for field in id_fields:
            msg[field] = self.MSG_ID_MATCHER
        return msg

    def _get_dispatched(self, name, connector_name=None):
        rkey = self._rkey(name, connector_name)
        return self._amqp.get_messages('vumi', rkey)

    def _wait_for_dispatched(self, name, amount, connector_name=None):
        rkey = self._rkey(name, connector_name)
        return self._amqp.wait_messages('vumi', rkey, amount)

    def clear_all_dispatched(self):
        self._amqp.clear_messages('vumi')

    def _clear_dispatched(self, name, connector_name=None):
        rkey = self._rkey(name, connector_name)
        return self._amqp.clear_messages('vumi', rkey)

    def get_dispatched_events(self, connector_name=None):
        return self._get_dispatched('event', connector_name)

    def get_dispatched_inbound(self, connector_name=None):
        return self._get_dispatched('inbound', connector_name)

    def get_dispatched_outbound(self, connector_name=None):
        return self._get_dispatched('outbound', connector_name)

    def get_dispatched_failures(self, connector_name=None):
        return self._get_dispatched('failures', connector_name)

    def wait_for_dispatched_events(self, amount, connector_name=None):
        return self._wait_for_dispatched('event', amount, connector_name)

    def wait_for_dispatched_inbound(self, amount, connector_name=None):
        return self._wait_for_dispatched('inbound', amount, connector_name)

    def wait_for_dispatched_outbound(self, amount, connector_name=None):
        return self._wait_for_dispatched('outbound', amount, connector_name)

    def wait_for_dispatched_failures(self, amount, connector_name=None):
        return self._wait_for_dispatched('failures', amount, connector_name)

    def clear_dispatched_events(self, connector_name=None):
        return self._clear_dispatched('event', connector_name)

    def clear_dispatched_inbound(self, connector_name=None):
        return self._clear_dispatched('inbound', connector_name)

    def clear_dispatched_outbound(self, connector_name=None):
        return self._clear_dispatched('outbound', connector_name)

    def clear_dispatched_failures(self, connector_name=None):
        return self._clear_dispatched('failures', connector_name)

    def _dispatch(self, message, rkey, exchange='vumi'):
        self._amqp.publish_message(exchange, rkey, message)
        return self._amqp.kick_delivery()

    def dispatch_inbound(self, message, connector_name=None):
        rkey = self._rkey('inbound', connector_name)
        return self._dispatch(message, rkey)

    def dispatch_outbound(self, message, connector_name=None):
        rkey = self._rkey('outbound', connector_name)
        return self._dispatch(message, rkey)

    def dispatch_event(self, message, connector_name=None):
        rkey = self._rkey('event', connector_name)
        return self._dispatch(message, rkey)

    def dispatch_failure(self, message, connector_name=None):
        rkey = self._rkey('failure', connector_name)
        return self._dispatch(message, rkey)

from datetime import datetime

from twisted.trial.unittest import TestCase
from twisted.internet.defer import inlineCallbacks, returnValue

from vumi.errors import ConfigError
from vumi.application.base import ApplicationWorker, SESSION_NEW, SESSION_CLOSE
from vumi.message import TransportUserMessage, TransportEvent
from vumi.tests.fake_amqp import FakeAMQPBroker
from vumi.tests.utils import get_stubbed_worker, PersistenceMixin


class DummyApplicationWorker(ApplicationWorker):

    SEND_TO_TAGS = frozenset(['default', 'outbound1'])

    def __init__(self, *args, **kwargs):
        super(ApplicationWorker, self).__init__(*args, **kwargs)
        self.record = []

    def consume_unknown_event(self, event):
        self.record.append(('unknown_event', event))

    def consume_ack(self, event):
        self.record.append(('ack', event))

    def consume_delivery_report(self, event):
        self.record.append(('delivery_report', event))

    def consume_user_message(self, message):
        self.record.append(('user_message', message))

    def new_session(self, message):
        self.record.append(('new_session', message))

    def close_session(self, message):
        self.record.append(('close_session', message))


class FakeUserMessage(TransportUserMessage):
    def __init__(self, **kw):
        kw['to_addr'] = 'to'
        kw['from_addr'] = 'from'
        kw['transport_name'] = 'test'
        kw['transport_type'] = 'fake'
        kw['transport_metadata'] = {}
        super(FakeUserMessage, self).__init__(**kw)


class TestApplicationWorker(TestCase):

    @inlineCallbacks
    def setUp(self):
        self.transport_name = 'test'
        self.config = {
            'transport_name': self.transport_name,
            'send_to': {
                'default': {
                    'transport_name': 'default_transport',
                    },
                'outbound1': {
                    'transport_name': 'outbound1_transport',
                    },
                },
            }
        self.worker = get_stubbed_worker(DummyApplicationWorker,
                                         config=self.config)
        self.broker = self.worker._amqp_client.broker
        yield self.worker.startWorker()

    @inlineCallbacks
    def tearDown(self):
        yield self.worker.stopWorker()

    @inlineCallbacks
    def send(self, msg, routing_suffix='inbound'):
        routing_key = "%s.%s" % (self.transport_name, routing_suffix)
        self.broker.publish_message("vumi", routing_key, msg)
        yield self.broker.kick_delivery()

    @inlineCallbacks
    def send_event(self, event):
        yield self.send(event, 'event')

    def recv(self, routing_suffix='outbound'):
        routing_key = "%s.%s" % (self.transport_name, routing_suffix)
        return self.broker.get_messages("vumi", routing_key)

    def assert_msgs_match(self, msgs, expected_msgs):
        for key in ['timestamp', 'message_id']:
            for msg in msgs + expected_msgs:
                self.assertTrue(key in msg.payload)
                msg[key] = 'OVERRIDDEN_BY_TEST'

        for msg, expected_msg in zip(msgs, expected_msgs):
            self.assertEqual(msg, expected_msg)
        self.assertEqual(len(msgs), len(expected_msgs))

    @inlineCallbacks
    def test_event_dispatch(self):
        events = [
            ('ack', TransportEvent(event_type='ack',
                                   sent_message_id='remote-id',
                                   user_message_id='ack-uuid')),
            ('delivery_report', TransportEvent(event_type='delivery_report',
                                               delivery_status='pending',
                                               user_message_id='dr-uuid')),
            ]
        for name, event in events:
            yield self.send_event(event)
            self.assertEqual(self.worker.record, [(name, event)])
            del self.worker.record[:]

    @inlineCallbacks
    def test_unknown_event_dispatch(self):
        # temporarily pretend the worker doesn't know about acks
        del self.worker._event_handlers['ack']
        bad_event = TransportEvent(event_type='ack',
                                   sent_message_id='remote-id',
                                   user_message_id='bad-uuid')
        yield self.send_event(bad_event)
        self.assertEqual(self.worker.record, [('unknown_event', bad_event)])

    @inlineCallbacks
    def test_user_message_dispatch(self):
        messages = [
            ('user_message', FakeUserMessage()),
            ('new_session', FakeUserMessage(session_event=SESSION_NEW)),
            ('close_session', FakeUserMessage(session_event=SESSION_CLOSE)),
            ]
        for name, message in messages:
            yield self.send(message)
            self.assertEqual(self.worker.record, [(name, message)])
            del self.worker.record[:]

    @inlineCallbacks
    def test_reply_to(self):
        msg = FakeUserMessage()
        yield self.worker.reply_to(msg, "More!")
        yield self.worker.reply_to(msg, "End!", False)
        replies = self.recv()
        expecteds = [msg.reply("More!"), msg.reply("End!", False)]
        self.assert_msgs_match(replies, expecteds)

    @inlineCallbacks
    def test_reply_to_group(self):
        msg = FakeUserMessage()
        yield self.worker.reply_to_group(msg, "Group!")
        replies = self.recv()
        expecteds = [msg.reply_group("Group!")]
        self.assert_msgs_match(replies, expecteds)

    @inlineCallbacks
    def test_send_to(self):
        sent_msg = yield self.worker.send_to('+12345', "Hi!")
        sends = self.recv()
        expecteds = [TransportUserMessage.send('+12345', "Hi!",
                transport_name='default_transport')]
        self.assert_msgs_match(sends, expecteds)
        self.assert_msgs_match(sends, [sent_msg])

    @inlineCallbacks
    def test_send_to_with_options(self):
        sent_msg = yield self.worker.send_to('+12345', "Hi!",
                transport_type=TransportUserMessage.TT_USSD)
        sends = self.recv()
        expecteds = [TransportUserMessage.send('+12345', "Hi!",
                transport_type=TransportUserMessage.TT_USSD,
                transport_name='default_transport')]
        self.assert_msgs_match(sends, expecteds)
        self.assert_msgs_match(sends, [sent_msg])

    @inlineCallbacks
    def test_send_to_with_tag(self):
        sent_msg = yield self.worker.send_to('+12345', "Hi!", "outbound1",
                transport_type=TransportUserMessage.TT_USSD)
        sends = self.recv()
        expecteds = [TransportUserMessage.send('+12345', "Hi!",
                transport_type=TransportUserMessage.TT_USSD,
                transport_name='outbound1_transport')]
        self.assert_msgs_match(sends, expecteds)
        self.assert_msgs_match(sends, [sent_msg])

    def test_send_to_with_bad_tag(self):
        self.assertRaises(ValueError, self.worker.send_to,
                          '+12345', "Hi!", "outbound_unknown")

    @inlineCallbacks
    def test_send_to_with_no_send_to_tags(self):
        config = {'transport_name': 'notags_app'}
        notags_worker = get_stubbed_worker(ApplicationWorker,
                                           config=config)
        yield notags_worker.startWorker()
        self.assertRaises(ValueError, notags_worker.send_to,
                          '+12345', "Hi!")

    @inlineCallbacks
    def test_send_to_with_bad_config(self):
        config = {'transport_name': 'badconfig_app',
                  'send_to': {
                      'default': {},  # missing transport_name
                      'outbound1': {},  # also missing transport_name
                      },
                  }
        badcfg_worker = get_stubbed_worker(DummyApplicationWorker,
                                           config=config)
        errors = []
        d = badcfg_worker.startWorker()
        d.addErrback(lambda result: errors.append(result))
        yield d
        self.assertEqual(errors[0].type, ConfigError)

    def test_subclassing_api(self):
        worker = get_stubbed_worker(ApplicationWorker,
                                    {'transport_name': 'test'})
        ack = TransportEvent(event_type='ack',
                             sent_message_id='remote-id',
                             user_message_id='ack-uuid')
        dr = TransportEvent(event_type='delivery_report',
                            delivery_status='pending',
                            user_message_id='dr-uuid')
        worker.consume_ack(ack)
        worker.consume_delivery_report(dr)
        worker.consume_unknown_event(FakeUserMessage())
        worker.consume_user_message(FakeUserMessage())
        worker.new_session(FakeUserMessage())
        worker.close_session(FakeUserMessage())


class ApplicationTestCase(TestCase, PersistenceMixin):

    """
    This is a base class for testing application workers.

    """

    # base timeout of 5s for all application tests
    timeout = 5

    transport_name = "sphex"
    transport_type = None
    application_class = None

    def setUp(self):
        self._workers = []
        self._amqp = FakeAMQPBroker()
        self._persist_setUp()

    @inlineCallbacks
    def tearDown(self):
        for worker in self._workers:
            yield worker.stopWorker()
        yield self._persist_tearDown()

    def rkey(self, name):
        return "%s.%s" % (self.transport_name, name)

    @inlineCallbacks
    def get_application(self, config, cls=None, start=True):
        """
        Get an instance of a worker class.

        :param config: Config dict.
        :param cls: The Application class to instantiate.
                    Defaults to :attr:`application_class`
        :param start: True to start the application (default), False otherwise.

        Some default config values are helpfully provided in the
        interests of reducing boilerplate:

        * ``transport_name`` defaults to :attr:`self.transport_name`
        * ``send_to`` defaults to a dictionary with config for each tag
          defined in worker's SEND_TO_TAGS attribute. Each tag's config
          contains a transport_name set to ``<tag>_outbound``.
        """

        if cls is None:
            cls = self.application_class
        config = self.mk_config(config)
        config.setdefault('transport_name', self.transport_name)
        if 'send_to' not in config and cls.SEND_TO_TAGS:
            config['send_to'] = {}
            for tag in cls.SEND_TO_TAGS:
                config['send_to'][tag] = {
                    'transport_name': '%s_outbound' % tag}
        worker = get_stubbed_worker(cls, config, self._amqp)
        self._workers.append(worker)
        if start:
            yield worker.startWorker()
        returnValue(worker)

    def mkmsg_in(self, content='hello world', message_id='abc',
                 to_addr='9292', from_addr='+41791234567', group=None,
                 session_event=None, transport_type=None,
                 helper_metadata=None, transport_metadata=None):
        if transport_type is None:
            transport_type = self.transport_type
        if helper_metadata is None:
            helper_metadata = {}
        if transport_metadata is None:
            transport_metadata = {}
        return TransportUserMessage(
            from_addr=from_addr,
            to_addr=to_addr,
            group=group,
            message_id=message_id,
            transport_name=self.transport_name,
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
                  ):
        if transport_type is None:
            transport_type = self.transport_type
        if transport_metadata is None:
            transport_metadata = {}
        params = dict(
            to_addr=to_addr,
            from_addr=from_addr,
            group=group,
            message_id=message_id,
            transport_name=self.transport_name,
            transport_type=transport_type,
            transport_metadata=transport_metadata,
            content=content,
            session_event=session_event,
            in_reply_to=in_reply_to,
            )
        return TransportUserMessage(**params)

    def mkmsg_ack(self, user_message_id='1', sent_message_id='abc',
                  transport_metadata=None):
        if transport_metadata is None:
            transport_metadata = {}
        return TransportEvent(
            event_type='ack',
            user_message_id=user_message_id,
            sent_message_id=sent_message_id,
            transport_name=self.transport_name,
            transport_metadata=transport_metadata,
            )

    def mkmsg_delivery(self, status='delivered', user_message_id='abc',
                       transport_metadata=None):
        if transport_metadata is None:
            transport_metadata = {}
        return TransportEvent(
            event_type='delivery_report',
            transport_name=self.transport_name,
            user_message_id=user_message_id,
            delivery_status=status,
            to_addr='+41791234567',
            transport_metadata=transport_metadata,
            )

    def get_dispatched_messages(self):
        return self._amqp.get_messages('vumi', self.rkey('outbound'))

    def wait_for_dispatched_messages(self, amount):
        return self._amqp.wait_messages('vumi', self.rkey('outbound'), amount)

    def dispatch(self, message, rkey=None, exchange='vumi'):
        if rkey is None:
            rkey = self.rkey('inbound')
        self._amqp.publish_message(exchange, rkey, message)
        return self._amqp.kick_delivery()


class TestApplicationMiddlewareHooks(ApplicationTestCase):

    transport_name = 'carrier_pigeon'
    application_class = ApplicationWorker

    TEST_MIDDLEWARE_CONFIG = {
       "middleware": [
            {"mw1": "vumi.middleware.tests.utils.RecordingMiddleware"},
            {"mw2": "vumi.middleware.tests.utils.RecordingMiddleware"},
            ],
        }

    @inlineCallbacks
    def test_middleware_for_inbound_messages(self):
        app = yield self.get_application(self.TEST_MIDDLEWARE_CONFIG)
        msgs = []
        app.consume_user_message = msgs.append
        orig_msg = self.mkmsg_in()
        orig_msg['timestamp'] = 0
        yield app.dispatch_user_message(orig_msg)
        [msg] = msgs
        self.assertEqual(msg['record'], [
            ('mw1', 'inbound', self.transport_name),
            ('mw2', 'inbound', self.transport_name),
            ])

    @inlineCallbacks
    def test_middleware_for_events(self):
        app = yield self.get_application(self.TEST_MIDDLEWARE_CONFIG)
        msgs = []
        app._event_handlers['ack'] = msgs.append
        orig_msg = self.mkmsg_ack()
        orig_msg['event_id'] = 1234
        orig_msg['timestamp'] = 0
        yield app.dispatch_event(orig_msg)
        [msg] = msgs
        self.assertEqual(msg['record'], [
            ('mw1', 'event', self.transport_name),
            ('mw2', 'event', self.transport_name),
            ])

    @inlineCallbacks
    def test_middleware_for_outbound_messages(self):
        app = yield self.get_application(self.TEST_MIDDLEWARE_CONFIG)
        orig_msg = self.mkmsg_out()
        yield app.reply_to(orig_msg, 'Hello!')
        msgs = self.get_dispatched_messages()
        [msg] = msgs
        self.assertEqual(msg['record'], [
            ['mw2', 'outbound', self.transport_name],
            ['mw1', 'outbound', self.transport_name],
            ])

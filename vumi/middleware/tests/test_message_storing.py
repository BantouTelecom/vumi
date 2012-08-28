"""Tests for vumi.middleware.message_storing."""

from twisted.trial.unittest import TestCase
from twisted.internet.defer import inlineCallbacks

from vumi.middleware.tagger import TaggingMiddleware
from vumi.message import TransportUserMessage, TransportEvent
from vumi.tests.utils import PersistenceMixin


class StoringMiddlewareTestCase(TestCase, PersistenceMixin):

    DEFAULT_CONFIG = {
        }

    @inlineCallbacks
    def setUp(self):
        self._persist_setUp()
        dummy_worker = object()
        config = self.mk_config({})

        # Create and stash a riak manager to clean up afterwards, because we
        # don't get access to the one inside the middleware.
        self.get_riak_manager()

        # We've already skipped the test by now if we don't have riakasaurus,
        # so it's safe to import stuff that pulls it in without guards.
        from vumi.middleware.message_storing import StoringMiddleware

        self.mw = StoringMiddleware("dummy_storer", config, dummy_worker)
        yield self.mw.setup_middleware()
        self.store = self.mw.store
        yield self.store.manager.purge_all()
        yield self.store.redis._purge_all()  # just in case

    @inlineCallbacks
    def tearDown(self):
        yield self.mw.teardown_middleware()
        yield self.store.manager.purge_all()
        yield self._persist_tearDown()

    def mk_msg(self):
        msg = TransportUserMessage(to_addr="45678", from_addr="12345",
                                   transport_name="dummy_endpoint",
                                   transport_type="dummy_transport_type")
        return msg

    def mk_ack(self, user_message_id="1"):
        ack = TransportEvent(event_type="ack", user_message_id=user_message_id,
                             sent_message_id="1")
        return ack

    @inlineCallbacks
    def test_handle_outbound(self):
        msg = self.mk_msg()
        msg_id = msg['message_id']
        response = yield self.mw.handle_outbound(msg, "dummy_endpoint")
        self.assertTrue(isinstance(response, TransportUserMessage))

        stored_msg = yield self.store.get_outbound_message(msg_id)
        message_events = yield self.store.message_events(msg_id)

        self.assertEqual(stored_msg, msg)
        self.assertEqual(message_events, [])

    @inlineCallbacks
    def test_handle_outbound_with_tag(self):
        batch_id = yield self.store.batch_start([("pool", "tag")])
        msg = self.mk_msg()
        msg_id = msg['message_id']
        TaggingMiddleware.add_tag_to_msg(msg, ["pool", "tag"])
        response = yield self.mw.handle_outbound(msg, "dummy_endpoint")
        self.assertTrue(isinstance(response, TransportUserMessage))

        stored_msg = yield self.store.get_outbound_message(msg_id)
        message_events = yield self.store.message_events(msg_id)
        batch_messages = yield self.store.batch_messages(batch_id)
        batch_replies = yield self.store.batch_replies(batch_id)

        self.assertEqual(stored_msg, msg)
        self.assertEqual(message_events, [])
        self.assertEqual(batch_messages, [msg])
        self.assertEqual(batch_replies, [])

    @inlineCallbacks
    def test_handle_inbound(self):
        msg = self.mk_msg()
        msg_id = msg['message_id']
        response = yield self.mw.handle_inbound(msg, "dummy_endpoint")
        self.assertTrue(isinstance(response, TransportUserMessage))

        stored_msg = yield self.store.get_inbound_message(msg_id)

        self.assertEqual(stored_msg, msg)

    @inlineCallbacks
    def test_handle_inbound_with_tag(self):
        batch_id = yield self.store.batch_start([("pool", "tag")])
        msg = self.mk_msg()
        msg_id = msg['message_id']
        TaggingMiddleware.add_tag_to_msg(msg, ["pool", "tag"])
        response = yield self.mw.handle_inbound(msg, "dummy_endpoint")
        self.assertTrue(isinstance(response, TransportUserMessage))

        stored_msg = yield self.store.get_inbound_message(msg_id)
        batch_messages = yield self.store.batch_messages(batch_id)
        batch_replies = yield self.store.batch_replies(batch_id)

        self.assertEqual(stored_msg, msg)
        self.assertEqual(batch_messages, [])
        self.assertEqual(batch_replies, [msg])

    @inlineCallbacks
    def test_handle_event(self):
        msg = self.mk_msg()
        msg_id = msg["message_id"]
        yield self.store.add_outbound_message(msg)

        ack = self.mk_ack(user_message_id=msg_id)
        event_id = ack['event_id']
        response = yield self.mw.handle_event(ack, "dummy_endpoint")
        self.assertTrue(isinstance(response, TransportEvent))

        stored_event = yield self.store.get_event(event_id)
        message_events = yield self.store.message_events(msg_id)

        self.assertEqual(stored_event, ack)
        self.assertEqual(message_events, [ack])

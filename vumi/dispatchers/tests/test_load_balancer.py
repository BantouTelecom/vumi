"""Tests for vumi.dispatchers.load_balancer."""

from twisted.internet.defer import inlineCallbacks

from vumi.tests.utils import VumiWorkerTestCase, LogCatcher
from vumi.dispatchers.tests.utils import DummyDispatcher
from vumi.dispatchers.load_balancer import LoadBalancingRouter


class BaseLoadBalancingTestCase(VumiWorkerTestCase):

    reply_affinity = None
    rewrite_transport_names = None

    @inlineCallbacks
    def setUp(self):
        yield super(BaseLoadBalancingTestCase, self).setUp()
        config = {
            "transport_names": [
                "transport_1",
                "transport_2",
            ],
            "exposed_names": ["round_robin"],
            "router_class": ("vumi.dispatchers.load_balancer."
                             "LoadBalancingRouter"),
        }
        if self.reply_affinity is not None:
            config['reply_affinity'] = self.reply_affinity
        if self.rewrite_transport_names is not None:
            config['rewrite_transport_names'] = self.rewrite_transport_names
        self.dispatcher = DummyDispatcher(config)
        self.router = LoadBalancingRouter(self.dispatcher, config)
        yield self.router.setup_routing()

    @inlineCallbacks
    def tearDown(self):
        yield super(BaseLoadBalancingTestCase, self).tearDown()
        yield self.router.teardown_routing()


class TestLoadBalancingWithoutReplyAffinity(BaseLoadBalancingTestCase):

    reply_affinity = False

    def test_inbound_message_routing(self):
        msg1 = self.mkmsg_in(content='msg 1', transport_name='transport_1')
        self.router.dispatch_inbound_message(msg1)
        msg2 = self.mkmsg_in(content='msg 2', transport_name='transport_2')
        self.router.dispatch_inbound_message(msg2)
        publishers = self.dispatcher.exposed_publisher
        self.assertEqual(publishers['round_robin'].msgs, [msg1, msg2])

    def test_inbound_event_routing(self):
        msg1 = self.mkmsg_ack(transport_name='transport_1')
        self.router.dispatch_inbound_event(msg1)
        msg2 = self.mkmsg_ack(transport_name='transport_2')
        self.router.dispatch_inbound_event(msg2)
        publishers = self.dispatcher.exposed_event_publisher
        self.assertEqual(publishers['round_robin'].msgs, [msg1, msg2])

    def test_outbound_message_routing(self):
        msg1 = self.mkmsg_out(content='msg 1')
        self.router.dispatch_outbound_message(msg1)
        msg2 = self.mkmsg_out(content='msg 2')
        self.router.dispatch_outbound_message(msg2)
        msg3 = self.mkmsg_out(content='msg 3')
        self.router.dispatch_outbound_message(msg3)
        publishers = self.dispatcher.transport_publisher
        self.assertEqual(publishers['transport_1'].msgs, [msg1, msg3])
        self.assertEqual(publishers['transport_2'].msgs, [msg2])


class TestLoadBalancingWithReplyAffinity(BaseLoadBalancingTestCase):

    reply_affinity = True

    def test_inbound_message_routing(self):
        msg1 = self.mkmsg_in(content='msg 1', transport_name='transport_1')
        self.router.dispatch_inbound_message(msg1)
        msg2 = self.mkmsg_in(content='msg 2', transport_name='transport_2')
        self.router.dispatch_inbound_message(msg2)
        publishers = self.dispatcher.exposed_publisher
        self.assertEqual(publishers['round_robin'].msgs, [msg1, msg2])

    def test_inbound_event_routing(self):
        msg1 = self.mkmsg_ack(transport_name='transport_1')
        self.router.dispatch_inbound_event(msg1)
        msg2 = self.mkmsg_ack(transport_name='transport_2')
        self.router.dispatch_inbound_event(msg2)
        publishers = self.dispatcher.exposed_event_publisher
        self.assertEqual(publishers['round_robin'].msgs, [msg1, msg2])

    def test_outbound_message_routing(self):
        msg1 = self.mkmsg_out(content='msg 1', in_reply_to='msg X')
        self.router.push_transport_name(msg1, 'transport_1')
        self.router.dispatch_outbound_message(msg1)
        msg2 = self.mkmsg_out(content='msg 2', in_reply_to='msg X')
        self.router.push_transport_name(msg2, 'transport_1')
        self.router.dispatch_outbound_message(msg2)
        msg3 = self.mkmsg_out(content='msg 3', in_reply_to='msg X')
        self.router.push_transport_name(msg3, 'transport_2')
        self.router.dispatch_outbound_message(msg3)
        publishers = self.dispatcher.transport_publisher
        self.assertEqual(publishers['transport_1'].msgs, [msg1, msg2])
        self.assertEqual(publishers['transport_2'].msgs, [msg3])

    def test_outbound_message_with_unknown_transport_name(self):
        # we expect unknown outbound transport_names to be
        # round-robinned and logged.
        msg1 = self.mkmsg_out(content='msg 1', in_reply_to='msg X')
        self.router.push_transport_name(msg1, 'transport_unknown')
        with LogCatcher() as lc:
            self.router.dispatch_outbound_message(msg1)
            [errmsg] = lc.messages()
            self.assertTrue("unknown load balancer endpoint "
                            "'transport_unknown' was was received" in errmsg)
        publishers = self.dispatcher.transport_publisher
        self.assertEqual(publishers['transport_1'].msgs, [msg1])


class TestLoadBalancingWithRewriteTransportNames(BaseLoadBalancingTestCase):

    rewrite_transport_names = True

    def test_inbound_message_routing(self):
        msg = self.mkmsg_in(content='msg 1', transport_name='transport_1')
        self.router.dispatch_inbound_message(msg)
        [new_msg] = self.dispatcher.exposed_publisher['round_robin'].msgs
        self.assertEqual(new_msg['transport_name'], 'round_robin')

    def test_inbound_event_routing(self):
        msg = self.mkmsg_ack(transport_name='transport_1')
        self.router.dispatch_inbound_event(msg)
        [new_msg] = self.dispatcher.exposed_event_publisher['round_robin'].msgs
        self.assertEqual(new_msg['transport_name'], 'round_robin')

    def test_outbound_message_routing(self):
        msg1 = self.mkmsg_out(content='msg 1', transport_name='round_robin')
        self.router.dispatch_outbound_message(msg1)
        [new_msg] = self.dispatcher.transport_publisher['transport_1'].msgs
        self.assertEqual(new_msg['transport_name'], 'transport_1')


class TestLoadBalancingWithoutRewriteTransportNames(BaseLoadBalancingTestCase):

    rewrite_transport_names = False

    def test_inbound_message_routing(self):
        msg = self.mkmsg_in(content='msg 1', transport_name='transport_1')
        self.router.dispatch_inbound_message(msg)
        [new_msg] = self.dispatcher.exposed_publisher['round_robin'].msgs
        self.assertEqual(new_msg['transport_name'], 'transport_1')

    def test_inbound_event_routing(self):
        msg = self.mkmsg_ack(transport_name='transport_1')
        self.router.dispatch_inbound_event(msg)
        [new_msg] = self.dispatcher.exposed_event_publisher['round_robin'].msgs
        self.assertEqual(new_msg['transport_name'], 'transport_1')

    def test_outbound_message_routing(self):
        msg1 = self.mkmsg_out(content='msg 1', transport_name='round_robin')
        self.router.dispatch_outbound_message(msg1)
        [new_msg] = self.dispatcher.transport_publisher['transport_1'].msgs
        self.assertEqual(new_msg['transport_name'], 'round_robin')

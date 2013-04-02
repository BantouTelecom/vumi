import binascii

from twisted.internet.defer import Deferred, inlineCallbacks, succeed
from twisted.internet.task import Clock
from smpp.pdu_builder import SubmitSMResp, DeliverSM

from vumi.message import TransportUserMessage
from vumi.transports.smpp.clientserver.client import (
    EsmeTransceiver, EsmeCallbacks)
from vumi.transports.smpp.transport import (SmppTransport,
                                            SmppTxTransport,
                                            SmppRxTransport)
from vumi.transports.smpp.service import SmppService
from vumi.transports.smpp.clientserver.config import ClientConfig
from vumi.transports.smpp.clientserver.client import unpacked_pdu_opts
from vumi.transports.smpp.clientserver.tests.utils import SmscTestServer
from vumi.transports.tests.utils import TransportTestCase
from vumi.tests.utils import LogCatcher


class SmppTransportTestCase(TransportTestCase):
    transport_class = SmppTransport

    @inlineCallbacks
    def setUp(self):
        super(SmppTransportTestCase, self).setUp()
        self.config = {
                "transport_name": self.transport_name,
                "system_id": "vumitest-vumitest-vumitest",
                "host": "host",
                "port": "port",
                "password": "password",
                "smpp_bind_timeout": 12,
                "smpp_enquire_link_interval": 123,
                "third_party_id_expiry": 3600,  # just 1 hour
                }
        self.clientConfig = ClientConfig.from_config(self.config)

        # hack a lot of transport setup
        self.transport = yield self.get_transport(self.config, start=False)
        self.transport.esme_client = None
        yield self.transport.startWorker()

        self._make_esme()
        self.transport.esme_client = self.esme
        self.transport.esme_connected(self.esme)

    def _make_esme(self):
        self.esme_callbacks = EsmeCallbacks(
            connect=lambda: None, disconnect=lambda: None,
            submit_sm_resp=self.transport.submit_sm_resp,
            delivery_report=self.transport.delivery_report,
            deliver_sm=lambda: None)
        self.esme = EsmeTransceiver(
            self.clientConfig, self.transport.redis, self.esme_callbacks)
        self.esme.sent_pdus = []
        self.esme.send_pdu = self.esme.sent_pdus.append
        self.esme.state = 'BOUND_TRX'

    def assert_sent_contents(self, expected):
        pdu_contents = [p.obj['body']['mandatory_parameters']['short_message']
                        for p in self.esme.sent_pdus]
        self.assertEqual(expected, pdu_contents)

    def test_bind_and_enquire_config(self):
        self.assertEqual(12, self.transport.client_config.smpp_bind_timeout)
        self.assertEqual(123,
                self.transport.client_config.smpp_enquire_link_interval)
        self.assertEqual(repr(123.0),
                repr(self.transport.client_config.smpp_enquire_link_interval))

    @inlineCallbacks
    def test_message_persistence(self):
        # A simple test of set -> get -> delete for redis message persistence
        message1 = self.mkmsg_out(
            message_id='1234567890abcdefg',
            content="hello world",
            to_addr="far-far-away")
        original_json = message1.to_json()
        yield self.transport.r_set_message(message1)
        retrieved_json = yield self.transport.r_get_message_json(
            message1['message_id'])
        self.assertEqual(original_json, retrieved_json)
        retrieved_message = yield self.transport.r_get_message(
            message1['message_id'])
        self.assertEqual(retrieved_message, message1)
        self.assertTrue((yield self.transport.r_delete_message(
                    message1['message_id'])))
        self.assertEqual((yield self.transport.r_get_message_json(
                    message1['message_id'])), None)
        self.assertEqual((yield self.transport.r_get_message(
                    message1['message_id'])), None)

    @inlineCallbacks
    def test_redis_third_party_id_persistence(self):
        # Testing: set -> get -> delete, for redis third party id mapping
        self.assertEqual(self.transport.third_party_id_expiry, 3600)
        our_id = "blergh34534545433454354"
        their_id = "omghesvomitingnumbers"
        yield self.transport.r_set_id_for_third_party_id(their_id, our_id)
        retrieved_our_id = (
            yield self.transport.r_get_id_for_third_party_id(their_id))
        self.assertEqual(our_id, retrieved_our_id)
        self.assertTrue((
                yield self.transport.r_delete_for_third_party_id(their_id)))
        self.assertEqual(None, (
                yield self.transport.r_get_id_for_third_party_id(their_id)))

    @inlineCallbacks
    def test_out_of_order_responses(self):
        # Sequence numbers are hardcoded, assuming we start fresh from 0.
        message1 = self.mkmsg_out("message 1", message_id='444')
        response1 = SubmitSMResp(1, "3rd_party_id_1")
        yield self.dispatch(message1)

        message2 = self.mkmsg_out("message 2", message_id='445')
        response2 = SubmitSMResp(2, "3rd_party_id_2")
        yield self.dispatch(message2)

        self.assert_sent_contents(["message 1", "message 2"])
        # respond out of order - just to keep things interesting
        yield self.esme.handle_data(response2.get_bin())
        yield self.esme.handle_data(response1.get_bin())

        self.assertEqual([
                self.mkmsg_ack('445', '3rd_party_id_2'),
                self.mkmsg_ack('444', '3rd_party_id_1'),
                ], self.get_dispatched_events())

    @inlineCallbacks
    def test_failed_submit(self):
        message = self.mkmsg_out("message", message_id='446')
        response = SubmitSMResp(1, "3rd_party_id_3",
                                command_status="ESME_RSUBMITFAIL")
        yield self.dispatch(message)
        yield self.esme.handle_data(response.get_bin())

        self.assert_sent_contents(["message"])
        # There should be a nack
        [nack] = yield self.wait_for_dispatched_events(1)
        self.assertEqual(nack['user_message_id'], message['message_id'])
        self.assertEqual(nack['nack_reason'], 'ESME_RSUBMITFAIL')

        comparison = self.mkmsg_fail(message.payload, 'ESME_RSUBMITFAIL')
        [actual] = yield self.get_dispatched_failures()
        self.assertEqual(actual, comparison)

    @inlineCallbacks
    def test_failed_submit_with_no_reason(self):
        message = self.mkmsg_out("message", message_id='446')
        # Equivalent of SubmitSMResp(1, "3rd_party_id_3", command_status='XXX')
        # but with a bad command_status (pdu_builder can't produce binary with
        # command_statuses' it doesn't understand). Use
        # smpp.pdu.unpack(response_bin) to get a PDU object:
        response_hex = ("0000001f80000004"
                        "0000ffff"  # unknown command status
                        "000000013372645f70617274795f69645f3300")
        response_bin = binascii.a2b_hex(response_hex)
        yield self.dispatch(message)
        yield self.esme.handle_data(response_bin)

        self.assert_sent_contents(["message"])
        # There should be a nack
        [nack] = yield self.wait_for_dispatched_events(1)
        self.assertEqual(nack['user_message_id'], message['message_id'])
        self.assertEqual(nack['nack_reason'], 'Unspecified')

        comparison = self.mkmsg_fail(message.payload, 'Unspecified')
        [actual] = yield self.get_dispatched_failures()
        self.assertEqual(actual, comparison)

    @inlineCallbacks
    def test_delivery_report_for_unknown_message(self):
        dr = ("id:123 sub:... dlvrd:... submit date:200101010030"
              " done date:200101020030 stat:DELIVRD err:... text:Meep")
        deliver = DeliverSM(1, short_message=dr)
        with LogCatcher(message="Failed to retrieve message id") as lc:
            yield self.esme.handle_data(deliver.get_bin())
            [warning] = lc.logs
            self.assertEqual(warning['message'],
                             ("Failed to retrieve message id for delivery "
                              "report. Delivery report from sphex "
                              "discarded.",))

    @inlineCallbacks
    def test_throttled_submit(self):
        clock = Clock()
        self.transport.callLater = clock.callLater

        def assert_throttled_status(throttled, messages, acks):
            self.assertEqual(self.transport.throttled, throttled)
            self.assert_sent_contents(messages)
            self.assertEqual(acks, self.get_dispatched_events())
            self.assertEqual([], self.get_dispatched_failures())

        assert_throttled_status(False, [], [])

        message = self.mkmsg_out("Heimlich", message_id="447")
        response = SubmitSMResp(1, "3rd_party_id_4",
                                command_status="ESME_RTHROTTLED")
        yield self.dispatch(message)
        yield self.esme.handle_data(response.get_bin())

        assert_throttled_status(True, ["Heimlich"], [])
        # Still waiting to resend
        clock.advance(0.05)
        assert_throttled_status(True, ["Heimlich"], [])
        message2 = self.mkmsg_out("Other", message_id="448")
        yield self.dispatch(message2)
        assert_throttled_status(True, ["Heimlich"], [])
        # Resent
        clock.advance(0.05)
        assert_throttled_status(True, ["Heimlich", "Heimlich"], [])
        # And acknowledged by the other side
        yield self.esme.handle_data(SubmitSMResp(2, "3rd_party_5").get_bin())
        yield self._amqp.kick_delivery()
        yield self.esme.handle_data(SubmitSMResp(3, "3rd_party_6").get_bin())
        assert_throttled_status(False, ["Heimlich", "Heimlich", "Other"],
                                [self.mkmsg_ack('447', '3rd_party_5'),
                                 self.mkmsg_ack('448', '3rd_party_6')])

    @inlineCallbacks
    def test_reconnect(self):
        connector = self.transport.connectors[self.transport.transport_name]
        self.assertFalse(connector._consumers['outbound'].paused)
        yield self.transport.esme_disconnected()
        self.assertTrue(connector._consumers['outbound'].paused)
        yield self.transport.esme_disconnected()
        self.assertTrue(connector._consumers['outbound'].paused)

        yield self.transport.esme_connected(self.esme)
        self.assertFalse(connector._consumers['outbound'].paused)
        yield self.transport.esme_connected(self.esme)
        self.assertFalse(connector._consumers['outbound'].paused)


class MockSmppTransport(SmppTransport):
    @inlineCallbacks
    def esme_connected(self, client):
        yield super(MockSmppTransport, self).esme_connected(client)
        self._block_till_bind.callback(None)


class MockSmppTxTransport(SmppTxTransport):
    @inlineCallbacks
    def esme_connected(self, client):
        yield super(MockSmppTxTransport, self).esme_connected(client)
        self._block_till_bind.callback(None)


class MockSmppRxTransport(SmppRxTransport):
    @inlineCallbacks
    def esme_connected(self, client):
        yield super(MockSmppRxTransport, self).esme_connected(client)
        self._block_till_bind.callback(None)


def mk_expected_pdu(direction, sequence_number, command_id, **extras):
    headers = {
        'command_status': 'ESME_ROK',
        'sequence_number': sequence_number,
        'command_id': command_id,
        }
    headers.update(extras)
    return {"direction": direction, "pdu": {"header": headers}}


class EsmeToSmscTestCase(TransportTestCase):

    transport_name = "esme_testing_transport"
    transport_class = MockSmppTransport

    def assert_pdu_header(self, expected, actual, field):
        self.assertEqual(expected['pdu']['header'][field],
                         actual['pdu']['header'][field])

    def assert_server_pdu(self, expected, actual):
        self.assertEqual(expected['direction'], actual['direction'])
        self.assert_pdu_header(expected, actual, 'sequence_number')
        self.assert_pdu_header(expected, actual, 'command_status')
        self.assert_pdu_header(expected, actual, 'command_id')

    @inlineCallbacks
    def clear_link_pdus(self):
        for expected in [
                mk_expected_pdu("inbound", 1, "bind_transceiver"),
                mk_expected_pdu("outbound", 1, "bind_transceiver_resp"),
                mk_expected_pdu("inbound", 2, "enquire_link"),
                mk_expected_pdu("outbound", 2, "enquire_link_resp")]:
            pdu = yield self.service.factory.smsc.pdu_queue.get()
            self.assert_server_pdu(expected, pdu)

    @inlineCallbacks
    def setUp(self):
        yield super(EsmeToSmscTestCase, self).setUp()
        self.config = {
            "system_id": "VumiTestSMSC",
            "password": "password",
            "host": "localhost",
            "port": 0,
            "transport_name": self.transport_name,
            "transport_type": "smpp",
        }
        self.service = SmppService(None, config=self.config)
        yield self.service.startWorker()
        self.service.factory.protocol = SmscTestServer
        self.config['port'] = self.service.listening.getHost().port
        self.transport = yield self.get_transport(self.config, start=False)
        self.expected_delivery_status = 'delivered'

    @inlineCallbacks
    def startTransport(self):
        self.transport._block_till_bind = Deferred()
        yield self.transport.startWorker()

    @inlineCallbacks
    def tearDown(self):
        yield super(EsmeToSmscTestCase, self).tearDown()
        self.transport.factory.stopTrying()
        self.transport.factory.esme.transport.loseConnection()
        yield self.service.listening.stopListening()
        yield self.service.listening.loseConnection()

    @inlineCallbacks
    def test_handshake_submit_and_deliver(self):

        # 1111111111111111111111111111111111111111111111111
        expected_pdus_1 = [
            mk_expected_pdu("inbound", 1, "bind_transceiver"),
            mk_expected_pdu("outbound", 1, "bind_transceiver_resp"),
            mk_expected_pdu("inbound", 2, "enquire_link"),
            mk_expected_pdu("outbound", 2, "enquire_link_resp"),
        ]

        # 2222222222222222222222222222222222222222222222222
        expected_pdus_2 = [
            mk_expected_pdu("inbound", 3, "submit_sm"),
            mk_expected_pdu("outbound", 3, "submit_sm_resp"),
            # the delivery report
            mk_expected_pdu("outbound", 1, "deliver_sm"),
            mk_expected_pdu("inbound", 1, "deliver_sm_resp"),
        ]

        # 3333333333333333333333333333333333333333333333333
        expected_pdus_3 = [
            # a sms delivered by the smsc
            mk_expected_pdu("outbound", 555, "deliver_sm"),
            mk_expected_pdu("inbound", 555, "deliver_sm_resp"),
        ]

        ## Startup
        yield self.startTransport()
        yield self.transport._block_till_bind

        # First we make sure the Client binds to the Server
        # and enquire_link pdu's are exchanged as expected
        pdu_queue = self.service.factory.smsc.pdu_queue

        for expected_message in expected_pdus_1:
            actual_message = yield pdu_queue.get()
            self.assert_server_pdu(expected_message, actual_message)

        # Next the Client submits a SMS to the Server
        # and recieves an ack and a delivery_report

        msg = TransportUserMessage(
                to_addr="2772222222",
                from_addr="2772000000",
                content='hello world',
                transport_name=self.transport_name,
                transport_type='smpp',
                transport_metadata={},
                rkey='%s.outbound' % self.transport_name,
                timestamp='0',
                )
        yield self.dispatch(msg)

        for expected_message in expected_pdus_2:
            actual_message = yield pdu_queue.get()
            self.assert_server_pdu(expected_message, actual_message)

        # We need the user_message_id to check the ack
        user_message_id = msg["message_id"]

        [ack, delv] = yield self.wait_for_dispatched_events(2)

        self.assertEqual(ack['message_type'], 'event')
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['transport_name'], self.transport_name)
        self.assertEqual(ack['user_message_id'], user_message_id)

        self.assertEqual(delv['message_type'], 'event')
        self.assertEqual(delv['event_type'], 'delivery_report')
        self.assertEqual(delv['transport_name'], self.transport_name)
        self.assertEqual(delv['user_message_id'], user_message_id)
        self.assertEqual(delv['delivery_status'],
                         self.expected_delivery_status)

        # Finally the Server delivers a SMS to the Client

        pdu = DeliverSM(555,
                short_message="SMS from server",
                destination_addr="2772222222",
                source_addr="2772000000",
                )
        self.service.factory.smsc.send_pdu(pdu)

        for expected_message in expected_pdus_3:
            actual_message = yield pdu_queue.get()
            self.assert_server_pdu(expected_message, actual_message)

        [mess] = self.get_dispatched_messages()

        self.assertEqual(mess['message_type'], 'user_message')
        self.assertEqual(mess['transport_name'], self.transport_name)
        self.assertEqual(mess['content'], "SMS from server")

        dispatched_failures = self.get_dispatched_failures()
        self.assertEqual(dispatched_failures, [])

    def send_out_of_order_multipart(self, smsc, to_addr, from_addr):
        destination_addr = to_addr
        source_addr = from_addr

        sequence_number = 1
        short_message1 = "\x05\x00\x03\xff\x03\x01back"
        pdu1 = DeliverSM(sequence_number,
                short_message=short_message1,
                destination_addr=destination_addr,
                source_addr=source_addr)

        sequence_number = 2
        short_message2 = "\x05\x00\x03\xff\x03\x02 at"
        pdu2 = DeliverSM(sequence_number,
                short_message=short_message2,
                destination_addr=destination_addr,
                source_addr=source_addr)

        sequence_number = 3
        short_message3 = "\x05\x00\x03\xff\x03\x03 you"
        pdu3 = DeliverSM(sequence_number,
                short_message=short_message3,
                destination_addr=destination_addr,
                source_addr=source_addr)

        smsc.send_pdu(pdu2)
        smsc.send_pdu(pdu3)
        smsc.send_pdu(pdu1)

    @inlineCallbacks
    def test_submit_and_deliver(self):

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind

        # Next the Client submits a SMS to the Server
        # and recieves an ack and a delivery_report

        msg = TransportUserMessage(
                to_addr="2772222222",
                from_addr="2772000000",
                content='hello world',
                transport_name=self.transport_name,
                transport_type='smpp',
                transport_metadata={},
                rkey='%s.outbound' % self.transport_name,
                timestamp='0',
                )
        yield self.dispatch(msg)

        # We need the user_message_id to check the ack
        user_message_id = msg["message_id"]

        [ack, delv] = yield self.wait_for_dispatched_events(2)

        self.assertEqual(ack['message_type'], 'event')
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['transport_name'], self.transport_name)
        self.assertEqual(ack['user_message_id'], user_message_id)

        self.assertEqual(delv['message_type'], 'event')
        self.assertEqual(delv['event_type'], 'delivery_report')
        self.assertEqual(delv['transport_name'], self.transport_name)
        self.assertEqual(delv['user_message_id'], user_message_id)
        self.assertEqual(delv['delivery_status'],
                         self.expected_delivery_status)

        # Finally the Server delivers a SMS to the Client

        pdu = DeliverSM(555,
                short_message="SMS from server",
                destination_addr="2772222222",
                source_addr="2772000000",
                )
        self.service.factory.smsc.send_pdu(pdu)

        # Have the server fire of an out-of-order multipart sms
        self.send_out_of_order_multipart(self.service.factory.smsc,
                                         to_addr="2772222222",
                                         from_addr="2772000000")

        [mess, multipart] = yield self.wait_for_dispatched_messages(2)

        self.assertEqual(mess['message_type'], 'user_message')
        self.assertEqual(mess['transport_name'], self.transport_name)
        self.assertEqual(mess['content'], "SMS from server")

        # Check the incomming multipart is re-assembled correctly
        self.assertEqual(multipart['message_type'], 'user_message')
        self.assertEqual(multipart['transport_name'], self.transport_name)
        self.assertEqual(multipart['content'], "back at you")

        dispatched_failures = self.get_dispatched_failures()
        self.assertEqual(dispatched_failures, [])

    @inlineCallbacks
    def test_submit_and_deliver_ussd_continue(self):

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind
        yield self.clear_link_pdus()

        # Next the Client submits a USSD message to the Server
        # and recieves an ack

        msg = TransportUserMessage(
                to_addr="2772222222",
                from_addr="2772000000",
                content='hello world',
                transport_name=self.transport_name,
                transport_type='ussd',
                transport_metadata={},
                rkey='%s.outbound' % self.transport_name,
                timestamp='0',
                )
        yield self.dispatch(msg)

        # First we make sure the Client binds to the Server
        # and enquire_link pdu's are exchanged as expected
        pdu_queue = self.service.factory.smsc.pdu_queue

        submit_sm_pdu = yield pdu_queue.get()
        self.assert_server_pdu(
            mk_expected_pdu('inbound', 3, 'submit_sm'), submit_sm_pdu)
        pdu_opts = unpacked_pdu_opts(submit_sm_pdu['pdu'])
        self.assertEqual('02', pdu_opts['ussd_service_op'])
        self.assertEqual('0000', pdu_opts['its_session_info'])

        # We need the user_message_id to check the ack
        user_message_id = msg.payload["message_id"]

        [ack, delv] = yield self.wait_for_dispatched_events(2)

        self.assertEqual(ack['message_type'], 'event')
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['transport_name'], self.transport_name)
        self.assertEqual(ack['user_message_id'], user_message_id)

        self.assertEqual(delv['message_type'], 'event')
        self.assertEqual(delv['event_type'], 'delivery_report')
        self.assertEqual(delv['transport_name'], self.transport_name)
        self.assertEqual(delv['user_message_id'], user_message_id)
        self.assertEqual(delv['delivery_status'],
                         self.expected_delivery_status)

        # Finally the Server delivers a USSD message to the Client

        pdu = DeliverSM(555,
                short_message="reply!",
                destination_addr="2772222222",
                source_addr="2772000000",
                )
        pdu._PDU__add_optional_parameter('ussd_service_op', '02')
        pdu._PDU__add_optional_parameter('its_session_info', '0000')
        self.service.factory.smsc.send_pdu(pdu)

        [mess] = yield self.wait_for_dispatched_messages(1)

        self.assertEqual(mess['message_type'], 'user_message')
        self.assertEqual(mess['transport_name'], self.transport_name)
        self.assertEqual(mess['content'], "reply!")
        self.assertEqual(mess['transport_type'], "ussd")
        self.assertEqual(mess['session_event'],
                         TransportUserMessage.SESSION_RESUME)

        self.assertEqual([], self.get_dispatched_failures())

    @inlineCallbacks
    def test_submit_and_deliver_ussd_close(self):

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind
        yield self.clear_link_pdus()

        # Next the Client submits a USSD message to the Server
        # and recieves an ack

        msg = TransportUserMessage(
                to_addr="2772222222",
                from_addr="2772000000",
                content='hello world',
                transport_name=self.transport_name,
                transport_type='ussd',
                transport_metadata={},
                rkey='%s.outbound' % self.transport_name,
                timestamp='0',
                session_event=TransportUserMessage.SESSION_CLOSE,
                )
        yield self.dispatch(msg)

        # First we make sure the Client binds to the Server
        # and enquire_link pdu's are exchanged as expected
        pdu_queue = self.service.factory.smsc.pdu_queue

        submit_sm_pdu = yield pdu_queue.get()
        self.assert_server_pdu(
            mk_expected_pdu('inbound', 3, 'submit_sm'), submit_sm_pdu)
        pdu_opts = unpacked_pdu_opts(submit_sm_pdu['pdu'])
        self.assertEqual('02', pdu_opts['ussd_service_op'])
        self.assertEqual('0001', pdu_opts['its_session_info'])

        # We need the user_message_id to check the ack
        user_message_id = msg.payload["message_id"]

        [ack, delv] = yield self.wait_for_dispatched_events(2)

        self.assertEqual(ack['message_type'], 'event')
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['transport_name'], self.transport_name)
        self.assertEqual(ack['user_message_id'], user_message_id)

        self.assertEqual(delv['message_type'], 'event')
        self.assertEqual(delv['event_type'], 'delivery_report')
        self.assertEqual(delv['transport_name'], self.transport_name)
        self.assertEqual(delv['user_message_id'], user_message_id)
        self.assertEqual(delv['delivery_status'],
                         self.expected_delivery_status)

        # Finally the Server delivers a USSD message to the Client

        pdu = DeliverSM(555,
                short_message="reply!",
                destination_addr="2772222222",
                source_addr="2772000000",
                )
        pdu._PDU__add_optional_parameter('ussd_service_op', '02')
        pdu._PDU__add_optional_parameter('its_session_info', '0001')
        self.service.factory.smsc.send_pdu(pdu)

        [mess] = yield self.wait_for_dispatched_messages(1)

        self.assertEqual(mess['message_type'], 'user_message')
        self.assertEqual(mess['transport_name'], self.transport_name)
        self.assertEqual(mess['content'], "reply!")
        self.assertEqual(mess['transport_type'], "ussd")
        self.assertEqual(mess['session_event'],
                         TransportUserMessage.SESSION_CLOSE)

        self.assertEqual([], self.get_dispatched_failures())

    @inlineCallbacks
    def test_submit_and_deliver_with_missing_id_lookup(self):

        def r_failing_get(third_party_id):
            return succeed(None)
        self.transport.r_get_id_for_third_party_id = r_failing_get

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind

        # Next the Client submits a SMS to the Server
        # and recieves an ack and a delivery_report

        msg = TransportUserMessage(
                to_addr="2772222222",
                from_addr="2772000000",
                content='hello world',
                transport_name=self.transport_name,
                transport_type='smpp',
                transport_metadata={},
                rkey='%s.outbound' % self.transport_name,
                timestamp='0',
                )

        # We need the user_message_id to check the ack
        user_message_id = msg["message_id"]

        lc = LogCatcher(message="Failed to retrieve message id")
        with lc:
            yield self.dispatch(msg)
            [ack] = yield self.wait_for_dispatched_events(1)

        self.assertEqual(ack['message_type'], 'event')
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['transport_name'], self.transport_name)
        self.assertEqual(ack['user_message_id'], user_message_id)

        # check that failure to send delivery report was logged
        [warning] = lc.logs
        self.assertEqual(warning['message'],
                         ("Failed to retrieve message id for delivery "
                          "report. Delivery report from "
                          "esme_testing_transport discarded.",))


class EsmeToSmscTestCaseDeliveryYo(EsmeToSmscTestCase):
    # This tests a slightly non-standard delivery report format for Yo!
    # the following delivery_report_regex is required as a config option
    # "id:(?P<id>\S{,65}) +sub:(?P<sub>.{1,3}) +dlvrd:(?P<dlvrd>.{1,3})"
    # " +submit date:(?P<submit_date>\d*) +done date:(?P<done_date>\d*)"
    # " +stat:(?P<stat>[0-9,A-Z]{1,7}) +err:(?P<err>.{1,3})"
    #" +[Tt]ext:(?P<text>.{,20}).*

    @inlineCallbacks
    def setUp(self):
        yield super(EsmeToSmscTestCase, self).setUp()
        delivery_report_regex = "id:(?P<id>\S{,65})" \
            " +sub:(?P<sub>.{1,3})" \
            " +dlvrd:(?P<dlvrd>.{1,3})" \
            " +submit date:(?P<submit_date>\d*)" \
            " +done date:(?P<done_date>\d*)" \
            " +stat:(?P<stat>[0-9,A-Z]{1,7})" \
            " +err:(?P<err>.{1,3})" \
            " +[Tt]ext:(?P<text>.{,20}).*" \

        self.config = {
            "system_id": "VumiTestSMSC",
            "password": "password",
            "host": "localhost",
            "port": 0,
            "transport_name": self.transport_name,
            "transport_type": "smpp",
            "delivery_report_regex": delivery_report_regex,
            "smsc_delivery_report_string": (
                'id:%s sub:1 dlvrd:1 submit date:%s done date:%s '
                'stat:0 err:0 text:If a general electio'),
        }
        self.service = SmppService(None, config=self.config)
        yield self.service.startWorker()
        self.service.factory.protocol = SmscTestServer
        self.config['port'] = self.service.listening.getHost().port
        self.transport = yield self.get_transport(self.config, start=False)
        self.expected_delivery_status = 'delivered'  # stat:0 means delivered


class TxEsmeToSmscTestCase(TransportTestCase):

    transport_name = "esme_testing_transport"
    transport_class = MockSmppTxTransport

    def assert_pdu_header(self, expected, actual, field):
        self.assertEqual(expected['pdu']['header'][field],
                         actual['pdu']['header'][field])

    def assert_server_pdu(self, expected, actual):
        self.assertEqual(expected['direction'], actual['direction'])
        self.assert_pdu_header(expected, actual, 'sequence_number')
        self.assert_pdu_header(expected, actual, 'command_status')
        self.assert_pdu_header(expected, actual, 'command_id')

    @inlineCallbacks
    def setUp(self):
        yield super(TxEsmeToSmscTestCase, self).setUp()
        self.config = {
            "system_id": "VumiTestSMSC",
            "password": "password",
            "host": "localhost",
            "port": 0,
            "transport_name": self.transport_name,
            "transport_type": "smpp",
        }
        self.service = SmppService(None, config=self.config)
        yield self.service.startWorker()
        self.service.factory.protocol = SmscTestServer
        self.config['port'] = self.service.listening.getHost().port
        self.transport = yield self.get_transport(self.config, start=False)
        self.expected_delivery_status = 'delivered'

    @inlineCallbacks
    def startTransport(self):
        self.transport._block_till_bind = Deferred()
        yield self.transport.startWorker()

    @inlineCallbacks
    def tearDown(self):
        yield super(TxEsmeToSmscTestCase, self).tearDown()
        self.transport.factory.stopTrying()
        self.transport.factory.esme.transport.loseConnection()
        yield self.service.listening.stopListening()
        yield self.service.listening.loseConnection()

    @inlineCallbacks
    def test_submit(self):

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind

        # Next the Client submits a SMS to the Server
        # and recieves an ack

        msg = TransportUserMessage(
                to_addr="2772222222",
                from_addr="2772000000",
                content='hello world',
                transport_name=self.transport_name,
                transport_type='smpp',
                transport_metadata={},
                rkey='%s.outbound' % self.transport_name,
                timestamp='0',
                )
        yield self.dispatch(msg)

        # We need the user_message_id to check the ack
        user_message_id = msg["message_id"]

        [ack] = yield self.wait_for_dispatched_events(1)

        self.assertEqual(ack['message_type'], 'event')
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['transport_name'], self.transport_name)
        self.assertEqual(ack['user_message_id'], user_message_id)

        dispatched_failures = self.get_dispatched_failures()
        self.assertEqual(dispatched_failures, [])


class RxEsmeToSmscTestCase(TransportTestCase):

    transport_name = "esme_testing_transport"
    transport_class = MockSmppRxTransport

    def assert_pdu_header(self, expected, actual, field):
        self.assertEqual(expected['pdu']['header'][field],
                         actual['pdu']['header'][field])

    def assert_server_pdu(self, expected, actual):
        self.assertEqual(expected['direction'], actual['direction'])
        self.assert_pdu_header(expected, actual, 'sequence_number')
        self.assert_pdu_header(expected, actual, 'command_status')
        self.assert_pdu_header(expected, actual, 'command_id')

    @inlineCallbacks
    def setUp(self):
        from twisted.internet.base import DelayedCall
        DelayedCall.debug = True

        yield super(RxEsmeToSmscTestCase, self).setUp()
        self.config = {
            "system_id": "VumiTestSMSC",
            "password": "password",
            "host": "localhost",
            "port": 0,
            "transport_name": self.transport_name,
            "transport_type": "smpp",
        }
        self.service = SmppService(None, config=self.config)
        yield self.service.startWorker()
        self.service.factory.protocol = SmscTestServer
        self.config['port'] = self.service.listening.getHost().port
        self.transport = yield self.get_transport(self.config, start=False)
        self.expected_delivery_status = 'delivered'

    @inlineCallbacks
    def startTransport(self):
        self.transport._block_till_bind = Deferred()
        yield self.transport.startWorker()

    @inlineCallbacks
    def tearDown(self):
        yield super(RxEsmeToSmscTestCase, self).tearDown()
        self.transport.factory.stopTrying()
        self.transport.factory.esme.transport.loseConnection()
        yield self.service.listening.stopListening()
        yield self.service.listening.loseConnection()

    @inlineCallbacks
    def test_deliver(self):

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind
        # The Server delivers a SMS to the Client

        pdu = DeliverSM(555,
                short_message="SMS from server",
                destination_addr="2772222222",
                source_addr="2772000000",
                )
        self.service.factory.smsc.send_pdu(pdu)

        [mess] = yield self.wait_for_dispatched_messages(1)

        self.assertEqual(mess['message_type'], 'user_message')
        self.assertEqual(mess['transport_name'], self.transport_name)
        self.assertEqual(mess['content'], "SMS from server")

        dispatched_failures = self.get_dispatched_failures()
        self.assertEqual(dispatched_failures, [])

    @inlineCallbacks
    def test_deliver_bad_encoding(self):

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind
        # The Server delivers a SMS to the Client

        bad_pdu = DeliverSM(555,
                short_message="SMS from server containing \xa7",
                destination_addr="2772222222",
                source_addr="2772000000",
                )

        good_pdu = DeliverSM(555,
                short_message="Next message",
                destination_addr="2772222222",
                source_addr="2772000000",
                )

        self.service.factory.smsc.send_pdu(bad_pdu)
        self.service.factory.smsc.send_pdu(good_pdu)
        [mess] = yield self.wait_for_dispatched_messages(1)

        self.assertEqual(mess['message_type'], 'user_message')
        self.assertEqual(mess['transport_name'], self.transport_name)
        self.assertEqual(mess['content'], "Next message")

        dispatched_failures = self.get_dispatched_failures()
        self.assertEqual(dispatched_failures, [])

        [failure] = self.flushLoggedErrors(UnicodeDecodeError)
        message = failure.getErrorMessage()
        codec, rest = message.split(' ', 1)
        self.assertTrue(codec in ("'utf8'", "'utf-8'"))
        self.assertTrue(rest.startswith(
                "codec can't decode byte 0xa7 in position 27"))

    @inlineCallbacks
    def test_deliver_ussd_start(self):

        self._block_till_bind = Deferred()

        # Startup
        yield self.startTransport()
        yield self.transport._block_till_bind
        # The Server delivers a SMS to the Client

        pdu = DeliverSM(
            555, destination_addr="2772222222", source_addr="2772000000")
        pdu._PDU__add_optional_parameter('ussd_service_op', '01')
        pdu._PDU__add_optional_parameter('its_session_info', '0000')
        self.service.factory.smsc.send_pdu(pdu)

        [mess] = yield self.wait_for_dispatched_messages(1)

        self.assertEqual(mess['transport_type'], 'ussd')
        self.assertEqual(mess['transport_name'], self.transport_name)
        self.assertEqual(mess['content'], None)
        self.assertEqual(mess['session_event'],
                         TransportUserMessage.SESSION_NEW)

        dispatched_failures = self.get_dispatched_failures()
        self.assertEqual(dispatched_failures, [])

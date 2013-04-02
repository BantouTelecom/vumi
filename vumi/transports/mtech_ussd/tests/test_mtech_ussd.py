from twisted.internet.defer import inlineCallbacks
from twisted.trial.unittest import TestCase

from vumi.transports.tests.utils import TransportTestCase
from vumi.utils import http_request_full
from vumi.message import TransportUserMessage
from vumi.transports.mtech_ussd import MtechUssdTransport
from vumi.transports.mtech_ussd.mtech_ussd import MtechUssdResponse


class TestMtechUssdTransport(TransportTestCase):

    timeout = 1
    transport_name = 'mtech_ussd'
    transport_class = MtechUssdTransport

    @inlineCallbacks
    def setUp(self):
        yield super(TestMtechUssdTransport, self).setUp()
        self.config = {
            'transport_type': 'ussd',
            'ussd_string_prefix': '*120*666#',
            'web_path': "/foo",
            'web_host': "localhost",
            'web_port': 0,
            'username': 'testuser',
            'password': 'testpass',
        }
        self.transport = yield self.get_transport(self.config)
        self.transport_url = self.transport.get_transport_url().rstrip('/')
        self.url = "%s%s" % (self.transport_url, self.config['web_path'])
        yield self.transport.session_manager.redis._purge_all()  # just in case

    def make_ussd_request_full(self, session_id, **kwargs):
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<page version="2.0">',
            '  <session_id>%s</session_id>' % (session_id,),
            ]
        for k, v in kwargs.items():
            lines.append('  <%s>%s</%s>' % (k, v, k))
        lines.append('</page>')
        data = '\n'.join(lines)

        return http_request_full(self.url, data, method='POST')

    def make_ussd_request(self, session_id, **kwargs):
        return self.make_ussd_request_full(session_id, **kwargs).addCallback(
            lambda r: r.delivered_body)

    def reply_to_message(self, *args, **kw):
        d = self.wait_for_dispatched_messages(1)

        def reply(r):
            msg = TransportUserMessage(**r[0].payload)
            self.dispatch(msg.reply(*args, **kw))
            return msg

        return d.addCallback(reply)

    @inlineCallbacks
    def test_empty_request(self):
        response = yield http_request_full(self.url, "", method='POST')
        self.assertEqual(response.code, 400)

    @inlineCallbacks
    def test_bad_request(self):
        response = yield http_request_full(self.url, "blah", method='POST')
        self.assertEqual(response.code, 400)

    @inlineCallbacks
    def test_inbound_new_continue(self):
        sid = 'a41739890287485d968ea66e8b44bfd3'
        response_d = self.make_ussd_request(
            sid, mobile_number='2348085832481', page_id='0',
            data='testmenu', gate='gateid')

        msg = yield self.reply_to_message("OK\n1 < 2")

        self.assertEqual(msg['transport_name'], self.transport_name)
        self.assertEqual(msg['transport_type'], "ussd")
        self.assertEqual(msg['transport_metadata'], {"session_id": sid})
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_NEW)
        self.assertEqual(msg['from_addr'], '2348085832481')
        # self.assertEqual(msg['to_addr'], '*120*666#')
        self.assertEqual(msg['content'], 'testmenu')

        response = yield response_d
        correct_response = ''.join([
                "<?xml version='1.0' encoding='UTF-8'?>",
                '<page version="2.0">',
                '<session_id>a41739890287485d968ea66e8b44bfd3</session_id>',
                '<div>OK<br />1 &lt; 2</div>',
                '<navigation>',
                '<link accesskey="*" pageId="indexX" />',
                '</navigation>',
                '</page>',
                ])
        self.assertEqual(response, correct_response)

    @inlineCallbacks
    def test_inbound_resume_continue(self):
        sid = 'a41739890287485d968ea66e8b44bfd3'
        yield self.transport.save_session(sid, '2348085832481', '*120*666#')
        response_d = self.make_ussd_request(sid, page_id="indexX", data="foo")

        msg = yield self.reply_to_message("OK")

        self.assertEqual(msg['transport_name'], self.transport_name)
        self.assertEqual(msg['transport_type'], "ussd")
        self.assertEqual(msg['transport_metadata'], {"session_id": sid})
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_RESUME)
        self.assertEqual(msg['from_addr'], '2348085832481')
        self.assertEqual(msg['to_addr'], '*120*666#')
        self.assertEqual(msg['content'], 'foo')

        response = yield response_d
        correct_response = ''.join([
                "<?xml version='1.0' encoding='UTF-8'?>",
                '<page version="2.0">',
                '<session_id>a41739890287485d968ea66e8b44bfd3</session_id>',
                '<div>OK</div>',
                '<navigation>',
                '<link accesskey="*" pageId="indexX" />',
                '</navigation>',
                '</page>',
                ])
        self.assertEqual(response, correct_response)

    @inlineCallbacks
    def test_nack(self):
        msg = self.mkmsg_out()
        yield self.dispatch(msg)
        [nack] = yield self.wait_for_dispatched_events(1)
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['sent_message_id'], msg['message_id'])
        self.assertEqual(nack['nack_reason'],
            'Missing in_reply_to, content or session_id')

    @inlineCallbacks
    def test_inbound_missing_session(self):
        sid = 'a41739890287485d968ea66e8b44bfd3'
        response = yield self.make_ussd_request_full(
            sid, page_id="indexX", data="foo")
        self.assertEqual(400, response.code)
        self.assertEqual('', response.delivered_body)

    @inlineCallbacks
    def test_inbound_new_and_resume(self):
        sid = 'a41739890287485d968ea66e8b44bfd3'
        response_d = self.make_ussd_request(
            sid, mobile_number='2348085832481', page_id='0',
            data='testmenu', gate='gateid')

        msg = yield self.reply_to_message("OK\n1 < 2")

        self.assertEqual(msg['transport_name'], self.transport_name)
        self.assertEqual(msg['transport_type'], "ussd")
        self.assertEqual(msg['transport_metadata'], {"session_id": sid})
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_NEW)
        self.assertEqual(msg['from_addr'], '2348085832481')
        # self.assertEqual(msg['to_addr'], '*120*666#')
        self.assertEqual(msg['content'], 'testmenu')

        response = yield response_d
        correct_response = ''.join([
                "<?xml version='1.0' encoding='UTF-8'?>",
                '<page version="2.0">',
                '<session_id>a41739890287485d968ea66e8b44bfd3</session_id>',
                '<div>OK<br />1 &lt; 2</div>',
                '<navigation>',
                '<link accesskey="*" pageId="indexX" />',
                '</navigation>',
                '</page>',
                ])
        self.assertEqual(response, correct_response)

        self.clear_all_dispatched()

        response_d = self.make_ussd_request(sid, page_id="indexX", data="foo")

        msg = yield self.reply_to_message("OK")

        self.assertEqual(msg['transport_name'], self.transport_name)
        self.assertEqual(msg['transport_type'], "ussd")
        self.assertEqual(msg['transport_metadata'], {"session_id": sid})
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_RESUME)
        self.assertEqual(msg['from_addr'], '2348085832481')
        self.assertEqual(msg['to_addr'], 'gateid')
        self.assertEqual(msg['content'], 'foo')

        response = yield response_d
        correct_response = ''.join([
                "<?xml version='1.0' encoding='UTF-8'?>",
                '<page version="2.0">',
                '<session_id>a41739890287485d968ea66e8b44bfd3</session_id>',
                '<div>OK</div>',
                '<navigation>',
                '<link accesskey="*" pageId="indexX" />',
                '</navigation>',
                '</page>',
                ])
        self.assertEqual(response, correct_response)

    @inlineCallbacks
    def test_inbound_resume_close(self):
        sid = 'a41739890287485d968ea66e8b44bfd3'
        yield self.transport.save_session(sid, '2348085832481', '*120*666#')
        response_d = self.make_ussd_request(sid, page_id="indexX", data="foo")

        msg = yield self.reply_to_message("OK", False)

        self.assertEqual(msg['transport_name'], self.transport_name)
        self.assertEqual(msg['transport_type'], "ussd")
        self.assertEqual(msg['transport_metadata'], {"session_id": sid})
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_RESUME)
        self.assertEqual(msg['from_addr'], '2348085832481')
        self.assertEqual(msg['to_addr'], '*120*666#')
        self.assertEqual(msg['content'], 'foo')

        response = yield response_d
        correct_response = ''.join([
                "<?xml version='1.0' encoding='UTF-8'?>",
                '<page version="2.0">',
                '<session_id>a41739890287485d968ea66e8b44bfd3</session_id>',
                '<div>OK</div>',
                '</page>',
                ])
        self.assertEqual(response, correct_response)

    @inlineCallbacks
    def test_inbound_cancel(self):
        sid = 'a41739890287485d968ea66e8b44bfd3'
        yield self.transport.save_session(sid, '2348085832481', '*120*666#')
        response = yield self.make_ussd_request(sid, status="1")

        correct_response = ''.join([
                "<?xml version='1.0' encoding='UTF-8'?>",
                '<page version="2.0">',
                '<session_id>a41739890287485d968ea66e8b44bfd3</session_id>',
                '</page>',
                ])
        self.assertEqual(response, correct_response)


class TestMtechUssdResponse(TestCase):
    def setUp(self):
        self.mur = MtechUssdResponse("sid123")

    def assert_message_xml(self, *lines):
        xml_str = ''.join(
            ["<?xml version='1.0' encoding='UTF-8'?>"] + list(lines))
        self.assertEqual(self.mur.to_xml(), xml_str)

    def test_empty_response(self):
        self.assert_message_xml(
            '<page version="2.0">',
            '<session_id>sid123</session_id>',
            '</page>')

    def test_free_text(self):
        self.mur.add_text("Please enter your name")
        self.mur.add_freetext_option()
        self.assert_message_xml(
            '<page version="2.0">',
            '<session_id>sid123</session_id>',
            '<div>Please enter your name</div>',
            '<navigation><link accesskey="*" pageId="indexX" /></navigation>',
            '</page>')

    def test_menu_options(self):
        self.mur.add_text("Please choose:")
        self.mur.add_menu_item('chicken', '1')
        self.mur.add_menu_item('beef', '2')
        self.assert_message_xml(
            '<page version="2.0">',
            '<session_id>sid123</session_id>',
            '<div>Please choose:</div>',
            '<navigation>',
            '<link accesskey="1" pageId="index1">chicken</link>',
            '<link accesskey="2" pageId="index2">beef</link>',
            '</navigation>',
            '</page>')

    def test_menu_options_title(self):
        self.mur.add_title("LUNCH")
        self.mur.add_text("Please choose:")
        self.mur.add_menu_item('chicken', '1')
        self.mur.add_menu_item('beef', '2')
        self.assert_message_xml(
            '<page version="2.0">',
            '<session_id>sid123</session_id>',
            '<title>LUNCH</title>',
            '<div>Please choose:</div>',
            '<navigation>',
            '<link accesskey="1" pageId="index1">chicken</link>',
            '<link accesskey="2" pageId="index2">beef</link>',
            '</navigation>',
            '</page>')

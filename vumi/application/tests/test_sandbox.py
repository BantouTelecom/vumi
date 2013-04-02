"""Tests for vumi.application.sandbox."""

import os
import sys
import json
import pkg_resources
from collections import defaultdict

from twisted.internet.defer import inlineCallbacks, fail, succeed
from twisted.internet.error import ProcessTerminated
from twisted.trial.unittest import TestCase, SkipTest

from vumi.message import TransportUserMessage, TransportEvent
from vumi.application.tests.utils import ApplicationTestCase
from vumi.application.sandbox import (
    Sandbox, SandboxCommand, SandboxError, RedisResource, OutboundResource,
    JsSandboxResource, LoggingResource, HttpClientResource, JsSandbox)
from vumi.tests.utils import LogCatcher, PersistenceMixin


class SandboxTestCaseBase(ApplicationTestCase):

    application_class = Sandbox

    def setup_app(self, executable=None, args=None, extra_config=None):
        tmp_path = self.mktemp()
        os.mkdir(tmp_path)
        config = {
            'path': tmp_path,
            'timeout': '10',
        }
        if executable is not None:
            config['executable'] = executable
        if args is not None:
            config['args'] = args
        if extra_config is not None:
            config.update(extra_config)
        return self.get_application(config)

    def mk_ack(self, **kw):
        msg_kw = {
            'event_type': 'ack', 'user_message_id': '1',
            'sent_message_id': '1', 'sandbox_id': 'sandbox1',
        }
        msg_kw.update(kw)
        return TransportEvent(**msg_kw)

    def mk_nack(self, **kw):
        msg_kw = {
            'event_type': 'nack', 'user_message_id': '1',
            'sandbox_id': 'sandbox1', 'nack_reason': 'unknown',
        }
        msg_kw.update(kw)
        return TransportEvent(**msg_kw)

    def mk_delivery_report(self, **kw):
        msg_kw = {
            'event_type': 'delivery_report', 'user_message_id': '1',
            'sent_message_id': '1', 'sandbox_id': 'sandbox1',
            'delivery_status': 'delivered',
        }
        msg_kw.update(kw)
        return TransportEvent(**msg_kw)

    def mk_msg(self, **kw):
        msg_kw = {
            'to_addr': "1", 'from_addr': "2",
            'transport_name': "test", 'transport_type': "sphex",
            'sandbox_id': 'sandbox1',
        }
        msg_kw.update(kw)
        return TransportUserMessage(**msg_kw)


class SandboxTestCase(SandboxTestCaseBase):

    def setup_app(self, python_code, extra_config=None):
        return super(SandboxTestCase, self).setup_app(
            sys.executable, ['-c', python_code],
            extra_config=extra_config)

    @inlineCallbacks
    def test_bad_command_from_sandbox(self):
        app = yield self.setup_app(
            "import sys, time\n"
            "sys.stdout.write('{}\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n"
        )
        status = yield app.process_event_in_sandbox(self.mk_ack())
        [sandbox_err] = self.flushLoggedErrors(SandboxError)
        self.assertEqual(str(sandbox_err.value).split(' [')[0],
                         "Resource fallback: unknown command 'unknown'"
                         " received from sandbox 'sandbox1'")
        self.assertEqual(status, None)
        [kill_err] = self.flushLoggedErrors(ProcessTerminated)
        self.assertTrue('process ended by signal' in str(kill_err.value))

    @inlineCallbacks
    def test_stderr_from_sandbox(self):
        app = yield self.setup_app(
            "import sys\n"
            "sys.stderr.write('err\\n')\n"
        )
        status = yield app.process_event_in_sandbox(self.mk_ack())
        self.assertEqual(status, 0)
        [sandbox_err] = self.flushLoggedErrors(SandboxError)
        self.assertEqual(str(sandbox_err.value).split(' [')[0], "err")

    @inlineCallbacks
    def test_resource_setup(self):
        r_server = yield self.get_redis_manager()
        json_data = SandboxCommand(cmd='db.set', key='foo',
                                   value={'a': 1, 'b': 2}).to_json()
        app = yield self.setup_app(
            "import sys\n"
            "sys.stdout.write(%r)\n" % json_data,
            {'sandbox': {
                'db': {
                    'cls': 'vumi.application.sandbox.RedisResource',
                    'redis_manager': {
                        'FAKE_REDIS': r_server,
                        'key_prefix': r_server._key_prefix,
                    },
                },
            }})
        status = yield app.process_event_in_sandbox(self.mk_ack())
        self.assertEqual(status, 0)
        self.assertEqual(sorted((yield r_server.keys())),
                         ['count#sandbox1',
                          'sandboxes#sandbox1#foo'])
        self.assertEqual((yield r_server.get('count#sandbox1')), '1')
        self.assertEqual((yield r_server.get('sandboxes#sandbox1#foo')),
                         json.dumps({'a': 1, 'b': 2}))

    @inlineCallbacks
    def test_outbound_reply_from_sandbox(self):
        msg = self.mk_msg()
        json_data = SandboxCommand(cmd='outbound.reply_to',
                                   content='Hooray!',
                                   in_reply_to=msg['message_id']).to_json()
        app = yield self.setup_app(
            "import sys\n"
            "sys.stdout.write(%r)\n" % json_data,
            {'sandbox': {
                'outbound': {
                    'cls': 'vumi.application.sandbox.OutboundResource',
                },
            }})
        status = yield app.process_message_in_sandbox(msg)
        self.assertEqual(status, 0)
        [reply] = self.get_dispatched_messages()
        self.assertEqual(reply['content'], "Hooray!")
        self.assertEqual(reply['session_event'], None)

    @inlineCallbacks
    def test_recv_limit(self):
        recv_limit = 1000
        app = yield self.setup_app(
            "import sys, time\n"
            "sys.stderr.write(%r)\n"
            "sys.stdout.write('\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n"
            % ("a" * (recv_limit - 1) + "\n"),
            {'recv_limit': str(recv_limit)})
        status = yield app.process_message_in_sandbox(self.mk_msg())
        self.assertEqual(status, None)
        [stderr_err] = self.flushLoggedErrors(SandboxError)
        [kill_err] = self.flushLoggedErrors(ProcessTerminated)
        self.assertTrue('process ended by signal' in str(kill_err.value))

    @inlineCallbacks
    def test_env_variable(self):
        app = yield self.setup_app(
            "import sys, os, json\n"
            "test_value = os.environ['TEST_VAR']\n"
            "log = {'cmd': 'log.info', 'cmd_id': '1',\n"
            "       'reply': False, 'msg': test_value}\n"
            "sys.stdout.write(json.dumps(log) + '\\n')\n",
            {'env': {'TEST_VAR': 'success'},
             'sandbox': {
                 'log': {'cls': 'vumi.application.sandbox.LoggingResource'},
             }},
        )
        with LogCatcher() as lc:
            status = yield app.process_message_in_sandbox(self.mk_msg())
            [value_str] = lc.messages()
        self.assertEqual(status, 0)
        self.assertEqual(value_str, "success")

    @inlineCallbacks
    def test_python_path_set(self):
        app = yield self.setup_app(
            "import sys, json\n"
            "path = ':'.join(sys.path)\n"
            "log = {'cmd': 'log.info', 'cmd_id': '1',\n"
            "       'reply': False, 'msg': path}\n"
            "sys.stdout.write(json.dumps(log) + '\\n')\n",
            {'env': {'PYTHONPATH': '/pp1:/pp2'},
             'sandbox': {
                'log': {'cls': 'vumi.application.sandbox.LoggingResource'},
            }},
        )
        with LogCatcher() as lc:
            status = yield app.process_message_in_sandbox(self.mk_msg())
            [path_str] = lc.messages()
        self.assertEqual(status, 0)
        path = path_str.split(':')
        self.assertTrue('/pp1' in path)
        self.assertTrue('/pp2' in path)

    @inlineCallbacks
    def test_python_path_unset(self):
        app = yield self.setup_app(
            "import sys, json\n"
            "path = ':'.join(sys.path)\n"
            "log = {'cmd': 'log.info', 'cmd_id': '1',\n"
            "       'reply': False, 'msg': path}\n"
            "sys.stdout.write(json.dumps(log) + '\\n')\n",
            {'env': {},
             'sandbox': {
                'log': {'cls': 'vumi.application.sandbox.LoggingResource'},
            }},
        )
        with LogCatcher() as lc:
            status = yield app.process_message_in_sandbox(self.mk_msg())
            [path_str] = lc.messages()
        self.assertEqual(status, 0)
        path = path_str.split(':')
        self.assertTrue('/pp1' not in path)
        self.assertTrue('/pp2' not in path)

    @inlineCallbacks
    def echo_check(self, handler_name, msg, expected_cmd):
        app = yield self.setup_app(
            "import sys, json\n"
            "cmd = sys.stdin.readline()\n"
            "log = {'cmd': 'log.info', 'cmd_id': '1',\n"
            "       'reply': False, 'msg': cmd}\n"
            "sys.stdout.write(json.dumps(log) + '\\n')\n",
            {'sandbox': {
                'log': {'cls': 'vumi.application.sandbox.LoggingResource'},
            }},
        )
        with LogCatcher() as lc:
            status = yield getattr(app, handler_name)(msg)
            [cmd_json] = lc.messages()

        self.assertEqual(status, 0)
        echoed_cmd = json.loads(cmd_json)
        self.assertEqual(echoed_cmd['cmd'], expected_cmd)
        echoed_cmd['msg']['timestamp'] = msg['timestamp']
        self.assertEqual(echoed_cmd['msg'], msg.payload)

    def test_consume_user_message(self):
        return self.echo_check('consume_user_message', self.mk_msg(),
                               'inbound-message')

    def test_close_session(self):
        return self.echo_check('close_session', self.mk_msg(),
                               'inbound-message')

    def test_consume_ack(self):
        return self.echo_check('consume_ack', self.mk_ack(),
                               'inbound-event')

    def test_consume_nack(self):
        return self.echo_check('consume_nack', self.mk_nack(),
                               'inbound-event')

    def test_consume_delivery_report(self):
        return self.echo_check('consume_delivery_report',
            self.mk_delivery_report(), 'inbound-event')


class JsSandboxTestCase(SandboxTestCaseBase):

    application_class = JsSandbox

    def setUp(self):
        super(JsSandboxTestCase, self).setUp()
        if JsSandbox.find_nodejs() is None:
            raise SkipTest("No node.js executable found.")

    def setup_app(self, javascript_code, extra_config=None):
        extra_config = extra_config or {}
        extra_config.update({
            'javascript': javascript_code,
        })
        return super(JsSandboxTestCase, self).setup_app(
            extra_config=extra_config)

    @inlineCallbacks
    def test_js_sandboxer(self):
        app_js = pkg_resources.resource_filename('vumi.application.tests',
                                                 'app.js')
        javascript = file(app_js).read()
        app = yield self.setup_app(javascript)

        with LogCatcher() as lc:
            status = yield app.process_message_in_sandbox(self.mk_msg())
            failures = [log['failure'].value for log in lc.errors]
            msgs = lc.messages()
        self.assertEqual(failures, [])
        self.assertEqual(status, 0)
        self.assertEqual(msgs, [
            'Starting sandbox ...',
            'Loading sandboxed code ...',
            'From init!',
            'From command: inbound-message',
            'Log successful: true',
            'Done.',
        ])

    @inlineCallbacks
    def test_js_sandboxer_with_app_context(self):
        app_js = pkg_resources.resource_filename('vumi.application.tests',
                                                 'app_requires_path.js')
        javascript = file(app_js).read()
        app = yield self.setup_app(javascript, extra_config={
            "app_context": "{path: require('path')}",
        })

        with LogCatcher() as lc:
            status = yield app.process_message_in_sandbox(self.mk_msg())
            failures = [log['failure'].value for log in lc.errors]
            msgs = lc.messages()
        self.assertEqual(failures, [])
        self.assertEqual(status, 0)
        self.assertEqual(msgs, [
            'Starting sandbox ...',
            'Loading sandboxed code ...',
            'From init!',
            'We have access to path!',
            'Done.',
        ])


class DummyAppWorker(object):

    class DummyApi(object):
        def __init__(self):
            pass

        def set_sandbox(self, sandbox):
            self.sandbox = sandbox
            self.sandbox_id = sandbox.sandbox_id

    class DummyProtocol(object):
        def __init__(self, sandbox_id, api):
            self.sandbox_id = sandbox_id
            self.api = api
            api.set_sandbox(self)

    sandbox_api_cls = DummyApi
    sandbox_protocol_cls = DummyProtocol

    def __init__(self):
        self.mock_calls = defaultdict(list)

    def create_sandbox_api(self):
        return self.sandbox_api_cls()

    def create_sandbox_protocol(self, sandbox_id, api):
        return self.sandbox_protocol_cls(sandbox_id, api)

    def __getattr__(self, name):
        def mock_method(*args, **kw):
            self.mock_calls[name].append((args, kw))
        return mock_method


class ResourceTestCaseBase(TestCase):

    app_worker_cls = DummyAppWorker
    resource_cls = None
    resource_name = 'test_resource'
    sandbox_id = 'test_id'

    def setUp(self):
        self.app_worker = self.app_worker_cls()
        self.resource = None
        self.api = self.app_worker.create_sandbox_api()
        self.sandbox = self.app_worker.create_sandbox_protocol(self.sandbox_id,
                                                               self.api)

    @inlineCallbacks
    def tearDown(self):
        if self.resource is not None:
            yield self.resource.teardown()

    @inlineCallbacks
    def create_resource(self, config):
        resource = self.resource_cls(self.resource_name,
                                     self.app_worker,
                                     config)
        yield resource.setup()
        self.resource = resource

    def dispatch_command(self, cmd, **kwargs):
        if self.resource is None:
            raise ValueError("Create a resource before"
                             " calling dispatch_command")
        msg = SandboxCommand(cmd=cmd, **kwargs)
        # round-trip message to get something more similar
        # to what would be returned by a real sandbox when
        # msgs are loaded from JSON.
        msg = SandboxCommand.from_json(msg.to_json())
        return self.resource.dispatch_request(self.api, msg)


class TestRedisResource(ResourceTestCaseBase, PersistenceMixin):

    resource_cls = RedisResource

    @inlineCallbacks
    def setUp(self):
        super(TestRedisResource, self).setUp()
        yield self._persist_setUp()
        self.r_server = yield self.get_redis_manager()
        yield self.create_resource({
            'redis_manager': {
                'FAKE_REDIS': self.r_server,
                'key_prefix': self.r_server._key_prefix,
            }})

    @inlineCallbacks
    def tearDown(self):
        yield super(TestRedisResource, self).tearDown()
        yield self._persist_tearDown()

    def check_reply(self, reply, success=True, **kw):
        self.assertEqual(reply['success'], success)
        for key, expected_value in kw.iteritems():
            self.assertEqual(reply[key], expected_value)

    @inlineCallbacks
    def create_metric(self, metric, value, total_count=1):
        metric_key = 'sandboxes#test_id#' + metric
        count_key = 'count#test_id'
        yield self.r_server.set(metric_key, value)
        yield self.r_server.set(count_key, total_count)

    @inlineCallbacks
    def check_metric(self, metric, value, total_count):
        metric_key = 'sandboxes#test_id#' + metric
        count_key = 'count#test_id'
        self.assertEqual((yield self.r_server.get(metric_key)), value)
        self.assertEqual((yield self.r_server.get(count_key)),
                         str(total_count))

    @inlineCallbacks
    def test_handle_set(self):
        reply = yield self.dispatch_command('set', key='foo', value='bar')
        self.check_reply(reply, success=True)
        yield self.check_metric('foo', json.dumps('bar'), 1)

    @inlineCallbacks
    def test_handle_set_too_many(self):
        yield self.create_metric('foo', 'a', total_count=100)
        reply = yield self.dispatch_command('set', key='bar', value='bar')
        self.check_reply(reply, success=False, reason='Too many keys')
        yield self.check_metric('bar', None, 100)

    @inlineCallbacks
    def test_handle_get(self):
        yield self.create_metric('foo', json.dumps('bar'))
        reply = yield self.dispatch_command('get', key='foo')
        self.check_reply(reply, success=True, value='bar')

    @inlineCallbacks
    def test_handle_get_for_unknown_key(self):
        reply = yield self.dispatch_command('get', key='foo')
        self.check_reply(reply, success=True, value=None)

    @inlineCallbacks
    def test_handle_delete(self):
        self.create_metric('foo', json.dumps('bar'))
        yield self.r_server.set('count#test_id', '1')
        reply = yield self.dispatch_command('delete', key='foo')
        self.check_reply(reply, success=True, existed=True)
        yield self.check_metric('foo', None, 0)

    @inlineCallbacks
    def test_handle_incr_default_amount(self):
        reply = yield self.dispatch_command('incr', key='foo')
        self.check_reply(reply, success=True, value=1)
        yield self.check_metric('foo', '1', 1)

    @inlineCallbacks
    def test_handle_incr_create(self):
        reply = yield self.dispatch_command('incr', key='foo', amount=2)
        self.check_reply(reply, success=True, value=2)
        yield self.check_metric('foo', '2', 1)

    @inlineCallbacks
    def test_handle_incr_existing(self):
        self.create_metric('foo', '2')
        reply = yield self.dispatch_command('incr', key='foo', amount=2)
        self.check_reply(reply, success=True, value=4)
        yield self.check_metric('foo', '4', 1)

    @inlineCallbacks
    def test_handle_incr_existing_non_int(self):
        self.create_metric('foo', 'a')
        reply = yield self.dispatch_command('incr', key='foo', amount=2)
        self.check_reply(reply, success=False)
        self.assertTrue(reply['reason'])
        yield self.check_metric('foo', 'a', 1)

    @inlineCallbacks
    def test_handle_incr_too_many_keys(self):
        yield self.create_metric('foo', 'a', total_count=100)
        reply = yield self.dispatch_command('incr', key='bar', amount=2)
        self.check_reply(reply, success=False, reason='Too many keys')
        yield self.check_metric('bar', None, 100)


class TestOutboundResource(ResourceTestCaseBase):

    resource_cls = OutboundResource

    @inlineCallbacks
    def setUp(self):
        super(TestOutboundResource, self).setUp()
        yield self.create_resource({})

    @inlineCallbacks
    def test_handle_reply_to(self):
        self.api.get_inbound_message = lambda msg_id: msg_id
        reply = yield self.dispatch_command('reply_to', content='hello',
                                            continue_session=True,
                                            in_reply_to='msg1')
        self.assertEqual(reply, None)
        self.assertEqual(self.app_worker.mock_calls['reply_to'],
                         [(('msg1', 'hello'), {'continue_session': True})])

    @inlineCallbacks
    def test_handle_reply_to_group(self):
        self.api.get_inbound_message = lambda msg_id: msg_id
        reply = yield self.dispatch_command('reply_to_group', content='hello',
                                            continue_session=True,
                                            in_reply_to='msg1')
        self.assertEqual(reply, None)
        self.assertEqual(self.app_worker.mock_calls['reply_to_group'],
                         [(('msg1', 'hello'), {'continue_session': True})])

    @inlineCallbacks
    def test_handle_send_to(self):
        reply = yield self.dispatch_command('send_to', content='hello',
                                            to_addr='1234',
                                            tag='default')
        self.assertEqual(reply, None)
        self.assertEqual(self.app_worker.mock_calls['send_to'],
                         [(('1234', 'hello'), {'tag': 'default'})])


class JsDummyAppWorker(DummyAppWorker):
    def javascript_for_api(self, api):
        return 'testscript'

    def app_context_for_api(self, api):
        return 'appcontext'


class TestJsSandboxResource(ResourceTestCaseBase):

    resource_cls = JsSandboxResource

    app_worker_cls = JsDummyAppWorker

    @inlineCallbacks
    def setUp(self):
        super(TestJsSandboxResource, self).setUp()
        yield self.create_resource({})

    def test_sandbox_init(self):
        msgs = []
        self.api.sandbox_send = lambda msg: msgs.append(msg)
        self.resource.sandbox_init(self.api)
        self.assertEqual(msgs, [SandboxCommand(cmd='initialize',
                                               cmd_id=msgs[0]['cmd_id'],
                                               javascript='testscript',
                                               app_context='appcontext')])


class TestLoggingResource(ResourceTestCaseBase):

    resource_cls = LoggingResource

    @inlineCallbacks
    def setUp(self):
        super(TestLoggingResource, self).setUp()
        yield self.create_resource({})

    @inlineCallbacks
    def test_handle_info(self):
        with LogCatcher() as lc:
            reply = yield self.dispatch_command('info', msg='foo')
            msgs = lc.messages()
        self.assertEqual(reply['success'], True)
        self.assertEqual(msgs, ['foo'])


class TestHttpClientResource(ResourceTestCaseBase):

    resource_cls = HttpClientResource

    class DummyResponse(object):
        pass

    @inlineCallbacks
    def setUp(self):
        super(TestHttpClientResource, self).setUp()
        yield self.create_resource({})
        import vumi.application.sandbox
        self.patch(vumi.application.sandbox,
                   'http_request_full', self.dummy_http_request)
        self._next_http_request_result = None
        self._http_requests = []

    def dummy_http_request(self, *args, **kw):
        self._http_requests.append((args, kw))
        return self._next_http_request_result

    def http_request_fail(self, error):
        self._next_http_request_result = fail(error)

    def http_request_succeed(self, body, code=200):
        response = self.DummyResponse()
        response.delivered_body = body
        response.code = code
        self._next_http_request_result = succeed(response)

    def assert_not_unicode(self, arg):
        self.assertFalse(isinstance(arg, unicode))

    def assert_http_request(self, url, method='GET', headers={}, data=None,
                            timeout=None, data_limit=None):
        timeout = (timeout if timeout is not None
                   else self.resource.timeout)
        data_limit = (data_limit if data_limit is not None
                      else self.resource.data_limit)
        args = (url,)
        kw = dict(method=method, headers=headers, data=data,
                  timeout=timeout, data_limit=data_limit)
        [(actual_args, actual_kw)] = self._http_requests
        self.assertEqual((actual_args, actual_kw), (args, kw))

        self.assert_not_unicode(actual_args[0])
        self.assert_not_unicode(actual_kw.get('data'))
        for key, values in actual_kw.get('headers', {}).items():
            self.assert_not_unicode(key)
            for value in values:
                self.assert_not_unicode(value)

    @inlineCallbacks
    def test_handle_get(self):
        self.http_request_succeed("foo")
        reply = yield self.dispatch_command('get',
                                            url='http://www.example.com')
        self.assertTrue(reply['success'])
        self.assertEqual(reply['body'], "foo")
        self.assert_http_request('http://www.example.com', method='GET')

    @inlineCallbacks
    def test_handle_post(self):
        self.http_request_succeed("foo")
        reply = yield self.dispatch_command('post',
                                            url='http://www.example.com')
        self.assertTrue(reply['success'])
        self.assertEqual(reply['body'], "foo")
        self.assert_http_request('http://www.example.com', method='POST')

    @inlineCallbacks
    def test_failed_get(self):
        self.http_request_fail(ValueError("HTTP request failed"))
        reply = yield self.dispatch_command('get',
                                            url='http://www.example.com')
        self.assertFalse(reply['success'])
        self.assertEqual(reply['reason'], "HTTP request failed")
        self.assert_http_request('http://www.example.com', method='GET')

    @inlineCallbacks
    def test_null_url(self):
        reply = yield self.dispatch_command('get')
        self.assertFalse(reply['success'])
        self.assertEqual(reply['reason'], "No URL given")

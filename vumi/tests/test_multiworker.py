from twisted.internet.defer import (Deferred, DeferredList, inlineCallbacks,
                                    returnValue)

from vumi.tests.utils import StubbedWorkerCreator, VumiWorkerTestCase
from vumi.service import Worker
from vumi.message import TransportUserMessage
from vumi.multiworker import MultiWorker


class ToyWorker(Worker):
    events = []

    def startService(self):
        self._d = Deferred()
        return super(ToyWorker, self).startService()

    @inlineCallbacks
    def startWorker(self):
        self.events.append("START: %s" % self.name)
        self.pub = yield self.publish_to("%s.out" % self.name)
        yield self.consume("%s.in" % self.name, self.process_message,
                           message_class=TransportUserMessage)
        self._d.callback(None)

    def stopWorker(self):
        self.events.append("STOP: %s" % self.name)

    def process_message(self, message):
        return self.pub.publish_message(
            message.reply(''.join(reversed(message['content']))))


class StubbedMultiWorker(MultiWorker):
    def WORKER_CREATOR(self, options):
        worker_creator = StubbedWorkerCreator(options)
        worker_creator.broker = self._amqp_client.broker
        return worker_creator

    def wait_for_workers(self):
        return DeferredList([w._d for w in self.workers])


def mkmsg(content):
    return TransportUserMessage(
        from_addr='from',
        to_addr='to',
        transport_name='sphex',
        transport_type='test',
        transport_metadata={},
        content=content,
        )


class MultiWorkerTestCase(VumiWorkerTestCase):

    base_config = {
        'workers': {
            'worker1': "%s.ToyWorker" % (__name__,),
            'worker2': "%s.ToyWorker" % (__name__,),
            'worker3': "%s.ToyWorker" % (__name__,),
            },
        'worker1': {
            'foo': 'bar',
            },
        }

    def setUp(self):
        super(MultiWorkerTestCase, self).setUp()
        ToyWorker.events[:] = []

    @inlineCallbacks
    def tearDown(self):
        yield super(MultiWorkerTestCase, self).tearDown()
        ToyWorker.events[:] = []

    @inlineCallbacks
    def get_multiworker(self, config):
        self.worker = yield self.get_worker(
            config, StubbedMultiWorker, start=False)
        yield self.worker.startService()
        yield self.worker.wait_for_workers()
        returnValue(self.worker)

    def dispatch(self, message, worker_name):
        rkey = "%s.in" % (worker_name,)
        return self._dispatch(message, rkey)

    def get_replies(self, worker_name):
        msgs = self._amqp.get_messages('vumi', "%s.out" % (worker_name,))
        return [m['content'] for m in msgs]

    @inlineCallbacks
    def test_start_stop_workers(self):
        self.assertEqual([], ToyWorker.events)
        worker = yield self.get_multiworker(self.base_config)
        self.assertEqual(['START: worker%s' % (i + 1) for i in range(3)],
                         sorted(ToyWorker.events))
        ToyWorker.events[:] = []
        yield worker.stopService()
        self.assertEqual(['STOP: worker%s' % (i + 1) for i in range(3)],
                         sorted(ToyWorker.events))

    @inlineCallbacks
    def test_message_flow(self):
        yield self.get_multiworker(self.base_config)
        yield self.dispatch(mkmsg("foo"), "worker1")
        self.assertEqual(['oof'], self.get_replies("worker1"))
        yield self.dispatch(mkmsg("bar"), "worker2")
        yield self.dispatch(mkmsg("baz"), "worker3")
        self.assertEqual(['rab'], self.get_replies("worker2"))
        self.assertEqual(['zab'], self.get_replies("worker3"))

    @inlineCallbacks
    def test_config(self):
        worker = yield self.get_multiworker(self.base_config)
        worker1 = worker.getServiceNamed("worker1")
        worker2 = worker.getServiceNamed("worker2")
        self.assertEqual({'foo': 'bar'}, worker1.config)
        self.assertEqual({}, worker2.config)

    @inlineCallbacks
    def test_default_config(self):
        cfg = {'defaults': {'foo': 'baz'}}
        cfg.update(self.base_config)
        worker = yield self.get_multiworker(cfg)
        worker1 = worker.getServiceNamed("worker1")
        worker2 = worker.getServiceNamed("worker2")
        self.assertEqual({'foo': 'bar'}, worker1.config)
        self.assertEqual({'foo': 'baz'}, worker2.config)

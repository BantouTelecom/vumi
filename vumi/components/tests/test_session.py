"""Tests for vumi.persist.session."""

import time

from twisted.internet.defer import inlineCallbacks
from twisted.trial.unittest import TestCase

from vumi.components import SessionManager
from vumi.tests.utils import PersistenceMixin


class SessionManagerTestCase(TestCase, PersistenceMixin):
    timeout = 2

    @inlineCallbacks
    def setUp(self):
        self._persist_setUp()
        self.manager = yield self.get_redis_manager()
        yield self.manager._purge_all()  # Just in case
        self.sm = SessionManager(self.manager)

    @inlineCallbacks
    def tearDown(self):
        yield self.sm.stop()
        yield self._persist_tearDown()

    @inlineCallbacks
    def test_active_sessions(self):
        def get_sessions():
            return self.sm.active_sessions().addCallback(lambda s: sorted(s))

        def ids():
            return get_sessions().addCallback(lambda s: [x[0] for x in s])

        self.assertEqual((yield ids()), [])
        yield self.sm.create_session("u1")
        self.assertEqual((yield ids()), ["u1"])
         # 10 seconds later
        yield self.sm.create_session("u2", created_at=time.time() + 10)
        self.assertEqual((yield ids()), ["u1", "u2"])

        s1, s2 = yield get_sessions()
        self.assertTrue(s1[1]['created_at'] < s2[1]['created_at'])

    @inlineCallbacks
    def test_schedule_session_expiry(self):
        self.sm.max_session_length = 60.0
        yield self.sm.create_session("u1")

    @inlineCallbacks
    def test_create_and_retrieve_session(self):
        session = yield self.sm.create_session("u1")
        self.assertEqual(sorted(session.keys()), ['created_at'])
        self.assertTrue(time.time() - float(session['created_at']) < 10.0)
        loaded = yield self.sm.load_session("u1")
        self.assertEqual(loaded, session)

    @inlineCallbacks
    def test_save_session(self):
        test_session = {"foo": 5, "bar": "baz"}
        yield self.sm.create_session("u1")
        yield self.sm.save_session("u1", test_session)
        session = yield self.sm.load_session("u1")
        self.assertTrue(session.pop('created_at') is not None)
        # Redis saves & returns all session values as strings
        self.assertEqual(session, dict([map(str, kvs) for kvs
                                        in test_session.items()]))

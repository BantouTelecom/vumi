"""Tests for vumi.persist.redis_base."""

from twisted.trial.unittest import TestCase

from vumi.persist.redis_base import Manager


class ManagerTestCase(TestCase):
    def mk_manager(self, key_prefix='test', client=None):
        if client is None:
            client = object()
        return Manager(client, key_prefix)

    def test_key_prefix(self):
        manager = self.mk_manager()
        self.assertEqual('test:None', manager._key(None))
        self.assertEqual('test:foo', manager._key('foo'))

    def test_no_key_prefix(self):
        manager = self.mk_manager(None)
        self.assertEqual(None, manager._key(None))
        self.assertEqual('foo', manager._key('foo'))

    def test_sub_manager(self):
        manager = self.mk_manager()
        sub_manager = manager.sub_manager("foo")
        self.assertEqual(sub_manager._key_prefix, "test:foo")
        self.assertEqual(sub_manager._client, manager._client)
        self.assertEqual(sub_manager._key_separator, manager._key_separator)

    def test_no_key_prefix_sub_manager(self):
        manager = self.mk_manager(None)
        sub_manager = manager.sub_manager("foo")
        self.assertEqual(sub_manager._key_prefix, "foo")
        self.assertEqual(sub_manager._client, manager._client)
        self.assertEqual(sub_manager._key_separator, manager._key_separator)

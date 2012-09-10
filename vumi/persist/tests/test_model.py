"""Tests for vumi.persist.model."""

from twisted.trial.unittest import TestCase
from twisted.internet.defer import inlineCallbacks

from vumi.persist.model import Model, Manager
from vumi.persist.fields import (
    ValidationError, Integer, Unicode, VumiMessage, Dynamic, ListOf,
    ForeignKey, ManyToMany)
from vumi.message import TransportUserMessage
from vumi.tests.utils import import_skip


class SimpleModel(Model):
    a = Integer()
    b = Unicode()


class IndexedModel(Model):
    a = Integer(index=True)
    b = Unicode(index=True, null=True)


class VumiMessageModel(Model):
    msg = VumiMessage(TransportUserMessage)


class DynamicModel(Model):
    a = Unicode()
    contact_info = Dynamic()


class ListOfModel(Model):
    items = ListOf(Integer())


class ForeignKeyModel(Model):
    simple = ForeignKey(SimpleModel, null=True)


class ManyToManyModel(Model):
    simples = ManyToMany(SimpleModel)


class InheritedModel(SimpleModel):
    c = Integer()


class OverriddenModel(InheritedModel):
    c = Integer(min=0, max=5)


class TestModelOnTxRiak(TestCase):

    # TODO: all copies of mkmsg must be unified!
    def mkmsg(self, **kw):
        kw.setdefault("transport_name", "sphex")
        kw.setdefault("transport_type", "sphex_type")
        kw.setdefault("to_addr", "1234")
        kw.setdefault("from_addr", "5678")
        return TransportUserMessage(**kw)

    @inlineCallbacks
    def setUp(self):
        try:
            from vumi.persist.txriak_manager import TxRiakManager
        except ImportError, e:
            import_skip(e, 'riakasaurus.riak')
        self.manager = TxRiakManager.from_config({'bucket_prefix': 'test.'})
        yield self.manager.purge_all()

    @Manager.calls_manager
    def tearDown(self):
        yield self.manager.purge_all()

    def test_simple_class(self):
        field_names = SimpleModel.field_descriptors.keys()
        self.assertEqual(sorted(field_names), ['a', 'b'])
        self.assertTrue(isinstance(SimpleModel.a, Integer))
        self.assertTrue(isinstance(SimpleModel.b, Unicode))

    def test_repr(self):
        simple_model = self.manager.proxy(SimpleModel)
        s = simple_model("foo", a=1, b=u"bar")
        self.assertEqual(repr(s), "<SimpleModel key=foo a=1 b=u'bar'>")

    def test_declare_backlinks(self):
        class TestModel(Model):
            pass

        TestModel.backlinks.declare_backlink("foo", lambda m, o: None)
        self.assertRaises(RuntimeError, TestModel.backlinks.declare_backlink,
                          "foo", lambda m, o: None)

        t = TestModel(self.manager, "key")
        self.assertTrue(callable(t.backlinks.foo))
        self.assertRaises(AttributeError, getattr, t.backlinks, 'bar')

    @Manager.calls_manager
    def test_simple_search(self):
        simple_model = self.manager.proxy(SimpleModel)
        yield simple_model.enable_search()
        yield simple_model("one", a=1, b=u'abc').save()
        yield simple_model("two", a=2, b=u'def').save()
        yield simple_model("three", a=2, b=u'ghi').save()

        [s1] = yield simple_model.search(a=1)
        self.assertEqual(s1.key, "one")
        self.assertEqual(s1.a, 1)
        self.assertEqual(s1.b, u'abc')

        [s2] = yield simple_model.search(a=2, b='def')
        self.assertEqual(s2.key, "two")

        keys = yield simple_model.search(a=2, return_keys=True)
        self.assertEqual(sorted(keys), ["three", "two"])

    @Manager.calls_manager
    def test_simple_search_escaping(self):
        simple_model = self.manager.proxy(SimpleModel)
        search = simple_model.search
        yield simple_model.enable_search()
        yield simple_model("one", a=1, b=u'a\'bc').save()

        search = lambda **q: simple_model.search(return_keys=True, **q)
        self.assertEqual((yield search(b=" OR a:1")), [])
        self.assertEqual((yield search(b="b' OR a:1 '")), [])
        self.assertEqual((yield search(b="a\'bc")), ["one"])

    @Manager.calls_manager
    def test_simple_riak_search(self):
        simple_model = self.manager.proxy(SimpleModel)
        yield simple_model.enable_search()
        yield simple_model("one", a=1, b=u'abc').save()
        yield simple_model("two", a=2, b=u'def').save()
        yield simple_model("three", a=2, b=u'ghi').save()

        [s1] = yield simple_model.riak_search('a:1')
        self.assertEqual(s1.key, "one")
        self.assertEqual(s1.a, 1)
        self.assertEqual(s1.b, u'abc')

        [s2] = yield simple_model.riak_search('a:2 AND b:def')
        self.assertEqual(s2.key, "two")

        [s1, s2] = yield simple_model.riak_search('b:abc OR b:def')
        self.assertEqual(s1.key, "one")
        self.assertEqual(s2.key, "two")

        keys = yield simple_model.riak_search('a:2', return_keys=True)
        self.assertEqual(sorted(keys), ["three", "two"])

    @Manager.calls_manager
    def test_simple_instance(self):
        simple_model = self.manager.proxy(SimpleModel)
        s1 = simple_model("foo", a=5, b=u'3')
        yield s1.save()

        s2 = yield simple_model.load("foo")
        self.assertEqual(s2.a, 5)
        self.assertEqual(s2.b, u'3')

    @Manager.calls_manager
    def test_simple_instance_delete(self):
        simple_model = self.manager.proxy(SimpleModel)
        s1 = simple_model("foo", a=5, b=u'3')
        yield s1.save()

        s2 = yield simple_model.load("foo")
        yield s2.delete()

        s3 = yield simple_model.load("foo")
        self.assertEqual(s3, None)

    @Manager.calls_manager
    def test_nonexist_keys_return_none(self):
        simple_model = self.manager.proxy(SimpleModel)
        s = yield simple_model.load("foo")
        self.assertEqual(s, None)

    @Manager.calls_manager
    def test_by_index(self):
        indexed_model = self.manager.proxy(IndexedModel)
        yield indexed_model("foo1", a=1, b=u"one").save()
        yield indexed_model("foo2", a=2, b=u"two").save()

        [key] = yield indexed_model.by_index(a=1, return_keys=True)
        self.assertEqual(key, "foo1")

        [obj] = yield indexed_model.by_index(b="two")
        self.assertEqual(obj.key, "foo2")
        self.assertEqual(obj.b, "two")

    @Manager.calls_manager
    def test_by_index_null(self):
        indexed_model = self.manager.proxy(IndexedModel)
        yield indexed_model("foo1", a=1, b=u"one").save()
        yield indexed_model("foo2", a=2, b=None).save()

        [key] = yield indexed_model.by_index(b=None, return_keys=True)
        self.assertEqual(key, "foo2")

    @Manager.calls_manager
    def test_vumimessage_field(self):
        msg_model = self.manager.proxy(VumiMessageModel)
        msg = self.mkmsg(extra="bar")
        m1 = msg_model("foo", msg=msg)
        yield m1.save()

        m2 = yield msg_model.load("foo")
        self.assertEqual(m1.msg, m2.msg)
        self.assertEqual(m2.msg, msg)

        self.assertRaises(ValidationError, setattr, m1, "msg", "foo")

        # test extra keys are removed
        msg2 = self.mkmsg()
        m1.msg = msg2
        self.assertTrue("extra" not in m1.msg)

    def _create_dynamic_instance(self, dynamic_model):
        d1 = dynamic_model("foo", a=u"ab")
        d1.contact_info['cellphone'] = u"+27123"
        d1.contact_info['telephone'] = u"+2755"
        d1.contact_info['honorific'] = u"BDFL"
        return d1

    @Manager.calls_manager
    def test_dynamic_fields(self):
        dynamic_model = self.manager.proxy(DynamicModel)
        d1 = self._create_dynamic_instance(dynamic_model)
        yield d1.save()

        d2 = yield dynamic_model.load("foo")
        self.assertEqual(d2.a, u"ab")
        self.assertEqual(d2.contact_info['cellphone'], u"+27123")
        self.assertEqual(d2.contact_info['telephone'], u"+2755")
        self.assertEqual(d2.contact_info['honorific'], u"BDFL")

    def test_dynamic_field_init(self):
        dynamic_model = self.manager.proxy(DynamicModel)
        contact_info = {'cellphone': u'+27123',
                        'telephone': u'+2755'}
        d1 = dynamic_model("foo", a=u"ab", contact_info=contact_info)
        self.assertEqual(d1.contact_info.copy(), contact_info)

    def test_dynamic_field_keys(self):
        d1 = self._create_dynamic_instance(self.manager.proxy(DynamicModel))
        keys = d1.contact_info.keys()
        iterkeys = d1.contact_info.iterkeys()
        self.assertTrue(keys, list)
        self.assertTrue(hasattr(iterkeys, 'next'))
        self.assertEqual(sorted(keys), ['cellphone', 'honorific', 'telephone'])
        self.assertEqual(sorted(iterkeys), sorted(keys))

    def test_dynamic_field_values(self):
        d1 = self._create_dynamic_instance(self.manager.proxy(DynamicModel))
        values = d1.contact_info.values()
        itervalues = d1.contact_info.itervalues()
        self.assertTrue(isinstance(values, list))
        self.assertTrue(hasattr(itervalues, 'next'))
        self.assertEqual(sorted(values), ["+27123", "+2755", "BDFL"])
        self.assertEqual(sorted(itervalues), sorted(values))

    def test_dynamic_field_items(self):
        d1 = self._create_dynamic_instance(self.manager.proxy(DynamicModel))
        items = d1.contact_info.items()
        iteritems = d1.contact_info.iteritems()
        self.assertTrue(isinstance(items, list))
        self.assertTrue(hasattr(iteritems, 'next'))
        self.assertEqual(sorted(items), [('cellphone', "+27123"),
                                         ('honorific', "BDFL"),
                                         ('telephone', "+2755")])
        self.assertEqual(sorted(iteritems), sorted(items))

    def test_dynamic_field_clear(self):
        d1 = self._create_dynamic_instance(self.manager.proxy(DynamicModel))
        d1.contact_info.clear()
        self.assertEqual(d1.contact_info.keys(), [])

    def test_dynamic_field_update(self):
        d1 = self._create_dynamic_instance(self.manager.proxy(DynamicModel))
        d1.contact_info.update({"cellphone": "123", "name": "foo"})
        self.assertEqual(sorted(d1.contact_info.items()), [
            ('cellphone', "123"), ('honorific', "BDFL"), ('name', "foo"),
            ('telephone', "+2755")])

    def test_dynamic_field_contains(self):
        d1 = self._create_dynamic_instance(self.manager.proxy(DynamicModel))
        self.assertTrue("cellphone" in d1.contact_info)
        self.assertFalse("landline" in d1.contact_info)

    def test_dynamic_field_del(self):
        d1 = self._create_dynamic_instance(self.manager.proxy(DynamicModel))
        del d1.contact_info["telephone"]
        self.assertEqual(sorted(d1.contact_info.keys()),
                         ['cellphone', 'honorific'])

    @Manager.calls_manager
    def test_listof_fields(self):
        list_model = self.manager.proxy(ListOfModel)
        l1 = list_model("foo")
        l1.items.append(1)
        l1.items.append(2)
        yield l1.save()

        l2 = yield list_model.load("foo")
        self.assertEqual(l2.items[0], 1)
        self.assertEqual(l2.items[1], 2)
        self.assertEqual(list(l2.items), [1, 2])

        l2.items[0] = 5
        self.assertEqual(l2.items[0], 5)

        del l2.items[0]
        self.assertEqual(list(l2.items), [2])

        l2.items.extend([3, 4, 5])
        self.assertEqual(list(l2.items), [2, 3, 4, 5])

        l2.items = [1]
        self.assertEqual(list(l2.items), [1])

    @Manager.calls_manager
    def test_foreignkey_fields(self):
        fk_model = self.manager.proxy(ForeignKeyModel)
        simple_model = self.manager.proxy(SimpleModel)
        s1 = simple_model("foo", a=5, b=u'3')
        f1 = fk_model("bar")
        f1.simple.set(s1)
        yield s1.save()
        yield f1.save()

        f2 = yield fk_model.load("bar")
        s2 = yield f2.simple.get()

        self.assertEqual(f2.simple.key, "foo")
        self.assertEqual(s2.a, 5)
        self.assertEqual(s2.b, u"3")

        f2.simple.set(None)
        s3 = yield f2.simple.get()
        self.assertEqual(s3, None)

        f2.simple.key = "foo"
        s4 = yield f2.simple.get()
        self.assertEqual(s4.key, "foo")

        f2.simple.key = None
        s5 = yield f2.simple.get()
        self.assertEqual(s5, None)

        self.assertRaises(ValidationError, f2.simple.set, object())

    @Manager.calls_manager
    def test_reverse_foreignkey_fields(self):
        fk_model = self.manager.proxy(ForeignKeyModel)
        simple_model = self.manager.proxy(SimpleModel)
        s1 = simple_model("foo", a=5, b=u'3')
        f1 = fk_model("bar1")
        f1.simple.set(s1)
        f2 = fk_model("bar2")
        f2.simple.set(s1)
        yield s1.save()
        yield f1.save()
        yield f2.save()

        s2 = yield simple_model.load("foo")
        results = yield s2.backlinks.foreignkeymodels()
        self.assertEqual(sorted(s.key for s in results), ["bar1", "bar2"])
        self.assertEqual([s.__class__ for s in results],
                         [ForeignKeyModel] * 2)

    @Manager.calls_manager
    def test_manytomany_field(self):
        mm_model = self.manager.proxy(ManyToManyModel)
        simple_model = self.manager.proxy(SimpleModel)

        s1 = simple_model("foo", a=5, b=u'3')
        m1 = mm_model("bar")
        m1.simples.add(s1)
        yield s1.save()
        yield m1.save()

        m2 = yield mm_model.load("bar")
        [s2] = yield m2.simples.get_all()

        self.assertEqual(m2.simples.keys(), ["foo"])
        self.assertEqual(s2.a, 5)
        self.assertEqual(s2.b, u"3")

        m2.simples.remove(s2)
        simples = yield m2.simples.get_all()
        self.assertEqual(simples, [])

        m2.simples.add_key("foo")
        [s4] = yield m2.simples.get_all()
        self.assertEqual(s4.key, "foo")

        m2.simples.remove_key("foo")
        simples = yield m2.simples.get_all()
        self.assertEqual(simples, [])

        self.assertRaises(ValidationError, m2.simples.add, object())
        self.assertRaises(ValidationError, m2.simples.remove, object())

        t1 = simple_model("bar1", a=3, b=u'4')
        t2 = simple_model("bar2", a=4, b=u'4')
        m2.simples.add(t1)
        m2.simples.add(t2)
        yield t1.save()
        yield t2.save()
        simples = yield m2.simples.get_all()
        simples.sort(key=lambda s: s.key)
        self.assertEqual([s.key for s in simples], ["bar1", "bar2"])
        self.assertEqual(simples[0].a, 3)
        self.assertEqual(simples[1].a, 4)

        m2.simples.clear()
        m2.simples.add_key("unknown")
        [s5] = yield m2.simples.get_all()
        self.assertEqual(s5, None)

    @Manager.calls_manager
    def test_reverse_manytomany_fields(self):
        mm_model = self.manager.proxy(ManyToManyModel)
        simple_model = self.manager.proxy(SimpleModel)
        s1 = simple_model("foo1", a=5, b=u'3')
        s2 = simple_model("foo2", a=4, b=u'4')
        m1 = mm_model("bar1")
        m1.simples.add(s1)
        m1.simples.add(s2)
        m2 = mm_model("bar2")
        m2.simples.add(s1)
        yield s1.save()
        yield s2.save()
        yield m1.save()
        yield m2.save()

        s1 = yield simple_model.load("foo1")
        results = yield s1.backlinks.manytomanymodels()
        self.assertEqual(sorted(s.key for s in results), ["bar1", "bar2"])
        self.assertEqual([s.__class__ for s in results],
                         [ManyToManyModel] * 2)

        s2 = yield simple_model.load("foo2")
        results = yield s2.backlinks.manytomanymodels()
        self.assertEqual(sorted(s.key for s in results), ["bar1"])

    @Manager.calls_manager
    def test_inherited_model(self):
        field_names = InheritedModel.field_descriptors.keys()
        self.assertEqual(sorted(field_names), ["a", "b", "c"])

        inherited_model = self.manager.proxy(InheritedModel)

        im1 = inherited_model("foo", a=1, b=u"2", c=3)
        yield im1.save()

        im2 = yield inherited_model.load("foo")
        self.assertEqual(im2.a, 1)
        self.assertEqual(im2.b, u'2')
        self.assertEqual(im2.c, 3)

    def test_overriden_model(self):
        int_field = OverriddenModel.field_descriptors['c'].field
        self.assertEqual(int_field.max, 5)
        self.assertEqual(int_field.min, 0)

        overridden_model = self.manager.proxy(OverriddenModel)

        overridden_model("foo", a=1, b=u"2", c=3)
        self.assertRaises(ValidationError, overridden_model, "foo",
                          a=1, b=u"2", c=-1)


class TestModelOnRiak(TestModelOnTxRiak):

    def setUp(self):
        try:
            from vumi.persist.riak_manager import RiakManager
        except ImportError, e:
            import_skip(e, 'riak')

        self.manager = RiakManager.from_config({'bucket_prefix': 'test.'})
        self.manager.purge_all()

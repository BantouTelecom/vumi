# -*- test-case-name: vumi.tests.test_worker -*-

"""Basic tools for workers that handle TransportMessages."""

from twisted.internet.defer import inlineCallbacks, succeed, maybeDeferred
from twisted.python import log

from vumi.service import Worker
from vumi.middleware import setup_middlewares_from_config
from vumi.connectors import ReceiveInboundConnector, ReceiveOutboundConnector
from vumi.config import Config, ConfigInt
from vumi.errors import DuplicateConnectorError
from vumi.blinkenlights.heartbeat import (HeartBeatPublisher,
                                          HeartBeatMessage)

import time
import os
import socket


def then_call(d, func, *args, **kw):
    return d.addCallback(lambda r: func(*args, **kw))


class BaseConfig(Config):
    """Base config definition for workers.

    You should subclass this and add worker-specific fields.
    """

    amqp_prefetch_count = ConfigInt(
        "The number of messages fetched concurrently from each AMQP queue"
        " by each worker instance.",
        default=20, static=True)


class BaseWorker(Worker):
    """Base class for a message processing worker.

    This contains common functionality used by application, transport and
    dispatcher workers. It should be subclassed by workers that need to
    manage their own connectors.
    """

    CONFIG_CLASS = BaseConfig

    def __init__(self, options, config=None):
        super(BaseWorker, self).__init__(options, config=config)
        self.connectors = {}
        self.middlewares = []
        self._static_config = self.CONFIG_CLASS(self.config, static=True)
        self._hb_pub = None

    def startWorker(self):
        log.msg('Starting a %s worker with config: %s'
                % (self.__class__.__name__, self.config))
        d = maybeDeferred(self._validate_config)
        then_call(d, self.setup_heartbeat)
        then_call(d, self.setup_middleware)
        then_call(d, self.setup_connectors)
        then_call(d, self.setup_worker)
        return d

    def stopWorker(self):
        log.msg('Stopping a %s worker.' % (self.__class__.__name__,))
        d = succeed(None)
        then_call(d, self.teardown_worker)
        then_call(d, self.teardown_connectors)
        then_call(d, self.teardown_middleware)
        then_call(d, self.teardown_heartbeat)
        return d

    def setup_connectors(self):
        raise NotImplementedError()

    @inlineCallbacks
    def setup_heartbeat(self):
        # Disable heartbeats if worker_name is not set. We're
        # currently using it as the primary identifier for a worker
        if 'worker_name' in self.config:
            log.msg("Starting HeartBeat publisher with worker_id=%s"
                    % self.config.get("worker_name"))
            self._hb_pub = yield self.start_publisher(HeartBeatPublisher,
                                                self._gen_heartbeat_attrs)
        else:
            log.msg("HeartBeat publisher disabled. No worker_id "
                    "field found in config.")

    def teardown_heartbeat(self):
        if self._hb_pub is not None:
            self._hb_pub.stop()
            self._hb_pub = None

    def _gen_heartbeat_attrs(self):
        worker_id = self.config.get("worker_name")
        attrs = {
            'version': HeartBeatMessage.VERSION_20130319,
            'system_id': Worker.SYSTEM_ID,
            'worker_id': worker_id,
            'hostname': socket.gethostname(),
            'timestamp': time.time(),
            'pid': os.getpid(),
        }
        return attrs

    def teardown_connectors(self):
        d = succeed(None)
        for connector_name in self.connectors.keys():
            then_call(d, self.teardown_connector, connector_name)
        return d

    def setup_worker(self):
        raise NotImplementedError()

    def teardown_worker(self):
        raise NotImplementedError()

    def setup_middleware(self):
        """Create middlewares from config."""
        d = setup_middlewares_from_config(self, self.config)
        d.addCallback(self.middlewares.extend)
        return d

    def teardown_middleware(self):
        """Teardown middlewares."""
        d = succeed(None)
        for mw in reversed(self.middlewares):
            then_call(d, mw.teardown_middleware)
        return d

    def get_static_config(self):
        """Return static (message independent) configuration."""
        return self._static_config

    def get_config(self, msg):
        """This should return a message-specific config object.

        It deliberately returns a deferred even when this isn't strictly
        necessary to ensure that workers will continue to work when per-message
        configuration needs to be fetched from elsewhere.
        """
        return succeed(self.CONFIG_CLASS(self.config))

    def _validate_config(self):
        """Once subclasses call `super().validate_config` properly,
           this method can be removed.
           """
        # TODO: remove this once all uses of validate_config have been fixed.
        self.validate_config()

    def validate_config(self):
        """
        Application-specific config validation happens in here.

        Subclasses may override this method to perform extra config
        validation.
        """
        # TODO: deprecate this in favour of a similar method on
        #       config classes.
        pass

    def setup_connector(self, connector_cls, connector_name, middleware=False):
        if connector_name in self.connectors:
            raise DuplicateConnectorError("Attempt to add duplicate connector"
                                          " with name %r" % (connector_name,))
        prefetch_count = self.get_static_config().amqp_prefetch_count
        middlewares = self.middlewares if middleware else None

        connector = connector_cls(self, connector_name,
                                  prefetch_count=prefetch_count,
                                  middlewares=middlewares)
        self.connectors[connector_name] = connector

        d = connector.setup()
        d.addCallback(lambda r: connector)
        return d

    def teardown_connector(self, connector_name):
        connector = self.connectors.pop(connector_name)
        d = connector.teardown()
        d.addCallback(lambda r: connector)
        return d

    def setup_ri_connector(self, connector_name, middleware=True):
        return self.setup_connector(ReceiveInboundConnector, connector_name,
                                    middleware=middleware)

    def setup_ro_connector(self, connector_name, middleware=True):
        return self.setup_connector(ReceiveOutboundConnector, connector_name,
                                    middleware=middleware)

    def pause_connectors(self):
        for connector in self.connectors.itervalues():
            connector.pause()

    def unpause_connectors(self):
        for connector in self.connectors.itervalues():
            connector.unpause()

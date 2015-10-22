# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import os
from collections import namedtuple
from contextlib import contextmanager
from subprocess import check_call
from subprocess import Popen

from cached_property import cached_property
from frozendict import frozendict
from py._error import error as pylib_error

from .daemontools import prepend_timestamps_to
from .daemontools import svc
from .daemontools import svok
from .daemontools import SvStat
from .daemontools import svstat
from .debug import debug
from .debug import trace
from .errors import Impossible
from .errors import NoSuchService
from .errors import NotReady
from .flock import Locked
from .functions import exec_
from .functions import show_runaway_processes


def idempotent_supervise(wrapped):
    """Run supervise(2), but be successful if it's run too many times."""

    def wrapper(self):
        if svok(self.path.strpath):
            return
        else:
            return wrapped(self)

    return wrapper


class Service(namedtuple('Service', ['path', 'scratch_dir', 'default_timeout'])):
    # TODO-TEST: regression: these cached-properties are actually cached

    def __str__(self):
        return self.name

    def svstat(self):
        self.assert_exists()
        with self.path.dirpath().as_cwd():
            result = svstat(self.name)
        if not self.notification_fd.exists():
            # services without notification need to be considered ready sometimes
            if (
                    # an 'up' service is always ready
                    (result.state == 'up' and result.process is None) or
                    # restarting continuously and successfully can/should be considered 'ready'
                    (result.process == 'starting' and result.exitcode == 0 and result.seconds == 0)
            ):
                result = result._replace(state='ready')
        trace('PARSED: %s', result)
        return result

    @cached_property
    def ready_script(self):
        return self.path.join('ready')

    @cached_property
    def notification_fd(self):
        return self.path.join('notification-fd')

    def start(self):
        """Idempotent start of a service or group of services"""
        self.background()
        svc(('-u', self.path.strpath))

    def stop(self):
        """Idempotent stop of a service or group of services"""
        self.assert_exists()
        svc(('-dx', self.path.strpath))

    def __get_timeout(self, name, default):
        timeout = self.path.join(name)
        if timeout.check():
            debug('%s exists', name)
            return float(timeout.read().strip())
        else:
            debug('%s doesn\'t exist', name)
            return float(default)

    @cached_property
    def timeout_stop(self):
        return self.__get_timeout('timeout-stop', self.default_timeout)

    @cached_property
    def timeout_ready(self):
        return self.__get_timeout('timeout-ready', self.default_timeout)

    def assert_stopped(self):
        status = self.svstat()
        if status.state != SvStat.UNSUPERVISED:
            raise NotReady('its status is ' + str(status))

        racelimit = 10
        while racelimit > 0:
            racelimit -= 1

            try:
                with self.flock():
                    return  # assertion success; nothing is running
            except Locked:
                show_runaway_processes(self.path.strpath)

        raise Impossible('lost the race 10 times in a row')

    def assert_ready(self):
        status = self.svstat()
        if status.state != 'ready':
            raise NotReady('its status is ' + str(status))

    def assert_exists(self):
        if not self.path.check(dir=True):
            raise NoSuchService("No such playground service: '%s'" % self.name)

    def ensure_logs(self):
        self.path.ensure('log')

    def ensure_directory_structure(self):
        """Ensure that the scratch directory exists and symlinks supervise.

        Due to quirks in pip and potentially other package managers, we don't
        want named FIFOs on disk inside the project repo (they'll end up in
        tarballs and other junk).

        Instead, we stick them in a scratch directory outside of the repo.
        """
        # TODO: enforce that we have the supervise lock when this is called, somehow
        self.assert_exists()
        self.ensure_logs()
        self.path.ensure('nosetsid')  # see http://skarnet.org/software/s6/servicedir.html
        try:
            self.path.join('down').remove()  # pgctl doesn't support the s6 down file
        except pylib_error.ENOENT:
            pass

        if self.ready_script.exists():
            with self.notification_fd.open('w') as f:
                f.write('%i\n' % f.fileno())
        supervise_in_scratch = self.scratch_dir.join('supervise')
        supervise_in_scratch.ensure_dir()

        # ensure symlink {service_dir}/supervise -> {scratch_dir}/supervise
        # TODO-TEST: a test that fails without -n
        check_call((
            'ln', '-sfn', '--',
            supervise_in_scratch.strpath,
            self.path.join('supervise').strpath,
        ))

    @contextmanager
    def flock(self):
        # if we already have the lock, from a parent process, use it.
        lock = os.environ.pop('PGCTL_SERVICE_LOCK', None)
        debug('parentlock: %r', lock)
        if lock:
            lock = int(lock)
            debug('retrieved parent lock! %i', lock)
            try:
                yield lock
            finally:
                os.close(lock)
        else:
            from .flock import flock
            with flock(self.path.strpath) as lock:
                debug('LOCK: %i', lock)
                self.ensure_directory_structure()
                with self.path.as_cwd():
                    yield lock

    @idempotent_supervise
    def background(self):
        """Run supervise(1), while ensuring it is properly symlinked."""
        with self.flock() as lock:
            log = self.path.join('log').open('a')
            log = prepend_timestamps_to(log)
            Popen(
                ('s6-supervise', self.path.strpath),
                stdin=open(os.devnull, 'w'),
                stdout=log.fileno(),
                stderr=log.fileno(),
                env=self.supervise_env(lock, debug=False),
                close_fds=False,  # we must keep the flock file descriptor opened.
            )
            log.close()

    @idempotent_supervise
    def foreground(self):
        with self.flock() as lock:
            exec_(
                ('s6-supervise', self.path.strpath),
                env=self.supervise_env(lock, debug=True),
            )

    @cached_property
    def name(self):
        return self.path.basename

    def supervise_env(self, lock, debug):
        """Returns an environment dict to use for running supervise."""
        env = dict(
            os.environ,
            PGCTL_SCRATCH=str(self.scratch_dir),
            # TODO-TEST: assert this env var is available and correct
            PGCTL_SERVICE=str(self.path),
            PGCTL_SERVICE_LOCK=str(lock),
        )
        if debug:
            env['PGCTL_DEBUG'] = 'true'
        else:
            env.pop('PGCTL_DEBUG', None)
        return frozendict(env)

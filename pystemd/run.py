#
# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.
#
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import pty as ptylib
import select
import sys
import struct
import termios
import fcntl
import tty
import uuid

import pystemd

from pystemd.dbuslib import DBus, DBusMachine
from pystemd.systemd1 import Manager as SDManager, Unit


EXIT_SUBSTATES = (b'exited', b'failed', b'dead')


class CExit:
    def __init__(self):
        self.pipe = []

    def __enter__(self):
        return self

    def __exit__(self, *excargs, **exckw):
        for call, args, kwargs in reversed(self.pipe):
            call(*args, **kwargs)

    def register(self, meth, *args, **kwargs):
        self.pipe.append((meth, args, kwargs))


def get_fno(obj):
    """
    Try to get the best fileno of a obj:
        * If the obj is a integer, it return that integer.
        * If the obj has a fileno method, it return that function call.
    """
    if obj is None:
        return None
    elif isinstance(obj, int):
        return obj
    elif hasattr(obj, 'fileno') and callable(getattr(obj, 'fileno')):
        return obj.fileno()

    raise TypeError("Expected None, int or fileobject with fileno method")


def run(cmd,
        name=None,
        user=None,
        user_mode=os.getuid() != 0,
        nice=None,
        runtime_max_sec=None,
        env=None,
        extra=None,
        cwd=None,
        machine=None,
        wait=False,
        remain_after_exit=False,
        pty=None, pty_master=None, pty_path=None,
        stdin=None, stdout=None, stderr=None,
        _wait_polling=None):
    """
    pystemd.run imitates systemd-run, but with a pythonic feel to it.

    Options:

        cmd: Array with the command to execute (absolute path only)
        name: Name of the unit, if not provided autogenerated.
        user: Username to execute the command, defaults to current user.
        user_mode: Equivalent to running `systemd-run --user`. Defaults to True
            if current user id not root (uid = 0).
        nice: Nice level to run the command.
        runtime_max_sec: set seconds before sending a sigterm to the process, if
           the service does not die nicely, it will send a sigkill.
        env: A dict with environmental variables.
        extra: If you know what you are doing, you can pass extra configuration
            settings to the start_transient_unit method.
        machine: Machine name to execute the command, by default we connect to
            the host's dbus.
        wait: wait for command completition before returning control, defaults
            to False.
        remain_after_exit: If True, the transient unit will remain after cmd
            has finish, also if true, this methods will return
            pystemd.systemd1.Unit object. defaults to False and this method
            returns None and the unit will be gone as soon as is done.
        pty: Set this variable to True if you want a pty to be created. if you
            pass a `machine`, the pty will be created in the machine. Setting
            this value will ignore whatever you set in pty_master and pty_path.
        pty_master: it has only meaning if you pass a pty_path also, this file
            descriptor will be used to foward redirection to `stdin` and `stdout`
            if no `stdin` or `stdout` is present, then this value does nothing.
        pty_path: Setting this value will pass this pty_path to the created
            process and will connect the process stdin, stdout and stderr to this
            pty. by itself it only ensure that your process has a real pty that
            can have ioctl operation over it. if you also pass a `pty_master`,
            `stdin` and `stdout` the pty forwars is handle for you.
        stdin: Specify a file descriptor for stdin, by default this is `None`
            and your unit will not have a stdin, you can specify it as
            `sys.stdin.fileno()`, or as a regular numer, e.g. `0`. If you set
            pty = True, or pass `pty_master` then this file descriptor will be
            read and forward to the pty.
        stdout: Specify a file descriptor for stdout, by default this is `None`
            and your unit will not have a stdout, you can specify it as
            `sys.stdout.fileno()`, or `open('/tmp/out', 'w').fileno()`, or a
            regular number, e.g. `1`. If you set pty = True, or pass `pty_master`
            then that pty will be read and forward to this file descriptor.
        stderr: Specify a file descriptor for stderr, by default this is `None`
            and your unit will not have a stderr, you can specify it as
            `sys.stderr.fileno()`, or `open('/tmp/err', 'w').fileno()`, or a
            regular number, e.g. `2`.

    More info in:
    https://github.com/facebookincubator/pystemd/blob/master/_docs/pystemd.run.md

    """
    def bus_factory():
        if machine:
            return DBusMachine(machine)
        else:
            return DBus(user_mode=user_mode)

    name = name or 'pystemd{}.service'.format(uuid.uuid4().hex).encode()
    runtime_max_usec = (runtime_max_sec or 0) * 10**6 or runtime_max_sec

    stdin, stdout, stderr = get_fno(stdin), get_fno(stdout), get_fno(stderr)
    env = env or {}
    unit_properties = {}
    selectors = []

    if user_mode:
        _wait_polling = _wait_polling or 0.5

    with CExit() as ctexit, \
            bus_factory() as bus, \
            SDManager(bus=bus) as manager:

        if pty:
            if machine:
                with pystemd.machine1.Machine(machine) as m:
                    pty_master, pty_path = m.Machine.OpenPTY()
            else:
                pty_master, pty_follower = ptylib.openpty()
                pty_path = os.ttyname(pty_follower).encode()
                ctexit.register(os.close, pty_master)

        if pty_path:
            unit_properties.update({
                b'StandardInput': b'tty',
                b'StandardOutput': b'tty',
                b'StandardError': b'tty',
                b'TTYPath': pty_path,
            })

            if None not in (stdin, pty_master):
                # lets set raw mode for stdin so we can foward input without
                # waiting for a new line, but lets also make sure we return the
                # attributes as they where after this method is done
                stdin_attrs = tty.tcgetattr(stdin)
                tty.setraw(stdin)
                ctexit.register(
                    tty.tcsetattr, stdin, tty.TCSAFLUSH, stdin_attrs)
                selectors.append(stdin)

            if None not in (stdout, pty_master):
                if os.getenv('TERM'):
                    env[b'TERM'] = env.get(b'TERM', os.getenv('TERM').encode())

                selectors.append(pty_master)
                # lets be a friend and set the size of the pty.
                winsize = fcntl.ioctl(
                    stdout, termios.TIOCGWINSZ,
                    struct.pack('HHHH', 0, 0, 0, 0))
                fcntl.ioctl(
                    pty_master, termios.TIOCSWINSZ, winsize)
        else:
            unit_properties.update({
                b'StandardInputFileDescriptor': get_fno(stdin) if stdin else stdin,
                b'StandardOutputFileDescriptor': get_fno(stdout) if stdout else stdout,
                b'StandardErrorFileDescriptor': get_fno(stderr) if stderr else stderr,
            })

        unit_properties.update({
            b'Description': b'pystemd: ' + name,
            b'ExecStart': [(cmd[0], cmd, False)],
            b'RemainAfterExit': remain_after_exit,
            b'WorkingDirectory': cwd,
            b'User': user,
            b'Nice': nice,
            b'RuntimeMaxUSec': runtime_max_usec,
            b'Environment': [
                b'%s=%s' % (key, value)
                for key, value in env.items()
            ] or None
        })

        unit_properties.update(extra or {})
        unit_properties = {
            k: v for k, v in unit_properties.items() if v is not None}

        unit = Unit(name, bus=bus, _autoload=True)
        if wait:
            mstr = (
                "type='signal',"
                "sender='org.freedesktop.systemd1',"
                "path='{}',"
                "interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged'"
            ).format(unit.path.decode()).encode()

            monbus = bus_factory()
            monbus.open()
            ctexit.register(monbus.close)

            monitor = pystemd.DBus.Manager(bus=monbus, _autoload=True)
            monitor.Monitoring.BecomeMonitor([mstr], 0)

            monitor_fd = monbus.get_fd()
            selectors.append(monitor_fd)

        # start the process
        manager.Manager.StartTransientUnit(name, b'fail', unit_properties)

        while wait:
            _in, _, _ = select.select(selectors, [], [], _wait_polling)

            if stdin in _in:
                data = os.read(stdin, 1024)
                os.write(pty_master, data)

            if pty_master in _in:

                try:
                    data = os.read(pty_master, 1024)
                except OSError:
                    selectors.remove(pty_master)
                else:
                    os.write(stdout, data)

            if monitor_fd in _in:
                m = monbus.process()
                if m.is_empty():
                    continue

                m.process_reply(False)
                if m.get_path() == unit.path:
                    if m.body[1].get(b'SubState') in EXIT_SUBSTATES:
                        break

            if _wait_polling and not _in and unit.Service.MainPID == 0:
                # on usermode the subcribe to events does not work that well
                # this is a temporaly hack. you can always not wait on usermode.
                break

        if remain_after_exit:
            unit.load()
            unit.bus_context = bus_factory
            return unit


# do pystemd.run callable.
run.__module__ = sys.modules[__name__]
sys.modules[__name__] = run

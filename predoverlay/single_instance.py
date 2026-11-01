"""One running copy per Windows user: a named mutex, plus a local socket that
lets a second launch tell the first one to say it is already running."""

import ctypes
import getpass
import sys

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183


def _name():
    return "PredOverlay-" + "".join(c for c in getpass.getuser() if c.isalnum())


class Guard:
    def __init__(self):
        self.handle = None
        self.server = None
        self.pokes = 0  # second launches seen before ``listen`` had a handler

    def acquire(self):
        """True when this is the only copy. The mutex dies with the process."""
        if sys.platform != "win32":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateMutexW(None, False, "Local\\" + _name())
        error = ctypes.get_last_error()
        if not self.handle:  # e.g. the first copy runs elevated: we may not open it
            return error not in (ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS)
        return error != ERROR_ALREADY_EXISTS

    def listen(self):
        """In the first copy: start accepting pokes from later launches."""
        from PySide6.QtNetwork import QLocalServer

        QLocalServer.removeServer(_name())
        self.server = QLocalServer()
        self.server.newConnection.connect(self._accept)
        self.on_message = None
        return self.server.listen(_name())

    def connect(self, on_message):
        """Call ``on_message()`` per poke, including any that came in already."""
        self.on_message = on_message
        for _ in range(self.pokes):
            on_message()
        self.pokes = 0

    def _accept(self):
        connection = self.server.nextPendingConnection()
        if connection is None:
            return
        connection.disconnectFromServer()
        if self.on_message:
            self.on_message()
        else:
            self.pokes += 1

    @staticmethod
    def notify_running():
        """In a second copy: poke the first one."""
        from PySide6.QtNetwork import QLocalSocket

        socket = QLocalSocket()
        socket.connectToServer(_name())
        if socket.waitForConnected(1000):
            socket.disconnectFromServer()

"""One running copy per Windows user: a named mutex, plus a local socket that
lets a second launch tell the first one to say it is already running, or (``--quit``,
used by installs) to quit the way its tray menu does."""

import ctypes
import getpass
import sys
import time

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183


def _name():
    return "PredOverlay-" + "".join(c for c in getpass.getuser() if c.isalnum())


class Guard:
    def __init__(self):
        self.handle = None
        self.server = None
        self.pokes_seen = []  # messages of later launches before ``connect`` had a handler

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
        if error == ERROR_ALREADY_EXISTS:
            # Our handle would keep the mutex alive after the first copy exits,
            # so a retry (see app._acquire) could never succeed.
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None
            return False
        return True

    def release(self):
        """Let go of the mutex (a check that nobody else holds it)."""
        if self.handle and sys.platform == "win32":
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = None

    def listen(self):
        """In the first copy: start accepting pokes from later launches."""
        from PySide6.QtNetwork import QLocalServer

        QLocalServer.removeServer(_name())
        self.server = QLocalServer()
        self.server.newConnection.connect(self._accept)
        self.on_message = None
        return self.server.listen(_name())

    def connect(self, on_message):
        """Call ``on_message(message)`` per poke (b"" or b"quit"), including any that
        came in already."""
        self.on_message = on_message
        for message in self.pokes_seen:
            on_message(message)
        self.pokes_seen = []

    def _accept(self):
        connection = self.server.nextPendingConnection()
        if connection is None:
            return
        # A plain poke sends nothing and goes; a message comes right after the connect.
        from PySide6.QtNetwork import QLocalSocket

        deadline = time.monotonic() + 1.0
        while (
            not connection.bytesAvailable()
            and connection.state() == QLocalSocket.LocalSocketState.ConnectedState
            and time.monotonic() < deadline
        ):
            connection.waitForReadyRead(50)
        message = bytes(connection.readAll()).strip()
        connection.disconnectFromServer()
        if self.on_message:
            self.on_message(message)
        else:
            self.pokes_seen.append(message)

    @staticmethod
    def notify_running(message=b""):
        """In a second copy: poke the first one, with b"quit" to make it quit. True when
        the first copy took it."""
        from PySide6.QtNetwork import QLocalSocket

        socket = QLocalSocket()
        socket.connectToServer(_name())
        if not socket.waitForConnected(1000):
            return False
        if message:
            socket.write(message)
            socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
        return True

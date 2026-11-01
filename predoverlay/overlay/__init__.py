"""Overlay v2: the match roster from the Omeda game backend, no screen reading.

``core`` is a pure reducer over events, ``tracker`` runs it on one thread with
I/O lanes, ``window`` (PySide6) polls the immutable ``model.ViewModel``.
"""

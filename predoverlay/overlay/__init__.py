"""The overlay: the match roster read off the screen and the game log, looked up on Pred.gg.

``core`` is a pure reducer over events, ``tracker`` runs it on one thread with
I/O lanes, ``window`` (PySide6) polls the immutable ``model.ViewModel``.
"""

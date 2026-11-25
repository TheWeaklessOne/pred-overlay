"""What the overlay's name model can write (namereader), shared with the name matching.

The classes after the CTC blank, in training order (the training project's own list): later model
versions only append, so older ones keep their indices. GAP is the model's mark for a
glyph it does not write (an emoji, a symbol, CJK or Arabic letters, a missing-glyph box):
one GAP stands for a whole run of them.
"""

import string

GAP = "�"
CYRILLIC = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяІіЇїЄєҐґЎў"
CHARSET = (
    string.ascii_letters
    + string.digits
    + " _-."
    + "éèáàüöäñçóíúÉÜÖÄÑ"
    + "ÈÁÀÇÓÍÚ"
    + "".join(c for c in string.punctuation if c not in "_-.")
    + CYRILLIC  # v7
    + GAP  # v7
)
V6 = 119  # the v6 model's classes after the blank: everything before CYRILLIC

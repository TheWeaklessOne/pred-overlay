"""When the panel is shown: a pure function plus a small F8 toggle state."""

LOADING_SCREEN = 150  # fallback: seconds after the map load if the log shows no match start
START_GRACE = 20  # after the match start the loading screen can wait for slower players


def on_loading_screen(phase, phase_age, started_age=None):
    """The loading screen: until the match starts (plus a grace), at most LOADING_SCREEN."""
    if phase != "loading" or phase_age >= LOADING_SCREEN:
        return False
    return started_age is None or started_age < START_GRACE


def auto_visible(phase, phase_age, tab_held=False, started_age=None):
    """The panel shows in draft and on the loading screen; the menu shows the badge.

    In the match nothing shows by itself: holding TAB and pointing at a player
    on the scoreboard brings up that player's card (the window handles that).
    """
    if phase in ("draft", "menu"):
        return True
    return on_loading_screen(phase, phase_age, started_age)


class Visibility:
    """F8 flips what is currently shown until the next phase change.

    A hide also ends as soon as the panel would be hidden anyway (the loading
    screen is over, TAB is released), so the next TAB shows it again; a pin
    stays until F8 or the next phase.
    """

    def __init__(self):
        self.forced = None
        self.phase = None
        self.key_was_down = False

    def update(self, *, phase, phase_age, tab_held, f8_down, foreground, started_age=None):
        if phase != self.phase:
            self.phase, self.forced = phase, None
        auto = auto_visible(phase, phase_age, tab_held, started_age)
        if self.forced is False and not auto:
            self.forced = None
        if f8_down and not self.key_was_down and foreground:
            current = auto if self.forced is None else self.forced
            self.forced = not current
        self.key_was_down = f8_down
        if not foreground:
            return False
        return auto if self.forced is None else self.forced

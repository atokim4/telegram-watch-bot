"""Errors the engine reports back to the user instead of crashing on."""


class WatchError(Exception):
    """Base class: the message is safe to show to the watch owner."""


class FetchError(WatchError):
    pass


class RobotsDisallowedError(FetchError):
    pass


class ParseError(WatchError):
    pass

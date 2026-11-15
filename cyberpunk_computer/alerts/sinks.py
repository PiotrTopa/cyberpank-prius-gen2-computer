"""
Output sinks of the alert framework: sound and notifications.

Neither has a real implementation yet, on purpose:

* **Sound.** The MFD Pi has no audio, the head-unit agent (Android, could play
  sounds with music ducking) does not exist, and the meter buzzer (BEAN 62:6E)
  is on the BEAN deny list: nothing here transmits on BEAN. Alerts carry a
  sound id; the manager calls :meth:`SoundSink.play` when an alert in mode
  ``on`` is shown. :class:`NullSoundSink` records the request and does nothing.

* **Notifications.** There is no push channel to the owner's phone. Parked-car
  events in mode ``on`` go to :meth:`NotificationSink.notify`;
  :class:`NullNotificationSink` keeps them in memory (the dashboard lists them
  from the alert log either way).

Interface for a later sink (head-unit agent, piezo on a satellite or a
spare powerbox relay; a push bridge):

    class MySoundSink(SoundSink):
        name = "headunit"
        def play(self, sound_id, severity, alert_id) -> bool: ...   # non-blocking, True = accepted
        def stop(self, alert_id) -> None: ...                       # the alert cleared / was acknowledged

    class MyNotificationSink(NotificationSink):
        name = "push"
        def notify(self, event: dict) -> bool: ...   # non-blocking; event = the log entry

Both are called on the backend's engine thread and must return at once (queue
work to their own thread). Sound ids (``SOUND_IDS``) are abstract cues, not
files: a sink maps them to its own tones.
"""

from __future__ import annotations

import collections
import logging
import time
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)

#: Abstract cues an alert may name. A sink decides what each sounds like.
SOUND_IDS: Dict[str, str] = {
    "soft": "single soft chime (information, eco, maintenance)",
    "notice": "two-tone chime (speed limit, curve, stop-and-go, distance coach)",
    "warning": "double urgent chime (distance warning)",
}


class SoundSink:
    name = "abstract"

    def play(self, sound_id: str, severity: str, alert_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def stop(self, alert_id: str) -> None:
        """The alert that asked for the sound cleared or was acknowledged."""

    def status(self) -> Dict[str, Any]:
        return {"sink": self.name}


class NullSoundSink(SoundSink):
    """No sound output exists: remember what would have been played."""

    name = "null"

    def __init__(self, keep: int = 50) -> None:
        self.requests: Deque[Dict[str, Any]] = collections.deque(maxlen=keep)
        self.count = 0

    def play(self, sound_id: str, severity: str, alert_id: str) -> bool:
        self.count += 1
        self.requests.append({"ts": time.time(), "sound": sound_id, "severity": severity,
                              "alert": alert_id})
        return False

    def status(self) -> Dict[str, Any]:
        return {"sink": self.name, "available": False, "requests": self.count,
                "note": "no audio output on this system yet (MFD Pi has none; head-unit agent "
                        "not built; meter buzzer on the BEAN deny list)"}


class NotificationSink:
    name = "abstract"

    def notify(self, event: Dict[str, Any]) -> bool:  # pragma: no cover
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        return {"sink": self.name}


class NullNotificationSink(NotificationSink):
    """No push channel exists: keep the last events for the dashboard."""

    name = "null"

    def __init__(self, keep: int = 100) -> None:
        self.events: Deque[Dict[str, Any]] = collections.deque(maxlen=keep)
        self.count = 0

    def notify(self, event: Dict[str, Any]) -> bool:
        self.count += 1
        self.events.append(dict(event))
        return False

    def status(self) -> Dict[str, Any]:
        return {"sink": self.name, "available": False, "events": self.count,
                "note": "no push channel yet; events are on the dashboard (Alerts tab)"}


def make_sound_sink(kind: Optional[str] = None) -> SoundSink:
    """Only the null sink exists today."""
    if kind not in (None, "", "null"):
        logger.warning("alert sound sink %r not available; using the null sink", kind)
    return NullSoundSink()


def make_notification_sink(kind: Optional[str] = None) -> NotificationSink:
    if kind not in (None, "", "null"):
        logger.warning("alert notification sink %r not available; using the null sink", kind)
    return NullNotificationSink()

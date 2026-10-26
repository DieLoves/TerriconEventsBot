from __future__ import annotations

from enum import StrEnum


class EventKind(StrEnum):
    """Display kind derived from the source ``eventType``.

    Lectures, meetups and courses of the theme endpoints have no kind: they are
    ordinary events.
    """

    NECONF = "neconf"
    HACKATHON = "hackathon"
    ACCELERATOR = "accelerator"
    INCUBATOR = "incubator"
    COURSE = "course"


PROGRAM_KINDS = (EventKind.ACCELERATOR, EventKind.INCUBATOR, EventKind.COURSE)
KIND_ICONS = {
    EventKind.NECONF: "🎤",
    EventKind.HACKATHON: "🏆",
    EventKind.ACCELERATOR: "🚀",
    EventKind.INCUBATOR: "🌱",
    EventKind.COURSE: "🎓",
}

# Source spellings, lower-cased; ``OC`` is the source code for online courses.
_SOURCE_TYPES: dict[EventKind, tuple[str, ...]] = {
    EventKind.NECONF: ("neconf",),
    EventKind.HACKATHON: ("hackaton", "hackathon"),
    EventKind.ACCELERATOR: ("accelerator",),
    EventKind.INCUBATOR: ("incubator",),
    EventKind.COURSE: ("course", "oc"),
}
_KIND_BY_SOURCE_TYPE = {
    source_type: kind for kind, spellings in _SOURCE_TYPES.items() for source_type in spellings
}


def event_kind(event_type: str | None) -> EventKind | None:
    if event_type is None:
        return None
    return _KIND_BY_SOURCE_TYPE.get(event_type.strip().casefold())


def is_program(event_type: str | None) -> bool:
    return event_kind(event_type) in PROGRAM_KINDS


def source_types(*kinds: EventKind) -> tuple[str, ...]:
    return tuple(source_type for kind in kinds for source_type in _SOURCE_TYPES[kind])

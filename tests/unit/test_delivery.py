from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from terricon_events_bot.application.delivery import (
    DigestEntry,
    DigestRenderer,
    EventButton,
    QuietHours,
)
from terricon_events_bot.domain.enums import Locale, NotificationType
from terricon_events_bot.localization import LocalizationCatalog

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALMATY = ZoneInfo("Asia/Almaty")


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_quiet_hours_cross_midnight_and_release_at_almaty_nine() -> None:
    quiet = QuietHours(ALMATY, time(22), time(9))

    before = utc(2030, 1, 1, 16, 59)  # 21:59 Almaty
    evening = utc(2030, 1, 1, 17)  # 22:00 Almaty
    morning = utc(2030, 1, 2, 3, 59)  # 08:59 Almaty
    end = utc(2030, 1, 2, 4)  # 09:00 Almaty

    assert quiet.delivery_time(before) == before
    assert quiet.delivery_time(evening) == end
    assert quiet.delivery_time(morning) == end
    assert quiet.delivery_time(end) == end


def test_quiet_hours_reject_naive_clock_values() -> None:
    quiet = QuietHours(ALMATY, time(22), time(9))

    try:
        quiet.delivery_time(datetime(2030, 1, 1, 22))
    except ValueError as error:
        assert "timezone-aware" in str(error)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("Naive datetime must be rejected")


def test_digest_renderer_splits_messages_and_keeps_delivery_mapping() -> None:
    renderer = DigestRenderer(
        LocalizationCatalog.load(PROJECT_ROOT / "locales"),
        timezone=ALMATY,
        text_limit=90,
    )
    entries = (
        DigestEntry(
            event_id=1,
            delivery_ids=(10, 11),
            notification_type=NotificationType.NEW_EVENT,
            title="A" * 60,
            starts_at=utc(2030, 1, 1, 3),
            changed_fields=(),
        ),
        DigestEntry(
            event_id=2,
            delivery_ids=(12,),
            notification_type=NotificationType.NEW_EVENT,
            title="B" * 60,
            starts_at=utc(2030, 1, 2, 3),
            changed_fields=(),
        ),
        DigestEntry(
            event_id=3,
            delivery_ids=(13,),
            notification_type=NotificationType.CANCELLATION,
            title="Cancelled",
            starts_at=utc(2030, 1, 3, 3),
            changed_fields=("state",),
        ),
    )

    chunks = renderer.render(entries, Locale.RU)

    assert len(chunks) == 3
    assert all(utf16_length(chunk.text) <= 90 for chunk in chunks)
    assert tuple(item for chunk in chunks for item in chunk.delivery_ids) == (10, 11, 12, 13)
    assert chunks[0].text.startswith("🆕 Новые мероприятия")
    assert chunks[-1].text.startswith("✏️ Важные изменения")
    assert "Событие отменено" in chunks[-1].text


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_digest_limit_counts_telegram_utf16_units_for_emoji_titles() -> None:
    renderer = DigestRenderer(
        LocalizationCatalog.load(PROJECT_ROOT / "locales"),
        timezone=ALMATY,
        text_limit=90,
    )
    entry = DigestEntry(
        event_id=1,
        delivery_ids=(10,),
        notification_type=NotificationType.NEW_EVENT,
        title="🎉" * 80,
        starts_at=utc(2030, 1, 1, 3),
        changed_fields=(),
    )

    (chunk,) = renderer.render((entry,), Locale.RU)

    assert len(chunk.text) <= 90 < utf16_length("🎉" * 80)
    assert utf16_length(chunk.text) <= 90
    chunk.text.encode("utf-8")  # raises on a surrogate pair cut in half


def test_digest_entries_link_to_registration_but_not_cancellations() -> None:
    renderer = DigestRenderer(
        LocalizationCatalog.load(PROJECT_ROOT / "locales"),
        timezone=ALMATY,
    )
    entries = (
        DigestEntry(
            event_id=1,
            delivery_ids=(10,),
            notification_type=NotificationType.NEW_EVENT,
            title="Meetup",
            starts_at=utc(2030, 1, 1, 3),
            changed_fields=(),
            url="https://share.kz/abc",
        ),
        DigestEntry(
            event_id=2,
            delivery_ids=(11,),
            notification_type=NotificationType.CANCELLATION,
            title="Cancelled",
            starts_at=utc(2030, 1, 2, 3),
            changed_fields=("state",),
            url="https://share.kz/old",
        ),
    )

    text = "\n".join(chunk.text for chunk in renderer.render(entries, Locale.RU))

    assert "• Meetup\n📅 01.01.2030 08:00\n🔗 https://share.kz/abc" in text
    assert "https://share.kz/old" not in text


def digest_entry(
    event_id: int, title: str, notification_type: NotificationType = NotificationType.NEW_EVENT
) -> DigestEntry:
    return DigestEntry(
        event_id=event_id,
        delivery_ids=(event_id * 10,),
        notification_type=notification_type,
        title=title,
        starts_at=utc(2030, 1, 1, 3),
        changed_fields=("address",) if notification_type is not NotificationType.NEW_EVENT else (),
    )


def test_digest_offers_a_bot_button_for_each_event_that_still_has_a_card() -> None:
    renderer = DigestRenderer(LocalizationCatalog.load(PROJECT_ROOT / "locales"), timezone=ALMATY)

    (single, changes) = renderer.render(
        (
            digest_entry(1, "Meetup"),
            digest_entry(2, "Changed", NotificationType.IMPORTANT_CHANGE),
            digest_entry(5, "Cancelled", NotificationType.CANCELLATION),
        ),
        Locale.RU,
    )
    (several,) = renderer.render(
        (digest_entry(3, "Short"), digest_entry(4, "Very " * 20 + "long")), Locale.KZ
    )

    assert single.buttons == (EventButton(1, "📋 Открыть в боте"),)
    assert changes.buttons == (EventButton(2, "📋 Открыть в боте"),)
    assert [button.event_id for button in several.buttons] == [3, 4]
    assert several.buttons[0].label == "📋 Short"
    assert several.buttons[1].label.endswith("…")
    assert len(several.buttons[1].label) <= len("📋 ") + 40

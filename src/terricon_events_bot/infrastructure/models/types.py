from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def enum_type(enum_class: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(
        enum_class,
        name=name,
        native_enum=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )

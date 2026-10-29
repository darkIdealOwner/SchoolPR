"""Узбекские номера телефонов: в базе всегда +998XXXXXXXXX, на экране +998 (XX)-XXX-XX-XX."""
import re

UZ_PHONE_RE = re.compile(r'^\+998\d{9}$')


def normalize_phone(value):
    """'+998 (50)-002-21-31', '998500022131', '500022131' -> '+998500022131'."""
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 9:
        digits = '998' + digits
    return '+' + digits if digits else ''


def is_valid_uz_phone(value):
    return bool(UZ_PHONE_RE.match(value or ''))


def format_phone(value):
    """'+998500022131' -> '+998 (50)-002-21-31'; другие номера возвращаются как есть."""
    if not is_valid_uz_phone(value):
        return value
    d = value[4:]
    return f"+998 ({d[:2]})-{d[2:5]}-{d[5:7]}-{d[7:]}"

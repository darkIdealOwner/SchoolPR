from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def money(value):
    """1234567.50 -> «1 234 567,50», 2000.00 -> «2 000» (разделитель — неразрывный пробел)."""
    if value is None or value == '':
        return '—'
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return value
    sign = '−' if amount < 0 else ''
    amount = abs(amount).quantize(Decimal('0.01'))
    whole, frac = f"{amount:.2f}".split('.')
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    result = ' '.join(groups)
    if frac != '00':
        result += ',' + frac
    return sign + result


@register.filter
def money_round(value):
    """Крупные итоги без тийинов: 1255919194.56 -> «1 255 919 195»."""
    try:
        return money(Decimal(value).quantize(Decimal(1)))
    except (InvalidOperation, TypeError, ValueError):
        return money(value)


@register.filter
def phone(value):
    """+998500022131 -> «+998 (50)-002-21-31»."""
    from users.phone import format_phone
    return format_phone(value) if value else ''


@register.filter
def percent(value):
    return '—' if value is None else f"{value}%"

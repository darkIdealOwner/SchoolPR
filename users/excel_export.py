"""Выгрузка таблиц в оформленный файл Excel (.xlsx): шапка с названием, суммы с разрядами, разделы и итоги жирным.

Строка таблицы — обычный список значений. Её вид определяется так:
- Line(..., style=...) задаёт его явно: section (заголовок раздела), subheader (подписи колонок внутри таблицы),
  subtotal, total, info;
- иначе: строка из одной подписи — раздел, пустая — отступ, «Итого…» / «Всего…» / «Чистый…» в первой ячейке — итог.
Суммы (Decimal) — целыми, с разделителями разрядов (тийины остаются в ячейке), минус красным, ноль — «–»;
целые числа (год, срок, количество) — как есть; даты — ДД.ММ.ГГГГ.
"""
import io
import re
from datetime import date, datetime
from decimal import Decimal

from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext as _

TOTAL_PREFIXES = ('итого', 'всего', 'чистый', 'чистая', 'jami', 'total', 'net ', '합계', '총')
MONEY = '#,##0;[Red]-#,##0;"–"'
HEADER_FILL, SECTION_FILL, SUBHEADER_FILL, TOTAL_FILL = '1F3864', 'D9E1F2', 'EEF2F9', 'F2F2F2'


class Percent(float):
    """Доля (0.234) — в Excel процентом «23,4%», в CSV текстом."""

    def __str__(self):
        return f'{self * 100:.1f}%'.replace('.', ',')


class Line(list):
    """Строка выгрузки с явным видом."""

    def __init__(self, values=(), style=None):
        super().__init__(values)
        self.style = style


def row_style(row):
    style = getattr(row, 'style', None)
    if style:
        return style
    filled = [v for v in row if v not in (None, '')]
    if not filled:
        return 'blank'
    if len(row) == 1 and isinstance(row[0], str):
        return 'section'
    first = row[0]
    if isinstance(first, str) and first.strip().lower().startswith(TOTAL_PREFIXES):
        return 'total'
    return 'line'


def _cell_value(value):
    """Значение для ячейки и её числовой формат."""
    if value is None:
        return '', None
    if isinstance(value, bool):
        return (_('да') if value else _('нет')), None
    if isinstance(value, Percent):
        return float(value), '0.0%'
    if isinstance(value, Decimal):
        return float(value), MONEY
    if isinstance(value, datetime):
        return value.date(), 'DD.MM.YYYY'
    if isinstance(value, date):
        return value, 'DD.MM.YYYY'
    if isinstance(value, (int, float)):
        return value, None
    return str(value), None


def _width(value, fmt):
    if fmt == MONEY:
        return len(f'{abs(value):,.0f}') + 2
    if fmt == 'DD.MM.YYYY':
        return 11
    if fmt == '0.0%':
        return 8
    text = str(value)
    return max((len(part) for part in text.split('\n')), default=0) + 2


def build_workbook(title, subtitle, headers, rows, autofilter=False):
    """Книга Excel (bytes) с одним листом: название, подзаголовок, шапка таблицы, строки."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r'[\[\]:*?/\\]', ' ', str(title))[:31] or 'SchoolPR'
    columns = max([len(headers)] + [len(r) for r in rows] + [1])
    thin = Side(style='thin', color='BFBFBF')
    widths = [0] * columns

    ws.cell(row=1, column=1, value=str(title)).font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=str(subtitle)).font = Font(italic=True, color='595959')

    header_row = 4
    for col, label in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col, value=str(label) if label not in (None, '') else '')
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor=HEADER_FILL)
        cell.alignment = Alignment(horizontal='center' if col > 1 else 'left', vertical='center', wrap_text=True)
        cell.border = Border(bottom=thin)
        widths[col - 1] = max(widths[col - 1], min(len(str(label or '')) + 2, 24))
    ws.row_dimensions[header_row].height = 30

    for offset, row in enumerate(rows, start=1):
        r = header_row + offset
        style = row_style(row)
        if style == 'blank':
            continue
        for col in range(1, columns + 1):
            raw = row[col - 1] if col - 1 < len(row) else None
            value, fmt = _cell_value(raw)
            cell = ws.cell(row=r, column=col, value=value if value != '' else None)
            if fmt:
                cell.number_format = fmt
            if value != '' and not (style == 'section' and col == 1):
                widths[col - 1] = max(widths[col - 1], _width(value, fmt))
            if style == 'section':
                cell.font = Font(bold=True, color='1F3864')
                cell.fill = PatternFill('solid', fgColor=SECTION_FILL)
            elif style == 'subheader':
                cell.font = Font(bold=True)
                cell.fill = PatternFill('solid', fgColor=SUBHEADER_FILL)
                cell.border = Border(bottom=thin)
                if col > 1:
                    cell.alignment = Alignment(horizontal='center', wrap_text=True)
            elif style == 'total':
                cell.font = Font(bold=True)
                cell.fill = PatternFill('solid', fgColor=TOTAL_FILL)
                cell.border = Border(top=Side(style='medium', color='808080'))
            elif style == 'subtotal':
                cell.font = Font(bold=True)
                cell.border = Border(top=thin)
            elif style == 'info':
                cell.font = Font(italic=True, color='595959')

    for col, width in enumerate(widths, start=1):
        limit = 50 if col == 1 else 40
        ws.column_dimensions[ws.cell(row=header_row, column=col).column_letter].width = max(8, min(width, limit))
    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)
    if autofilter and rows:
        ws.auto_filter.ref = f'A{header_row}:{ws.cell(row=header_row + len(rows), column=columns).coordinate}'
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f'{header_row}:{header_row}'

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def xlsx_response(filename, title, subtitle, headers, rows, autofilter=False):
    stamp = timezone.localdate().strftime('%Y-%m-%d')
    response = HttpResponse(build_workbook(title, subtitle, headers, rows, autofilter),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}_{stamp}.xlsx"'
    return response

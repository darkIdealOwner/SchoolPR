"""Выгрузка в оформленный Excel (.xlsx): шапка, числа как числа, разделы и итоги, все списки и отчёты.

Пример — test_reports.ReportBase (вымышленные данные).
"""
import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.test import SimpleTestCase
from django.urls import reverse

from users.excel_export import MONEY, Line, Percent, build_workbook, row_style
from users.test_reports import ReportBase

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
D = Decimal


def sheet(response):
    return openpyxl.load_workbook(io.BytesIO(response.content)).active


def find(ws, text):
    for row in ws.iter_rows():
        for cell in row:
            if cell.value == text:
                return cell
    raise AssertionError(f'нет ячейки «{text}»')


class BuilderTests(SimpleTestCase):
    def build(self, headers, rows, **kw):
        return openpyxl.load_workbook(io.BytesIO(build_workbook('Отчёт', 'Подзаголовок', headers, rows, **kw))).active

    def test_title_subtitle_header(self):
        ws = self.build(['Статья', 'Сумма'], [['Аренда', D(300)]])
        self.assertEqual((ws['A1'].value, ws['A2'].value, ws['A4'].value, ws['B4'].value), ('Отчёт', 'Подзаголовок', 'Статья', 'Сумма'))
        self.assertTrue(ws['A1'].font.b)
        self.assertTrue(ws['A4'].font.b)
        self.assertEqual(ws.freeze_panes, 'B5')
        self.assertEqual(ws.title, 'Отчёт')

    def test_money_is_number_with_format(self):
        ws = self.build(['', 'Сумма'], [['Аренда', D('1234567.89')], ['Возврат', D(-5)], ['Пусто', D(0)]])
        self.assertEqual((ws['B5'].value, ws['B5'].number_format), (1234567.89, MONEY))
        self.assertEqual(ws['B6'].value, -5)
        self.assertIn('[Red]', MONEY)
        self.assertIn('"–"', MONEY)

    def test_ints_dates_bools(self):
        ws = self.build(['Год', 'Дата', 'Отменён'], [[2015, date(2026, 9, 1), True]])
        self.assertEqual((ws['A5'].value, ws['A5'].number_format), (2015, 'General'))     # год без разрядов
        self.assertEqual(ws['B5'].number_format, 'DD.MM.YYYY')
        self.assertEqual(ws['C5'].value, 'да')

    def test_percent(self):
        ws = self.build(['', '%'], [['Доход', Percent(0.234)]])
        self.assertEqual((ws['B5'].value, ws['B5'].number_format), (0.234, '0.0%'))
        self.assertEqual(str(Percent(0.234)), '23,4%')

    def test_styles(self):
        self.assertEqual(row_style(['Поступления']), 'section')
        self.assertEqual(row_style([]), 'blank')
        self.assertEqual(row_style(['Итого', D(1)]), 'total')
        self.assertEqual(row_style(['Всего поступило', D(1)]), 'total')
        self.assertEqual(row_style(['Аренда', D(1)]), 'line')
        self.assertEqual(row_style(Line(['Аренда', D(1)], 'subtotal')), 'subtotal')

    def test_section_and_total_formatting(self):
        ws = self.build(['', 'Июль'], [['Поступления'], ['Касса', D(5)], ['Итого', D(5)]])
        self.assertTrue(ws['A5'].font.b)
        self.assertEqual(ws['B5'].fill.fgColor.rgb[-6:], 'D9E1F2')   # полоса раздела на всю ширину
        self.assertFalse(ws['A6'].font.b)
        self.assertTrue(ws['A7'].font.b and ws['B7'].font.b)

    def test_blank_row_is_empty(self):
        ws = self.build(['', 'Сумма'], [['А', D(1)], [], ['Б', D(2)]])
        self.assertIsNone(ws['A6'].value)
        self.assertEqual(ws['A7'].value, 'Б')

    def test_column_widths_follow_content(self):
        ws = self.build(['', 'Сумма'], [['Очень длинное название статьи расходов', D(1234567890)]])
        self.assertGreater(ws.column_dimensions['A'].width, 30)
        self.assertGreaterEqual(ws.column_dimensions['B'].width, 13)

    def test_autofilter(self):
        ws = self.build(['№', 'Ученик'], [['1', 'А'], ['2', 'Б']], autofilter=True)
        self.assertEqual(ws.auto_filter.ref, 'A4:B6')

    def test_bad_sheet_title_characters(self):
        data = build_workbook('Долги: окт/2026 [все]', '', ['A'], [])
        self.assertNotIn(':', openpyxl.load_workbook(io.BytesIO(data)).active.title)


class ExportPagesTests(ReportBase):
    def get(self, name, user=None, **params):
        self.client.force_login(user or self.owner)
        return self.client.get(reverse(name), {'export': 'xlsx', **params})

    def test_all_lists_export_xlsx(self):
        for name, extra in (('contract_list', {}), ('payment_list', {'month': '2026-10'}), ('debt_list', {}),
                            ('payout_list', {}), ('transaction_list', {})):
            response = self.get(name, **extra)
            self.assertEqual(response['Content-Type'], XLSX, name)
            self.assertIn('.xlsx"', response['Content-Disposition'], name)
            self.assertTrue(sheet(response)['A1'].value, name)

    def test_all_reports_export_xlsx(self):
        for name in ('report_dashboard', 'report_summary', 'report_cash_methods', 'report_payment_control', 'report_debts',
                     'report_tariffs', 'report_pivots', 'report_founders'):
            response = self.get(name, year=self.year.pk)
            self.assertEqual(response['Content-Type'], XLSX, name)
            ws = sheet(response)
            self.assertTrue(ws['A1'].value and ws['A2'].value, name)

    def test_report_subtitle_has_school_and_year(self):
        ws = sheet(self.get('report_cash_methods', school=self.school.pk, year=self.year.pk))
        self.assertEqual(ws['A1'].value, 'Движение денежных средств')
        self.assertIn(self.school.name, ws['A2'].value)
        self.assertIn('2026–2027', ws['A2'].value)
        self.assertIn('выгружено', ws['A2'].value)

    def test_network_subtitle(self):
        self.assertIn('Вся сеть', sheet(self.get('report_dashboard'))['A2'].value)

    def test_month_in_subtitle(self):
        ws = sheet(self.get('report_debts', school=self.school.pk, month='2026-10'))
        self.assertIn('окт', ws['A2'].value.lower())

    def test_director_subtitle_is_own_school(self):
        ws = sheet(self.get('contract_list', user=self.director))
        self.assertIn(self.school.name, ws['A2'].value)

    def test_cash_methods_numbers_and_styles(self):
        ws = sheet(self.get('report_cash_methods', school=self.school.pk))
        income = find(ws, 'Поступления')
        self.assertTrue(income.font.b)
        total = ws.cell(row=income.row + 4, column=1)
        self.assertEqual(total.value, 'Итого')
        self.assertTrue(total.font.b)
        self.assertEqual(ws.cell(row=total.row, column=14).value, 7470)
        self.assertEqual(ws.cell(row=total.row, column=14).number_format, MONEY)

    def test_dashboard_percent_column(self):
        ws = sheet(self.get('report_dashboard', school=self.school.pk))
        income = find(ws, 'Доход')
        self.assertEqual(ws.cell(row=income.row, column=3).value, 1.0)
        self.assertEqual(ws.cell(row=income.row, column=3).number_format, '0.0%')
        self.assertTrue(find(ws, 'Банк и касса').font.b)

    def test_contract_list_values_typed(self):
        ws = sheet(self.get('contract_list', school=self.school.pk))
        number = find(ws, '10')
        row = [c.value for c in ws[number.row]]
        self.assertIn('Ученик Десятый', row)
        self.assertIn(1000, row)          # стоимость — число
        self.assertTrue(ws.auto_filter.ref)

    def test_investor_export_has_no_personal_data(self):
        from users.models import Contract
        Contract.objects.filter(pk=self.c1.pk).update(parents_full_name='Родитель Секретный')
        ws = sheet(self.get('contract_list', user=self.investor))
        values = {c.value for row in ws.iter_rows() for c in row}
        self.assertNotIn('Родитель Секретный', values)

    def test_csv_still_available(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('report_cash_methods'), {'export': 'csv'})
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')

    def test_buttons_download_xlsx(self):
        self.client.force_login(self.owner)
        for name in ('contract_list', 'payment_list', 'debt_list', 'payout_list', 'transaction_list', 'report_dashboard'):
            body = self.client.get(reverse(name)).content.decode()
            self.assertIn('export=xlsx', body, name)
            self.assertNotIn('export=csv', body, name)

    def test_unknown_export_shows_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('contract_list'), {'export': 'pdf'})
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')

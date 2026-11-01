import json

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import redirect
from django.views.generic import FormView

from users.models import Contract, Payment, School, User

from .importers import run_import
from .models import SyncRun
from django.utils.translation import gettext, gettext_lazy as _


class ImportFileForm(forms.Form):
    entity = forms.ChoiceField(label=_("Что загружаем"), choices=SyncRun.ENTITY_CHOICES)
    file = forms.FileField(label=_("JSON-файл из 1С"))
    dry_run = forms.BooleanField(label=_("Пробный запуск (только проверить, ничего не сохранять)"), required=False, initial=True)

    def clean_file(self):
        uploaded = self.cleaned_data['file']
        if uploaded.size > 20 * 1024 * 1024:
            raise forms.ValidationError(_("Файл больше 20 МБ — разбейте выгрузку на части."))
        try:
            data = json.loads(uploaded.read().decode('utf-8-sig'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise forms.ValidationError(_("Это не JSON: %(error)s") % {'error': exc})
        if isinstance(data, dict):
            data = data.get('records')
        if not isinstance(data, list):
            raise forms.ValidationError(_('В файле должен быть список записей или {"records": [...]}'))
        return data


class IntegrationView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = 'home/integrations/integration.html'
    form_class = ImportFileForm

    def test_func(self):
        return self.request.user.role == 'OWNER'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        def linked(qs):
            return {'linked': qs.filter(external_id__isnull=False).count(), 'total': qs.count()}

        context.update({
            'odata_configured': bool(settings.ONEC_ODATA_URL),
            'api_configured': bool(settings.ONEC_API_TOKEN),
            'link_stats': [
                (_('Школы ↔ подразделения 1С'), linked(School.objects.all())),
                (_('Сотрудники ↔ физлица 1С'), linked(User.objects.filter(role='EMPLOYEE'))),
                (_('Договоры ↔ договоры контрагентов 1С'), linked(Contract.objects.all())),
            ],
            'payments_from_1c': Payment.objects.filter(source=Payment.SOURCE_1C).count(),
            'payments_not_in_1c': Payment.active.filter(source=Payment.SOURCE_MANUAL, external_id__isnull=True).count(),
            'runs': SyncRun.objects.select_related('triggered_by')[:20],
        })
        return context

    def form_valid(self, form):
        run = run_import(form.cleaned_data['entity'], form.cleaned_data['file'], channel='file',
                         dry_run=form.cleaned_data['dry_run'], user=self.request.user)
        summary = gettext(
            "%(entity)s: всего %(total)s, создано %(created)s, обновлено %(updated)s, "
            "сторнировано %(reversed)s, без изменений %(skipped)s, ошибок %(failed)s."
        ) % {'entity': run.get_entity_display(), 'total': run.total, 'created': run.created, 'updated': run.updated,
             'reversed': run.reversed, 'skipped': run.skipped, 'failed': run.failed}
        if run.dry_run:
            summary = gettext("Пробный запуск, ничего не сохранено.") + " " + summary
        (messages.success if run.status == 'SUCCESS' else messages.error)(self.request, summary)
        return redirect('integration')

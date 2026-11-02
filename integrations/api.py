"""HTTP API для 1С.

1С (HTTP-запросами из обработки или регламентного задания) может:
  - забирать договоры, созданные на платформе, и возвращать GUID созданных у себя договоров;
  - забирать оплаты, принятые на платформе (наличные в школе), чтобы провести их у себя;
  - отправлять свои документы (оплаты, выплаты, расходы) — так же, как при загрузке файлом.

Каждый запрос — с заголовком  Authorization: Token <ONEC_API_TOKEN>.
"""
import hmac
from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import AuditLog, Contract, Payment

from .importers import DOCUMENTS, LINKS, run_import


class OneCTokenAuthentication(BaseAuthentication):
    keyword = b'token'

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword:
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Неверный заголовок Authorization")
        expected = settings.ONEC_API_TOKEN
        if not expected:
            raise exceptions.AuthenticationFailed("API для 1С выключено: не задан ONEC_API_TOKEN")
        if not hmac.compare_digest(parts[1].decode('utf-8', errors='replace'), expected):
            raise exceptions.AuthenticationFailed("Неверный токен")
        return AnonymousUser(), 'onec'

    def authenticate_header(self, request):
        return 'Token'


class HasOneCToken(BasePermission):
    def has_permission(self, request, view):
        return request.auth == 'onec'


class OneCAPIView(APIView):
    authentication_classes = [OneCTokenAuthentication]
    permission_classes = [HasOneCToken]


def _parse_since(request):
    value = request.query_params.get('updated_since')
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise exceptions.ValidationError({'updated_since': "ожидается дата/время ISO 8601, например 2026-10-01T00:00:00"})
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _money(value):
    return format(value, 'f')


class PingView(OneCAPIView):
    def get(self, request):
        return Response({'status': 'ok', 'time': timezone.now().isoformat()})


class ContractListAPI(OneCAPIView):
    """Договоры платформы. ?updated_since=ISO — только изменённые; ?unlinked=1 — ещё не связанные с 1С."""

    def get(self, request):
        qs = Contract.objects.select_related('school').order_by('updated_at', 'pk')
        since = _parse_since(request)
        if since:
            qs = qs.filter(updated_at__gte=since)
        if request.query_params.get('unlinked') == '1':
            qs = qs.filter(external_id__isnull=True)
        data = [{
            'id': c.pk,
            'external_id': c.external_id,
            'number': c.number,
            'date': c.date.isoformat(),
            'school_id': c.school_id,
            'school_external_id': c.school.external_id if c.school else None,
            'school_name': c.school.name if c.school else None,
            'student_full_name': c.student_full_name,
            'student_birth_year': c.student_birth_year,
            'student_class': c.student_class,
            'parents_full_name': c.parents_full_name,
            'passport_data': c.passport_data,
            'inps': c.inps,
            'address': c.address,
            'phone_number': c.phone_number,
            'specialization': c.specialization,
            'duration_months': c.duration_months,
            'monthly_amount': _money(c.total_amount),
            'is_cancelled': c.is_cancelled,
            'cancelled_at': c.cancelled_at.isoformat() if c.cancelled_at else None,
            'updated_at': c.updated_at.isoformat(),
        } for c in qs]
        return Response({'count': len(data), 'results': data})


class ContractLinkAPI(OneCAPIView):
    """1С сообщает GUID договора, созданного у себя: POST {"external_id": "..."}."""

    def post(self, request, pk):
        contract = get_object_or_404(Contract, pk=pk)
        external_id = str(request.data.get('external_id') or '').strip()
        if not external_id:
            raise exceptions.ValidationError({'external_id': "обязательное поле"})
        taken = Contract.objects.filter(external_id=external_id).exclude(pk=contract.pk).first()
        if taken:
            raise exceptions.ValidationError({'external_id': f"уже привязан к договору {taken.number}"})
        old = contract.external_id
        if old != external_id:
            contract.external_id = external_id
            contract.synced_at = timezone.now()
            contract.save(update_fields=['external_id', 'synced_at', 'updated_at'])
            AuditLog.record(None, 'UPDATE', contract, {'external_id': [old, external_id], 'source': '1C'})
        return Response({'id': contract.pk, 'external_id': contract.external_id})


class PaymentListAPI(OneCAPIView):
    """Оплаты, принятые на платформе (для проведения в 1С). ?updated_since=ISO, ?unlinked=1."""

    def get(self, request):
        qs = Payment.objects.filter(source=Payment.SOURCE_MANUAL).select_related('contract', 'contract__school') \
            .order_by('updated_at', 'pk')
        since = _parse_since(request)
        if since:
            qs = qs.filter(updated_at__gte=since)
        if request.query_params.get('unlinked') == '1':
            qs = qs.filter(external_id__isnull=True)
        data = [{
            'id': p.pk,
            'external_id': p.external_id,
            'contract_id': p.contract_id,
            'contract_number': p.contract.number,
            'contract_external_id': p.contract.external_id,
            'school_external_id': p.contract.school.external_id if p.contract.school else None,
            'payment_date': p.payment_date.isoformat(),
            'for_month': p.for_month.strftime('%Y-%m'),
            'amount': _money(p.amount),
            'kind': p.kind,   # PAYMENT — оплата, REFUND — возврат родителю
            'method': p.method,
            'notes': p.notes,
            'is_reversed': p.is_reversed,
            'reversal_reason': p.reversal_reason,
            'updated_at': p.updated_at.isoformat(),
        } for p in qs]
        return Response({'count': len(data), 'results': data})


class PaymentLinkAPI(OneCAPIView):
    """1С сообщает GUID документа, которым провела оплату платформы: POST {"external_id": "..."}."""

    def post(self, request, pk):
        payment = get_object_or_404(Payment, pk=pk)
        external_id = str(request.data.get('external_id') or '').strip()
        if not external_id:
            raise exceptions.ValidationError({'external_id': "обязательное поле"})
        if Payment.objects.filter(external_id=external_id).exclude(pk=payment.pk).exists():
            raise exceptions.ValidationError({'external_id': "этот GUID уже привязан к другой оплате"})
        if payment.external_id != external_id:
            old = payment.external_id
            payment.external_id = external_id
            payment.synced_at = timezone.now()
            payment.save(update_fields=['external_id', 'synced_at', 'updated_at'])
            AuditLog.record(None, 'UPDATE', payment, {'external_id': [old, external_id], 'source': '1C'})
        return Response({'id': payment.pk, 'external_id': payment.external_id})


class ImportAPI(OneCAPIView):
    """Приём документов из 1С. Тело: список записей или {"records": [...], "dry_run": true}."""

    def post(self, request, entity):
        if entity not in DOCUMENTS and entity not in LINKS:
            raise exceptions.NotFound(f"Неизвестный тип данных: {entity}")
        body = request.data
        dry_run = False
        if isinstance(body, dict):
            dry_run = bool(body.get('dry_run'))
            body = body.get('records')
        if not isinstance(body, list):
            raise exceptions.ValidationError("ожидается список записей или {\"records\": [...]}")
        run = run_import(entity, body, channel='api', dry_run=dry_run)
        return Response({
            'run_id': run.pk,
            'status': run.status,
            'dry_run': run.dry_run,
            'total': run.total,
            'created': run.created,
            'updated': run.updated,
            'reversed': run.reversed,
            'skipped': run.skipped,
            'failed': run.failed,
            'errors': run.errors,
        }, status=200 if run.status != 'FAILED' else 422)

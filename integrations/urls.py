from django.urls import path

from . import api
from .views import IntegrationView

urlpatterns = [
    path('integrations/', IntegrationView.as_view(), name='integration'),

    path('api/1c/ping/', api.PingView.as_view(), name='api_1c_ping'),
    path('api/1c/contracts/', api.ContractListAPI.as_view(), name='api_1c_contracts'),
    path('api/1c/contracts/<int:pk>/link/', api.ContractLinkAPI.as_view(), name='api_1c_contract_link'),
    path('api/1c/payments/', api.PaymentListAPI.as_view(), name='api_1c_payments'),
    path('api/1c/payments/<int:pk>/link/', api.PaymentLinkAPI.as_view(), name='api_1c_payment_link'),
    path('api/1c/import/<str:entity>/', api.ImportAPI.as_view(), name='api_1c_import'),
]

from django.urls import path
from django.contrib.auth.views import LogoutView
from .views import (
    CustomLoginView, HomeView, ProfileView, CustomPasswordChangeView,
    UserListView, UserCreateView, UserUpdateView, UserDeleteView,
    SchoolListView, SchoolCreateView, SchoolUpdateView, SchoolDeleteView,
    ContractListView, ContractDetailView, ContractCreateView, ContractUpdateView, ContractDeleteView,
    PaymentListView, PaymentCreateView, PaymentReverseView, DebtListView,
    EmployeePayoutListView, EmployeePayoutCreateView, EmployeePayoutUpdateView, EmployeePayoutReverseView,
    TransactionListView, TransactionCreateView, TransactionUpdateView, TransactionReverseView,
    ExcelImportView, ExcelTemplateView,
    AcademicYearListView, AcademicYearCreateView, AcademicYearUpdateView,
    AccountListView, AccountCreateView, AccountUpdateView, TransferListView, TransferCreateView, TransferReverseView,
    CategoryListView, CategoryCreateView, CategoryUpdateView,
    CounterpartyListView, CounterpartyCreateView, CounterpartyUpdateView,
    RefundCreateView, ReceiptListView, ReceiptCreateView, ReceiptResolveView, ReceiptReverseView,
)

urlpatterns = [
    path('login/', CustomLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('home/', HomeView.as_view(), name='home'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('password-change/', CustomPasswordChangeView.as_view(), name='password_change'),
    
    path('users/', UserListView.as_view(), name='user_list'),
    path('users/add/', UserCreateView.as_view(), name='user_add'),
    path('users/<int:pk>/edit/', UserUpdateView.as_view(), name='user_edit'),
    path('users/<int:pk>/delete/', UserDeleteView.as_view(), name='user_delete'),
 
    path('schools/', SchoolListView.as_view(), name='school_list'),
    path('schools/add/', SchoolCreateView.as_view(), name='school_add'),
    path('schools/<int:pk>/edit/', SchoolUpdateView.as_view(), name='school_edit'),
    path('schools/<int:pk>/delete/', SchoolDeleteView.as_view(), name='school_delete'),
    path('academic-years/', AcademicYearListView.as_view(), name='academic_year_list'),
    path('academic-years/add/', AcademicYearCreateView.as_view(), name='academic_year_add'),
    path('academic-years/<int:pk>/edit/', AcademicYearUpdateView.as_view(), name='academic_year_edit'),
 
    path('contracts/', ContractListView.as_view(), name='contract_list'),
    path('contracts/add/', ContractCreateView.as_view(), name='contract_add'),
    path('contracts/<int:pk>/', ContractDetailView.as_view(), name='contract_detail'),
    path('contracts/<int:pk>/edit/', ContractUpdateView.as_view(), name='contract_edit'),
    path('contracts/<int:pk>/delete/', ContractDeleteView.as_view(), name='contract_delete'),

    path('payments/', PaymentListView.as_view(), name='payment_list'),
    path('payments/add/', PaymentCreateView.as_view(), name='payment_create'),
    path('payments/<int:pk>/reverse/', PaymentReverseView.as_view(), name='payment_reverse'),
    
    path('debts/', DebtListView.as_view(), name='debt_list'),

    path('payouts/', EmployeePayoutListView.as_view(), name='payout_list'),
    path('payouts/add/', EmployeePayoutCreateView.as_view(), name='payout_create'),
    path('payouts/<int:pk>/edit/', EmployeePayoutUpdateView.as_view(), name='payout_edit'),
    path('payouts/<int:pk>/reverse/', EmployeePayoutReverseView.as_view(), name='payout_reverse'),

    path('transactions/', TransactionListView.as_view(), name='transaction_list'),
    path('transactions/add/', TransactionCreateView.as_view(), name='transaction_create'),
    path('transactions/<int:pk>/edit/', TransactionUpdateView.as_view(), name='transaction_edit'),
    path('transactions/<int:pk>/reverse/', TransactionReverseView.as_view(), name='transaction_reverse'),

    path('contracts/<int:pk>/refund/', RefundCreateView.as_view(), name='refund_create'),
    path('receipts/', ReceiptListView.as_view(), name='receipt_list'),
    path('receipts/add/', ReceiptCreateView.as_view(), name='receipt_create'),
    path('receipts/<int:pk>/resolve/', ReceiptResolveView.as_view(), name='receipt_resolve'),
    path('receipts/<int:pk>/reverse/', ReceiptReverseView.as_view(), name='receipt_reverse'),
    path('accounts/', AccountListView.as_view(), name='account_list'),
    path('accounts/add/', AccountCreateView.as_view(), name='account_add'),
    path('accounts/<int:pk>/edit/', AccountUpdateView.as_view(), name='account_edit'),
    path('transfers/', TransferListView.as_view(), name='transfer_list'),
    path('transfers/add/', TransferCreateView.as_view(), name='transfer_create'),
    path('transfers/<int:pk>/reverse/', TransferReverseView.as_view(), name='transfer_reverse'),
    path('categories/', CategoryListView.as_view(), name='category_list'),
    path('categories/add/', CategoryCreateView.as_view(), name='category_add'),
    path('categories/<int:pk>/edit/', CategoryUpdateView.as_view(), name='category_edit'),
    path('counterparties/', CounterpartyListView.as_view(), name='counterparty_list'),
    path('counterparties/add/', CounterpartyCreateView.as_view(), name='counterparty_add'),
    path('counterparties/<int:pk>/edit/', CounterpartyUpdateView.as_view(), name='counterparty_edit'),

    path('import/<slug:kind>/', ExcelImportView.as_view(), name='excel_import'),
    path('import/<slug:kind>/template/', ExcelTemplateView.as_view(), name='excel_template'),
]

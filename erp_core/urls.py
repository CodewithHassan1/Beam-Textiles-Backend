from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    AccountViewSet, PartnerViewSet, JournalEntryViewSet, 
    InventoryItemViewSet, StockTransactionViewSet, InvoiceViewSet, 
    BankStatementLineViewSet, DashboardStatsView, TrialBalanceView, 
    ProfitAndLossView, BalanceSheetView, SignUpView, LoginView
)

router = DefaultRouter()
router.register(r'accounts', AccountViewSet, basename='account')
router.register(r'partners', PartnerViewSet, basename='partner')
router.register(r'journals', JournalEntryViewSet, basename='journal')
router.register(r'inventory', InventoryItemViewSet, basename='inventory')
router.register(r'stock-transactions', StockTransactionViewSet, basename='stock-transaction')
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'bank-statements', BankStatementLineViewSet, basename='bank-statement')

urlpatterns = [
    # Router endpoints
    path('', include(router.urls)),

    # Custom auth endpoints
    path('auth/signup/', SignUpView.as_view(), name='auth-signup'),
    path('auth/login/', LoginView.as_view(), name='auth-login'),

    # Custom dashboard and reporting endpoints
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('reports/trial-balance/', TrialBalanceView.as_view(), name='report-trial-balance'),
    path('reports/profit-loss/', ProfitAndLossView.as_view(), name='report-profit-loss'),
    path('reports/balance-sheet/', BalanceSheetView.as_view(), name='report-balance-sheet'),
]

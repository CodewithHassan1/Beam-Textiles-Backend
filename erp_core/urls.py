from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    JournalEntryViewSet, BankStatementLineViewSet, DashboardStatsView, TrialBalanceView,
    ProfitAndLossView, BalanceSheetView, SignUpView, LoginView,
    MeView, ActivityLogViewSet
)

router = DefaultRouter()
router.register(r'journals', JournalEntryViewSet, basename='journal')
router.register(r'bank-statements', BankStatementLineViewSet, basename='bank-statement')
router.register(r'activity-logs', ActivityLogViewSet, basename='activity-log')

urlpatterns = [
    # Router endpoints
    path('', include(router.urls)),

    # Custom auth endpoints
    path('auth/signup/', SignUpView.as_view(), name='auth-signup'),
    path('auth/login/', LoginView.as_view(), name='auth-login'),
    path('auth/me/', MeView.as_view(), name='auth-me'),

    # Custom dashboard and reporting endpoints
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('reports/trial-balance/', TrialBalanceView.as_view(), name='report-trial-balance'),
    path('reports/profit-loss/', ProfitAndLossView.as_view(), name='report-profit-loss'),
    path('reports/balance-sheet/', BalanceSheetView.as_view(), name='report-balance-sheet'),
]

from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.db.models import ProtectedError
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.utils.dateparse import parse_date
from decimal import Decimal
import datetime

from .models import (
    Account, Partner, JournalEntry, TransactionLine, 
    InventoryItem, StockTransaction, Invoice, InvoiceLine, BankStatementLine
)
from .serializers import (
    AccountSerializer, PartnerSerializer, JournalEntrySerializer, 
    InventoryItemSerializer, StockTransactionSerializer, InvoiceSerializer, 
    BankStatementLineSerializer
)
from .utils import (
    process_invoice_posting, get_or_create_default_accounts,
    RECEIVABLES_CODE, PAYABLES_CODE, INVENTORY_ASSET_CODE,
    INPUT_TAX_CODE, OUTPUT_TAX_CODE, REVENUE_CODE, COGS_CODE
)

class AccountViewSet(viewsets.ModelViewSet):
    queryset = Account.objects.all().order_by('code')
    serializer_class = AccountSerializer

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "Cannot delete account: it is referenced by transactions or other records."},
                status=status.HTTP_400_BAD_REQUEST
            )


class PartnerViewSet(viewsets.ModelViewSet):
    queryset = Partner.objects.all().order_by('name')
    serializer_class = PartnerSerializer

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "Cannot delete partner: it is referenced by transactions or other records."},
                status=status.HTTP_400_BAD_REQUEST
            )


class JournalEntryViewSet(viewsets.ModelViewSet):
    queryset = JournalEntry.objects.all().order_by('-date', '-id')
    serializer_class = JournalEntrySerializer

    @action(detail=True, methods=['post'], url_path='post')
    def post_journal(self, request, pk=None):
        journal = self.get_object()
        try:
            journal.post_entry()
            return Response({'status': 'Journal entry posted successfully'}, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            raise DRFValidationError(e.message)


class InventoryItemViewSet(viewsets.ModelViewSet):
    queryset = InventoryItem.objects.all().order_by('sku')
    serializer_class = InventoryItemSerializer

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "Cannot delete inventory item: it has stock movements or is in invoices."},
                status=status.HTTP_400_BAD_REQUEST
            )


class StockTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = StockTransaction.objects.all().order_by('-timestamp')
    serializer_class = StockTransactionSerializer


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all().order_by('-issue_date', '-id')
    serializer_class = InvoiceSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status == 'Posted' or instance.status == 'Paid':
            return Response(
                {"detail": "Cannot delete a posted or paid invoice."},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "Cannot delete invoice: it is referenced by other records."},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'], url_path='post')
    def post_invoice(self, request, pk=None):
        try:
            journal_entry = process_invoice_posting(pk)
            return Response({
                'status': 'Invoice posted successfully',
                'journal_entry_id': journal_entry.id
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            raise DRFValidationError(e.message)


class BankStatementLineViewSet(viewsets.ModelViewSet):
    queryset = BankStatementLine.objects.all().order_by('date', 'id')
    serializer_class = BankStatementLineSerializer

    @action(detail=False, methods=['post'], url_path='import')
    def import_mock_statements(self, request):
        """
        Seed mock bank transactions for demonstration and testing.
        """
        statements = [
            {"date": "2026-05-10", "description": "ACH Customer Deposit Co.", "amount": "1150.00"},
            {"date": "2026-05-12", "description": "Supplier Raw Materials Inv-10", "amount": "-575.00"},
            {"date": "2026-05-15", "description": "Office Supplies Depot", "amount": "-120.00"},
            {"date": "2026-05-20", "description": "Customer Payment Ref #2201", "amount": "2300.00"},
            {"date": "2026-05-22", "description": "Monthly Web Hosting Services", "amount": "-45.00"},
        ]
        created = []
        with transaction.atomic():
            for item in statements:
                line, created_flag = BankStatementLine.objects.get_or_create(
                    date=parse_date(item["date"]),
                    description=item["description"],
                    amount=Decimal(item["amount"]),
                    defaults={"reconciled": False}
                )
                if created_flag:
                    created.append(line)
        
        serializer = self.get_serializer(created, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='auto-match')
    def auto_match(self, request):
        """
        Reconciliation engine: Automatically matches BankStatementLines to general ledger
        TransactionLines with matching amounts and date proximity (+/- 7 days).
        """
        unreconciled_lines = BankStatementLine.objects.filter(reconciled=False)
        matched_count = 0

        # We assume cash/bank general ledger transactions correspond to Accounts Receivable,
        # Accounts Payable, or standard bank ledger accounts (e.g. 10100 Cash/Bank Asset account)
        # Let's search any posted transaction line whose value matches
        for statement in unreconciled_lines:
            target_amount = abs(statement.amount)
            # Find date bounds (+/- 7 days)
            start_date = statement.date - datetime.timedelta(days=7)
            end_date = statement.date + datetime.timedelta(days=7)

            # Query candidate TransactionLines that are posted, unreconciled, and within proximity
            candidates = TransactionLine.objects.filter(
                journal_entry__posted=True,
                journal_entry__date__range=(start_date, end_date),
                bankstatementline__isnull=True  # Ensure it hasn't been matched yet
            )

            # Filter by matching credit or debit depending on sign
            if statement.amount > 0:
                # Receipt -> should match a debit to Cash/Bank account (increasing asset)
                # or a credit to Accounts Receivable (reducing customer balance)
                # We search lines with debit or credit of matching amount
                candidates = candidates.filter(debit=target_amount)
            else:
                # Withdrawal -> should match a credit to Cash/Bank (decreasing asset)
                # or a debit to Accounts Payable (reducing supplier liability)
                candidates = candidates.filter(credit=target_amount)

            match = candidates.first()
            if match:
                statement.matched_transaction_line = match
                statement.reconciled = True
                statement.save()
                matched_count += 1

        return Response({
            'status': 'Reconciliation run completed',
            'matched_records_count': matched_count
        }, status=status.HTTP_200_OK)


class DashboardStatsView(APIView):
    def get(self, request):
        # 1. Total Receivables (Receivable Account Balance)
        try:
            ar_acc = Account.objects.get(code=RECEIVABLES_CODE)
            receivables = ar_acc.balance
        except Account.DoesNotExist:
            receivables = Decimal('0.00')

        # 2. Total Payables (Payable Account Balance)
        try:
            ap_acc = Account.objects.get(code=PAYABLES_CODE)
            payables = ap_acc.balance
        except Account.DoesNotExist:
            payables = Decimal('0.00')

        # 3. Cash & Bank Balance (Let's sum cash/bank accounts, defaulting to a custom code '10100' or default remaining cash)
        # We can dynamically sum all Asset accounts that are not AR or Inventory
        cash_assets = Account.objects.filter(account_type='Asset').exclude(code__in=['11000', '12000'])
        cash_balance = sum(acc.balance for acc in cash_assets)

        # 4. Inventory Valuation
        inventory_valuation = sum(item.stock_qty * item.avg_unit_cost for item in InventoryItem.objects.all())

        # 5. Net Profit (Revenue - Expenses)
        revenue_sum = sum(acc.balance for acc in Account.objects.filter(account_type='Revenue'))
        expense_sum = sum(acc.balance for acc in Account.objects.filter(account_type='Expense'))
        net_profit = revenue_sum - expense_sum

        # Recent transactions
        recent_entries = JournalEntry.objects.all().order_by('-date', '-id')[:5]
        entries_data = []
        for entry in recent_entries:
            entries_data.append({
                'id': entry.id,
                'date': entry.date,
                'description': entry.description,
                'reference': entry.reference,
                'posted': entry.posted,
                'total_amount': sum(line.debit for line in entry.lines.all())
            })

        return Response({
            'receivables': receivables,
            'payables': payables,
            'cash_balance': cash_balance,
            'inventory_valuation': inventory_valuation,
            'net_profit': net_profit,
            'recent_entries': entries_data
        })


class TrialBalanceView(APIView):
    def get(self, request):
        get_or_create_default_accounts()  # Make sure accounts exist
        accounts = Account.objects.all().order_by('code')
        tb_lines = []
        total_debit = Decimal('0.00')
        total_credit = Decimal('0.00')

        for acc in accounts:
            debit = Decimal('0.00')
            credit = Decimal('0.00')

            # Show debit or credit based on the account type and its balance
            if acc.account_type in ['Asset', 'Expense']:
                if acc.balance >= 0:
                    debit = acc.balance
                else:
                    credit = abs(acc.balance)
            else:  # Liability, Equity, Revenue
                if acc.balance >= 0:
                    credit = acc.balance
                else:
                    debit = abs(acc.balance)

            tb_lines.append({
                'code': acc.code,
                'name': acc.name,
                'account_type': acc.account_type,
                'debit': debit,
                'credit': credit
            })
            total_debit += debit
            total_credit += credit

        return Response({
            'lines': tb_lines,
            'total_debit': total_debit,
            'total_credit': total_credit
        })


class ProfitAndLossView(APIView):
    def get(self, request):
        get_or_create_default_accounts()
        revenue_accounts = Account.objects.filter(account_type='Revenue')
        expense_accounts = Account.objects.filter(account_type='Expense')

        revenue_lines = []
        total_revenue = Decimal('0.00')
        for acc in revenue_accounts:
            revenue_lines.append({'code': acc.code, 'name': acc.name, 'amount': acc.balance})
            total_revenue += acc.balance

        expense_lines = []
        total_expense = Decimal('0.00')
        for acc in expense_accounts:
            expense_lines.append({'code': acc.code, 'name': acc.name, 'amount': acc.balance})
            total_expense += acc.balance

        net_income = total_revenue - total_expense

        return Response({
            'revenues': revenue_lines,
            'total_revenue': total_revenue,
            'expenses': expense_lines,
            'total_expense': total_expense,
            'net_income': net_income
        })


class BalanceSheetView(APIView):
    def get(self, request):
        get_or_create_default_accounts()
        assets = Account.objects.filter(account_type='Asset')
        liabilities = Account.objects.filter(account_type='Liability')
        equity = Account.objects.filter(account_type='Equity')

        # Add P&L Net income to Equity for current balance sheet
        revenue_sum = sum(acc.balance for acc in Account.objects.filter(account_type='Revenue'))
        expense_sum = sum(acc.balance for acc in Account.objects.filter(account_type='Expense'))
        net_income = revenue_sum - expense_sum

        asset_lines = []
        total_assets = Decimal('0.00')
        for acc in assets:
            asset_lines.append({'code': acc.code, 'name': acc.name, 'amount': acc.balance})
            total_assets += acc.balance

        liability_lines = []
        total_liabilities = Decimal('0.00')
        for acc in liabilities:
            liability_lines.append({'code': acc.code, 'name': acc.name, 'amount': acc.balance})
            total_liabilities += acc.balance

        equity_lines = []
        total_equity = Decimal('0.00')
        for acc in equity:
            equity_lines.append({'code': acc.code, 'name': acc.name, 'amount': acc.balance})
            total_equity += acc.balance

        # We display net income as part of retained earnings
        equity_lines.append({'code': '39999', 'name': 'Current Year Net Profit', 'amount': net_income})
        total_equity += net_income

        return Response({
            'assets': asset_lines,
            'total_assets': total_assets,
            'liabilities': liability_lines,
            'total_liabilities': total_liabilities,
            'equity': equity_lines,
            'total_equity': total_equity,
            'total_liabilities_and_equity': total_liabilities + total_equity
        })


from rest_framework.permissions import AllowAny

class SignUpView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from django.contrib.auth.models import User
        from rest_framework.authtoken.models import Token
        
        username = request.data.get('username')
        password = request.data.get('password')
        email = request.data.get('email', '')

        if not username or not password:
            return Response(
                {"detail": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(username=username).exists():
            return Response(
                {"detail": "Username already exists."},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.create_user(username=username, email=email, password=password)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({
            "token": token.key,
            "username": user.username,
            "email": user.email
        }, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from django.contrib.auth import authenticate
        from rest_framework.authtoken.models import Token
        
        username = request.data.get('username')
        password = request.data.get('password')

        if not username or not password:
            return Response(
                {"detail": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = authenticate(username=username, password=password)
        if user is None:
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        token, _ = Token.objects.get_or_create(user=user)
        return Response({
            "token": token.key,
            "username": user.username,
            "email": user.email
        }, status=status.HTTP_200_OK)


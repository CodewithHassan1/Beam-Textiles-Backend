from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.db.models import ProtectedError
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.utils.dateparse import parse_date
from decimal import Decimal, InvalidOperation
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
        if instance.status != 'Draft':
            return Response(
                {"detail": "Posted or Paid invoices cannot be deleted. Please reverse or void the transaction instead."},
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


def _bank_accounts():
    """Accounts that represent real cash/bank movements. Falls back to a code/name
    heuristic for databases created before the is_bank flag existed."""
    from django.db.models import Q
    qs = Account.objects.filter(is_bank=True)
    if qs.exists():
        return qs
    return Account.objects.filter(account_type='Asset').filter(
        Q(code='10100') | Q(name__icontains='bank') | Q(name__icontains='cash')
    )


def _candidate_lines(statement, bank_account_ids, window_days=10):
    """Posted, still-unmatched transaction lines on a BANK account, in the correct
    direction, within +/- window_days of the statement date."""
    start = statement.date - datetime.timedelta(days=window_days)
    end = statement.date + datetime.timedelta(days=window_days)
    qs = TransactionLine.objects.filter(
        journal_entry__posted=True,
        journal_entry__date__range=(start, end),
        account_id__in=bank_account_ids,
        bankstatementline__isnull=True,
    ).select_related('journal_entry', 'account')
    # Deposit (+) -> debit to bank; Withdrawal (-) -> credit to bank
    if statement.amount > 0:
        qs = qs.filter(debit__gt=0)
    else:
        qs = qs.filter(credit__gt=0)
    return qs


def _match_score(statement, line):
    """Confidence score 0-100 for a (statement, ledger line) pairing."""
    score = 0
    target = abs(statement.amount)
    line_amount = line.debit if line.debit > 0 else line.credit
    if abs(line_amount - target) < Decimal('0.01'):
        score += 60                      # exact amount
    elif target > 0 and abs(line_amount - target) / target <= Decimal('0.02'):
        score += 35                      # within 2%
    delta = abs((statement.date - line.journal_entry.date).days)
    if delta == 0:
        score += 25
    elif delta <= 3:
        score += 18
    elif delta <= 7:
        score += 10
    ref = (line.journal_entry.reference or '').strip().lower()
    if ref and ref in (statement.description or '').lower():
        score += 15                      # invoice/journal reference appears in narration
    return min(score, 100)


def _suggest(statement, bank_account_ids, top=3):
    scored = [
        (_match_score(statement, ln), ln)
        for ln in _candidate_lines(statement, bank_account_ids)
    ]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda x: (-x[0], abs((statement.date - x[1].journal_entry.date).days)))
    out = []
    for sc, ln in scored[:top]:
        out.append({
            'transaction_line_id': ln.id,
            'journal_id': ln.journal_entry_id,
            'journal_reference': ln.journal_entry.reference,
            'journal_date': ln.journal_entry.date,
            'account_code': ln.account.code,
            'amount': ln.debit if ln.debit > 0 else ln.credit,
            'confidence': sc,
        })
    return out


class BankStatementLineViewSet(viewsets.ModelViewSet):
    queryset = BankStatementLine.objects.all().order_by('date', 'id')
    serializer_class = BankStatementLineSerializer

    # DRF's default parsers (JSON, Form, MultiPart) are active, so file uploads
    # work without extra configuration.
    @action(detail=False, methods=['post'], url_path='import')
    def import_csv(self, request):
        """
        Import real bank statement lines from an uploaded CSV file.
        Accepts multipart 'file'. Recognised headers (case-insensitive):
        date | description/details/narration | amount/value.
        Returns a summary: imported / skipped (duplicates) / failed (invalid).
        """
        import csv
        import io

        upload = request.FILES.get('file')
        if not upload:
            return Response({"detail": "No CSV file provided (form field 'file')."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            text = upload.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            return Response({"detail": "File must be a UTF-8 encoded CSV."},
                            status=status.HTTP_400_BAD_REQUEST)

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return Response({"detail": "CSV is empty or has no header row."},
                            status=status.HTTP_400_BAD_REQUEST)

        imported, skipped, failed = 0, 0, 0
        errors = []
        created = []
        with transaction.atomic():
            for i, row in enumerate(reader, start=2):  # row 1 = header
                norm = {(k or '').strip().lower(): (v or '').strip() for k, v in row.items()}
                date_raw = norm.get('date') or norm.get('transaction date') or norm.get('txn date')
                desc = (norm.get('description') or norm.get('details')
                        or norm.get('narration') or '').strip()
                amount_raw = norm.get('amount') or norm.get('value')

                d = parse_date(date_raw) if date_raw else None
                if not d:
                    failed += 1
                    errors.append(f"Row {i}: missing/invalid date '{date_raw}'")
                    continue
                try:
                    amt = Decimal(str(amount_raw).replace(',', '').replace('(', '-').replace(')', ''))
                except (InvalidOperation, AttributeError, TypeError):
                    failed += 1
                    errors.append(f"Row {i}: invalid amount '{amount_raw}'")
                    continue
                if not desc:
                    desc = 'Imported bank transaction'

                if BankStatementLine.objects.filter(date=d, description=desc, amount=amt).exists():
                    skipped += 1
                    continue

                created.append(BankStatementLine.objects.create(
                    date=d, description=desc, amount=amt, reconciled=False))
                imported += 1

        return Response({
            'imported': imported,
            'skipped': skipped,
            'failed': failed,
            'errors': errors[:50],
            'lines': BankStatementLineSerializer(created, many=True).data,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='auto-match')
    def auto_match(self, request):
        """
        Accounting-safe auto reconciliation: scores candidate BANK-account ledger
        lines by amount, date proximity and reference, and auto-links only
        high-confidence matches (>= 80). Uses live ledger data — never hardcoded.
        """
        bank_ids = list(_bank_accounts().values_list('id', flat=True))
        if not bank_ids:
            return Response({"detail": "No bank account configured. Flag a cash/bank account first."},
                            status=status.HTTP_400_BAD_REQUEST)

        matched = []
        THRESHOLD = 80
        with transaction.atomic():
            for statement in BankStatementLine.objects.filter(reconciled=False):
                suggestions = _suggest(statement, bank_ids, top=1)
                if suggestions and suggestions[0]['confidence'] >= THRESHOLD:
                    best = suggestions[0]
                    statement.matched_transaction_line_id = best['transaction_line_id']
                    statement.reconciled = True
                    statement.save()
                    matched.append({
                        'bank_line_id': statement.id,
                        'description': statement.description,
                        'transaction_line_id': best['transaction_line_id'],
                        'journal_reference': best['journal_reference'],
                        'confidence': best['confidence'],
                    })

        return Response({
            'status': 'Reconciliation run completed',
            'matched_records_count': len(matched),
            'threshold': THRESHOLD,
            'matches': matched,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='suggestions')
    def suggestions(self, request, pk=None):
        """Return ranked candidate matches (with confidence) for one statement line."""
        statement = self.get_object()
        bank_ids = list(_bank_accounts().values_list('id', flat=True))
        return Response({
            'bank_line_id': statement.id,
            'suggestions': _suggest(statement, bank_ids, top=5),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='match')
    def manual_match(self, request, pk=None):
        """Manually link a statement line to a specific bank ledger TransactionLine."""
        statement = self.get_object()
        line_id = request.data.get('transaction_line_id')
        if not line_id:
            return Response({"detail": "transaction_line_id is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            line = TransactionLine.objects.select_related('account', 'journal_entry').get(pk=line_id)
        except TransactionLine.DoesNotExist:
            return Response({"detail": "Ledger transaction line not found."},
                            status=status.HTTP_404_NOT_FOUND)
        if hasattr(line, 'bankstatementline') and line.bankstatementline and line.bankstatementline.id != statement.id:
            return Response({"detail": "That ledger line is already matched to another statement."},
                            status=status.HTTP_400_BAD_REQUEST)
        statement.matched_transaction_line = line
        statement.reconciled = True
        statement.save()
        return Response(self.get_serializer(statement).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='unmatch')
    def manual_unmatch(self, request, pk=None):
        """Remove an existing match and return the statement line to pending."""
        statement = self.get_object()
        statement.matched_transaction_line = None
        statement.reconciled = False
        statement.save()
        return Response(self.get_serializer(statement).data, status=status.HTTP_200_OK)


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


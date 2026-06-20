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
    JournalEntry, TransactionLine, BankStatementLine
)
from .serializers import (
    JournalEntrySerializer, BankStatementLineSerializer
)
from .utils import (
    process_invoice_posting, get_or_create_default_accounts,
    RECEIVABLES_CODE, PAYABLES_CODE, INVENTORY_ASSET_CODE,
    INPUT_TAX_CODE, OUTPUT_TAX_CODE, REVENUE_CODE, COGS_CODE
)
from .models import Profile, ActivityLog
from .permissions import RoleBasedPermission, IsAdminRole, user_role


def client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log_activity(request, action, model_name='', object_id='', detail='', username=None):
    user = getattr(request, 'user', None)
    authed = user if (user and user.is_authenticated) else None
    # `username` override is needed for auth events (during login the request
    # user is still anonymous, but we want to record who attempted/logged in).
    name = username if username is not None else (getattr(user, 'username', '') or '')
    ActivityLog.objects.create(
        user=authed,
        username=name,
        action=action,
        model_name=model_name,
        object_id=str(object_id or ''),
        detail=detail[:255],
        ip_address=client_ip(request),
    )


class AuditLogMixin:
    """Records create/update/delete actions on a viewset to the ActivityLog."""
    def perform_create(self, serializer):
        obj = serializer.save()
        log_activity(self.request, 'create', obj.__class__.__name__, getattr(obj, 'pk', ''))
        return obj

    def perform_update(self, serializer):
        obj = serializer.save()
        log_activity(self.request, 'update', obj.__class__.__name__, getattr(obj, 'pk', ''))
        return obj

    def perform_destroy(self, instance):
        name, pk = instance.__class__.__name__, instance.pk
        super().perform_destroy(instance)
        log_activity(self.request, 'delete', name, pk)



class JournalEntryViewSet(AuditLogMixin, viewsets.ModelViewSet):
    queryset = JournalEntry.objects.all().order_by('-date', '-id')
    serializer_class = JournalEntrySerializer
    permission_classes = [RoleBasedPermission]

    @action(detail=True, methods=['post'], url_path='post')
    def post_journal(self, request, pk=None):
        journal = self.get_object()
        try:
            journal.post_entry()
            return Response({'status': 'Journal entry posted successfully'}, status=status.HTTP_200_OK)
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


class BankStatementLineViewSet(AuditLogMixin, viewsets.ModelViewSet):
    queryset = BankStatementLine.objects.all().order_by('date', 'id')
    serializer_class = BankStatementLineSerializer
    permission_classes = [RoleBasedPermission]

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
        # All features removed - return minimal dashboard data
        receivables = Decimal('0.00')
        payables = Decimal('0.00')
        cash_balance = Decimal('0.00')
        inventory_valuation = Decimal('0.00')
        net_profit = Decimal('0.00')

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
        # Chart of Accounts removed - return empty trial balance
        return Response({
            'lines': [],
            'total_debit': Decimal('0.00'),
            'total_credit': Decimal('0.00')
        })


class ProfitAndLossView(APIView):
    def get(self, request):
        # Chart of Accounts removed - return empty P&L
        return Response({
            'revenues': [],
            'total_revenue': Decimal('0.00'),
            'expenses': [],
            'total_expense': Decimal('0.00'),
            'net_income': Decimal('0.00')
        })


class BalanceSheetView(APIView):
    def get(self, request):
        # Chart of Accounts removed - return empty balance sheet
        return Response({
            'assets': [],
            'total_assets': Decimal('0.00'),
            'liabilities': [],
            'total_liabilities': Decimal('0.00'),
            'equity': [],
            'total_equity': Decimal('0.00'),
            'total_liabilities_and_equity': Decimal('0.00')
        })


from rest_framework.permissions import AllowAny

# Account-lockout policy: too many recent failed logins for a username blocks it.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_MINUTES = 15


def _recent_failures(username):
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
    return ActivityLog.objects.filter(action='login_failed', username=username, timestamp__gte=since).count()


class SignUpView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'signup'

    def post(self, request):
        from django.contrib.auth.models import User
        from django.contrib.auth.password_validation import validate_password
        from rest_framework.authtoken.models import Token

        username = (request.data.get('username') or '').strip()
        password = request.data.get('password')
        email = request.data.get('email', '')

        if not username or not password:
            return Response({"detail": "Username and password are required."},
                            status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(username=username).exists():
            return Response({"detail": "Username already exists."},
                            status=status.HTTP_400_BAD_REQUEST)

        # Enforce Django's password validators (length, common, numeric, similarity).
        try:
            validate_password(password)
        except DjangoValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.create_user(username=username, email=email, password=password)
        # New self-service accounts get the lowest-privilege role by default.
        role = user_role(user)
        token, _ = Token.objects.get_or_create(user=user)
        log_activity(request, 'signup', 'User', user.pk, detail=f"role={role}", username=user.username)
        return Response({
            "token": token.key,
            "username": user.username,
            "email": user.email,
            "role": role,
        }, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'login'

    def post(self, request):
        from django.contrib.auth import authenticate
        from rest_framework.authtoken.models import Token

        username = (request.data.get('username') or '').strip()
        password = request.data.get('password')

        if not username or not password:
            return Response({"detail": "Username and password are required."},
                            status=status.HTTP_400_BAD_REQUEST)

        # Account lockout after repeated failures.
        if _recent_failures(username) >= LOGIN_MAX_FAILURES:
            return Response(
                {"detail": f"Account temporarily locked due to repeated failed logins. Try again in {LOGIN_LOCKOUT_MINUTES} minutes."},
                status=status.HTTP_403_FORBIDDEN)

        user = authenticate(username=username, password=password)
        if user is None:
            log_activity(request, 'login_failed', 'User', '', detail=username, username=username)
            return Response({"detail": "Invalid credentials."},
                            status=status.HTTP_401_UNAUTHORIZED)

        token, _ = Token.objects.get_or_create(user=user)
        log_activity(request, 'login', 'User', user.pk, username=user.username)
        return Response({
            "token": token.key,
            "username": user.username,
            "email": user.email,
            "role": user_role(user),
        }, status=status.HTTP_200_OK)


class MeView(APIView):
    """Returns the authenticated user's identity and role."""
    def get(self, request):
        u = request.user
        return Response({
            "username": u.username,
            "email": u.email,
            "role": user_role(u),
            "is_superuser": u.is_superuser,
        })


class ActivityLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only audit trail. Admin and Super Admin only."""
    queryset = ActivityLog.objects.all()
    serializer_class = None  # set below to avoid forward-reference
    permission_classes = [IsAdminRole]

    def get_serializer_class(self):
        from .serializers import ActivityLogSerializer
        return ActivityLogSerializer


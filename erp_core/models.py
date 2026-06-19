from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from decimal import Decimal


class Profile(models.Model):
    """Role-based access control profile, one per Django user."""
    ROLE_SUPER_ADMIN = 'super_admin'
    ROLE_ADMIN = 'admin'
    ROLE_MANAGER = 'manager'
    ROLE_STAFF = 'staff'
    ROLE_CHOICES = [
        (ROLE_SUPER_ADMIN, 'Super Admin'),
        (ROLE_ADMIN, 'Admin'),
        (ROLE_MANAGER, 'Manager'),
        (ROLE_STAFF, 'Staff'),
    ]
    # Privilege rank for hierarchy comparisons (higher = more access).
    RANK = {ROLE_STAFF: 1, ROLE_MANAGER: 2, ROLE_ADMIN: 3, ROLE_SUPER_ADMIN: 4}

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_STAFF)

    @property
    def rank(self):
        return self.RANK.get(self.role, 0)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


@receiver(post_save, sender=User)
def ensure_profile(sender, instance, created, **kwargs):
    """Auto-provision a profile. Superusers map to super_admin, staff users to
    admin, everyone else defaults to the lowest-privilege staff role."""
    if created:
        if instance.is_superuser:
            role = Profile.ROLE_SUPER_ADMIN
        elif instance.is_staff:
            role = Profile.ROLE_ADMIN
        else:
            role = Profile.ROLE_STAFF
        Profile.objects.get_or_create(user=instance, defaults={'role': role})


class ActivityLog(models.Model):
    """Append-only audit trail of significant actions."""
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='activity')
    username = models.CharField(max_length=150, blank=True)  # retained even if user is deleted
    action = models.CharField(max_length=20)                 # login / create / update / delete
    model_name = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=40, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp', '-id']

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.username} {self.action} {self.model_name}#{self.object_id}"

class Account(models.Model):
    ACCOUNT_TYPES = [
        ('Asset', 'Asset'),
        ('Liability', 'Liability'),
        ('Equity', 'Equity'),
        ('Revenue', 'Revenue'),
        ('Expense', 'Expense'),
    ]

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES)
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    # Flags cash/bank ledger accounts so bank reconciliation only matches
    # statement lines against genuine bank movements (accounting-safe).
    is_bank = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.code} - {self.name} ({self.account_type})"


class Partner(models.Model):
    PARTNER_TYPES = [
        ('Customer', 'Customer'),
        ('Supplier', 'Supplier'),
    ]

    name = models.CharField(max_length=150)
    partner_type = models.CharField(max_length=20, choices=PARTNER_TYPES)
    email = models.EmailField(blank=True, null=True)
    tax_id = models.CharField(max_length=50, blank=True, null=True)
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.name} ({self.partner_type})"


class JournalEntry(models.Model):
    date = models.DateField()
    description = models.TextField(blank=True)
    reference = models.CharField(max_length=100, blank=True)
    posted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Journal Entries"

    def __str__(self):
        status = "Posted" if self.posted else "Draft"
        return f"Journal Entry #{self.id} on {self.date} - Ref: {self.reference} ({status})"

    def clean(self):
        # Prevent editing if already posted
        if self.pk:
            original = JournalEntry.objects.get(pk=self.pk)
            if original.posted:
                raise ValidationError("Cannot modify or delete a posted Journal Entry.")

    def post_entry(self):
        """
        Locks the journal entry and updates General Ledger and Partner balances under a transaction.
        Enforces sum(debit) == sum(credit).
        """
        if self.posted:
            raise ValidationError("This Journal Entry has already been posted.")

        with transaction.atomic():
            lines = self.lines.all()
            if not lines.exists():
                raise ValidationError("Cannot post an empty Journal Entry.")

            debit_sum = sum(line.debit for line in lines)
            credit_sum = sum(line.credit for line in lines)

            if abs(debit_sum - credit_sum) > Decimal('0.01'):
                raise ValidationError(
                    f"Double-entry mismatch: Debits ({debit_sum}) must equal Credits ({credit_sum}). Difference: {abs(debit_sum - credit_sum)}"
                )

            # Update account and partner balances
            for line in lines:
                account = line.account
                # Assets and Expenses increase with Debit, decrease with Credit
                # Liabilities, Equity, and Revenue increase with Credit, decrease with Debit
                if account.account_type in ['Asset', 'Expense']:
                    account.balance += (line.debit - line.credit)
                else:
                    account.balance += (line.credit - line.debit)
                account.save()

            # Mark as posted
            self.posted = True
            self.save()


class TransactionLine(models.Model):
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    debit = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    credit = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"Line for Account {self.account.code}: Debit {self.debit} | Credit {self.credit}"

    def clean(self):
        if self.debit < 0 or self.credit < 0:
            raise ValidationError("Debit and Credit values must be non-negative.")
        if self.debit > 0 and self.credit > 0:
            raise ValidationError("A single transaction line cannot have both Debit and Credit values.")
        if self.journal_entry.posted:
            raise ValidationError("Cannot add lines to a posted Journal Entry.")


class InventoryItem(models.Model):
    COSTING_METHODS = [
        ('FIFO', 'FIFO (First-In, First-Out)'),
        ('AVCO', 'AVCO (Average Costing)'),
    ]

    sku = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    costing_method = models.CharField(max_length=10, choices=COSTING_METHODS, default='AVCO')
    stock_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    avg_unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.sku} - {self.name} (Qty: {self.stock_qty}, Avg Cost: Rs {self.avg_unit_cost:.2f})"


class StockTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('IN', 'Purchase / Stock In'),
        ('OUT', 'Sale / Stock Out'),
    ]

    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='stock_transactions')
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, null=True, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)  # Positive for IN, Negative for OUT
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=5, choices=TRANSACTION_TYPES)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.transaction_type}: {self.inventory_item.sku} Qty {self.quantity} @ Rs {self.unit_cost:.2f}"


class Invoice(models.Model):
    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Posted', 'Posted'),
        ('Paid', 'Paid'),
        ('Cancelled', 'Cancelled'),
    ]

    INVOICE_TYPES = [
        ('Customer', 'Customer Invoice'),
        ('Vendor', 'Vendor Bill / Purchase Invoice'),
    ]

    partner = models.ForeignKey(Partner, on_delete=models.PROTECT)
    invoice_type = models.CharField(max_length=10, choices=INVOICE_TYPES, default='Customer')
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice')
    issue_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='Draft')
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15.00'))  # e.g., 15.00 for 15%
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.invoice_type} Invoice #{self.id} for {self.partner.name} - Rs {self.total_amount:.2f} ({self.status})"

    def delete(self, *args, **kwargs):
        if self.status != 'Draft':
            raise ValidationError("Posted or Paid invoices cannot be deleted. Please reverse or void the transaction instead.")
        super().delete(*args, **kwargs)


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='lines')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total_price = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    def save(self, *args, **kwargs):
        self.total_price = self.quantity * self.unit_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.inventory_item.sku}: {self.quantity} x {self.unit_price} = {self.total_price}"


class BankStatementLine(models.Model):
    date = models.DateField()
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=15, decimal_places=2)  # Positive for deposit, negative for withdrawal
    reconciled = models.BooleanField(default=False)
    matched_transaction_line = models.ForeignKey(TransactionLine, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        status = "Reconciled" if self.reconciled else "Unreconciled"
        return f"{self.date} - {self.description} - Rs {self.amount:.2f} ({status})"

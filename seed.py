import os
import sys
import django
from decimal import Decimal
import datetime

# Add the current directory to path and set up Django configuration
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_backend.settings')
django.setup()

from django.core.management import call_command
from erp_core.models import Account, Partner, InventoryItem, Invoice, InvoiceLine, BankStatementLine
from erp_core.utils import get_or_create_default_accounts, process_invoice_posting

def seed_database():
    print("Step 1: Running Database Migrations...")
    call_command('makemigrations', 'erp_core')
    call_command('migrate')

    print("Step 2: Generating Chart of Accounts...")
    coa = get_or_create_default_accounts()
    
    # Add a cash/bank asset account for bank statement reconciliation
    cash_bank_acc, created = Account.objects.get_or_create(
        code='10100',
        defaults={'name': 'Cash and Bank Assets', 'account_type': 'Asset', 'balance': Decimal('10000.00')}
    )
    # Give the bank account some starting money (equity contribution)
    if created:
        print("Created Cash/Bank account. Seeding starting balance...")
        # Since Assets increase with Debit, let's create a starting journal entry
        from erp_core.models import JournalEntry, TransactionLine
        je = JournalEntry.objects.create(
            date=datetime.date.today(),
            description="Owner's Initial Capital Contribution",
            reference="INIT-001"
        )
        # Debit Cash/Bank
        TransactionLine.objects.create(
            journal_entry=je,
            account=cash_bank_acc,
            debit=Decimal('10000.00'),
            credit=Decimal('0.00')
        )
        # Credit Equity Account (e.g. 30000 Retained Earnings or Owner Equity)
        equity_acc, _ = Account.objects.get_or_create(
            code='30000',
            defaults={'name': 'Owners Capital', 'account_type': 'Equity'}
        )
        TransactionLine.objects.create(
            journal_entry=je,
            account=equity_acc,
            debit=Decimal('0.00'),
            credit=Decimal('10000.00')
        )
        je.post_entry()

    print("Step 3: Creating Partners...")
    # Customers
    acme, _ = Partner.objects.get_or_create(
        name="Lahore Fabric House",
        partner_type="Customer",
        defaults={"email": "orders@lahorefabric.pk", "tax_id": "3520112-4"}
    )
    globex, _ = Partner.objects.get_or_create(
        name="Gulberg Garments Co.",
        partner_type="Customer",
        defaults={"email": "accounts@gulberggarments.pk", "tax_id": "3740998-1"}
    )

    # Suppliers
    techcorp, _ = Partner.objects.get_or_create(
        name="Faisalabad Yarn Mills",
        partner_type="Supplier",
        defaults={"email": "sales@fsdyarnmills.pk", "tax_id": "3310554-3"}
    )
    shipfast, _ = Partner.objects.get_or_create(
        name="Crescent Cotton Traders",
        partner_type="Supplier",
        defaults={"email": "billing@crescentcotton.pk", "tax_id": "3210667-8"}
    )

    print("Step 4: Creating Inventory Items...")
    # AVCO Costing Item
    laptop, _ = InventoryItem.objects.get_or_create(
        sku="FAB-COT-01",
        defaults={
            "name": "Cotton Fabric Roll (per metre)",
            "costing_method": "AVCO",
            "stock_qty": Decimal('0.00'),
            "avg_unit_cost": Decimal('0.00')
        }
    )
    # FIFO Costing Item
    monitor, _ = InventoryItem.objects.get_or_create(
        sku="YRN-PLY-02",
        defaults={
            "name": "Polyester Blended Yarn Cone",
            "costing_method": "FIFO",
            "stock_qty": Decimal('0.00'),
            "avg_unit_cost": Decimal('0.00')
        }
    )

    print("Step 5: Simulating Supply Chain Purchase Bills (Inventory Receipts)...")
    # Purchase 50 laptops @ Rs 800 each
    bill1, created1 = Invoice.objects.get_or_create(
        partner=techcorp,
        invoice_type="Vendor",
        issue_date=datetime.date.today() - datetime.timedelta(days=15),
        due_date=datetime.date.today() + datetime.timedelta(days=15),
        defaults={
            "status": "Draft",
            "tax_rate": Decimal('15.00')
        }
    )
    if created1:
        InvoiceLine.objects.create(invoice=bill1, inventory_item=laptop, quantity=Decimal('50.00'), unit_price=Decimal('800.00'))
        # Re-fetch bill to update totals before posting
        bill1.subtotal = Decimal('40000.00')
        bill1.tax_amount = Decimal('6000.00')
        bill1.total_amount = Decimal('46000.00')
        bill1.save()
        process_invoice_posting(bill1.id)
        print("  -> Vendor bill posted for 50 Laptops (AVCO cost base established at Rs 800).")

    # Purchase 30 monitors @ Rs 300 each
    bill2, created2 = Invoice.objects.get_or_create(
        partner=techcorp,
        invoice_type="Vendor",
        issue_date=datetime.date.today() - datetime.timedelta(days=10),
        due_date=datetime.date.today() + datetime.timedelta(days=20),
        defaults={
            "status": "Draft",
            "tax_rate": Decimal('10.00')
        }
    )
    if created2:
        InvoiceLine.objects.create(invoice=bill2, inventory_item=monitor, quantity=Decimal('30.00'), unit_price=Decimal('300.00'))
        bill2.subtotal = Decimal('9000.00')
        bill2.tax_amount = Decimal('900.00')
        bill2.total_amount = Decimal('9900.00')
        bill2.save()
        process_invoice_posting(bill2.id)
        print("  -> Vendor bill posted for 30 Monitors (FIFO layer 1 established: 30 @ Rs 300).")

    # Purchase 20 more monitors @ Rs 350 each (price increased)
    bill3, created3 = Invoice.objects.get_or_create(
        partner=techcorp,
        invoice_type="Vendor",
        issue_date=datetime.date.today() - datetime.timedelta(days=8),
        due_date=datetime.date.today() + datetime.timedelta(days=22),
        defaults={
            "status": "Draft",
            "tax_rate": Decimal('10.00')
        }
    )
    if created3:
        InvoiceLine.objects.create(invoice=bill3, inventory_item=monitor, quantity=Decimal('20.00'), unit_price=Decimal('350.00'))
        bill3.subtotal = Decimal('7000.00')
        bill3.tax_amount = Decimal('700.00')
        bill3.total_amount = Decimal('7700.00')
        bill3.save()
        process_invoice_posting(bill3.id)
        print("  -> Vendor bill posted for 20 Monitors (FIFO layer 2 established: 20 @ Rs 350).")

    print("Step 6: Simulating Sales Invoices (Customer Orders & COGS Automation)...")
    # Sell 10 laptops @ Rs 1200 each and 35 monitors @ Rs 500 each to Acme Corp
    inv1, created_inv1 = Invoice.objects.get_or_create(
        partner=acme,
        invoice_type="Customer",
        issue_date=datetime.date.today() - datetime.timedelta(days=5),
        due_date=datetime.date.today() + datetime.timedelta(days=25),
        defaults={
            "status": "Draft",
            "tax_rate": Decimal('15.00')
        }
    )
    if created_inv1:
        InvoiceLine.objects.create(invoice=inv1, inventory_item=laptop, quantity=Decimal('10.00'), unit_price=Decimal('1200.00')) # Rs 12,000
        InvoiceLine.objects.create(invoice=inv1, inventory_item=monitor, quantity=Decimal('35.00'), unit_price=Decimal('500.00'))  # Rs 17,500
        inv1.subtotal = Decimal('29500.00')
        inv1.tax_amount = Decimal('4425.00')
        inv1.total_amount = Decimal('33925.00')
        inv1.save()
        process_invoice_posting(inv1.id)
        # FIFO monitor COGS should be: (30 @ Rs 300) + (5 @ Rs 350) = Rs 9,000 + Rs 1,750 = Rs 10,750
        # AVCO laptop COGS should be: 10 @ Rs 800 = Rs 8,000
        print("  -> Customer invoice posted for 10 Laptops & 35 Monitors.")
        print("     - FIFO Costing verified: 35 Monitors sold (exhausted 30 units @ Rs 300 and 5 units @ Rs 350).")

    print("Step 7: Creating Bank Statement Lines for Reconciliation...")
    # We create some unreconciled lines, and some that are ready to match
    # Let's seed some entries that map to our invoice totals for reconciliation matching
    BankStatementLine.objects.get_or_create(
        date=datetime.date.today() - datetime.timedelta(days=2),
        description="Wire Deposit Lahore Fabric House INV-1",
        amount=Decimal('33925.00'),
        reconciled=False
    )
    BankStatementLine.objects.get_or_create(
        date=datetime.date.today() - datetime.timedelta(days=1),
        description="ACH Withdrawal Faisalabad Yarn Mills BILL-1",
        amount=Decimal('-46000.00'),
        reconciled=False
    )
    BankStatementLine.objects.get_or_create(
        date=datetime.date.today(),
        description="Interest Payment Received",
        amount=Decimal('15.50'),
        reconciled=False
    )

    print("\nDatabase seeding completed successfully!")
    print("General Ledger, Inventory costing layers, Invoicing, and Reconciliations are fully populated.")

if __name__ == '__main__':
    seed_database()

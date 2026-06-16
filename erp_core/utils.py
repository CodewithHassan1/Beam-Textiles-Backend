from decimal import Decimal
from django.db import transaction, models
from django.core.exceptions import ValidationError
from .models import (
    Account, Partner, JournalEntry, TransactionLine, 
    InventoryItem, StockTransaction, Invoice, InvoiceLine
)

# Helper constants for Account Codes
# If accounts don't exist, we will create them in migrations or setup
RECEIVABLES_CODE = '11000' # Accounts Receivable (Asset)
INVENTORY_ASSET_CODE = '12000' # Inventory Asset (Asset)
INPUT_TAX_CODE = '13000' # Input Tax / Tax Receivable (Asset)
PAYABLES_CODE = '21000' # Accounts Payable (Liability)
OUTPUT_TAX_CODE = '22000' # Output Tax / Tax Payable (Liability)
REVENUE_CODE = '40000' # Sales Revenue (Revenue)
COGS_CODE = '50000' # Cost of Goods Sold (Expense)

def get_or_create_default_accounts():
    """Ensures core ledger accounts exist in the chart of accounts."""
    accounts = {
        RECEIVABLES_CODE: ('Accounts Receivable', 'Asset'),
        INVENTORY_ASSET_CODE: ('Inventory Asset', 'Asset'),
        INPUT_TAX_CODE: ('Input Tax Asset', 'Asset'),
        PAYABLES_CODE: ('Accounts Payable', 'Liability'),
        OUTPUT_TAX_CODE: ('Output Tax Liability', 'Liability'),
        REVENUE_CODE: ('Sales Revenue', 'Revenue'),
        COGS_CODE: ('Cost of Goods Sold', 'Expense'),
    }
    created_accounts = {}
    for code, (name, acc_type) in accounts.items():
        account, _ = Account.objects.get_or_create(
            code=code,
            defaults={'name': name, 'account_type': acc_type}
        )
        created_accounts[code] = account
    return created_accounts


def calculate_fifo_cogs(item, quantity_to_sell):
    """
    Computes Cost of Goods Sold (COGS) using FIFO.
    Traverses historical positive stock transactions (IN) and maps them against 
    total historical sold quantities (OUT) to determine which purchases are consumed.
    """
    if item.stock_qty < quantity_to_sell:
        raise ValidationError(f"Insufficient stock for {item.sku}. Requested: {quantity_to_sell}, Available: {item.stock_qty}")

    # Total quantity sold before this transaction
    # Note: StockTransaction.quantity is positive for IN, negative for OUT
    total_sold_query = StockTransaction.objects.filter(
        inventory_item=item, 
        transaction_type='OUT'
    ).aggregate(total=models.Sum('quantity'))
    
    total_previously_sold = abs(total_sold_query['total'] or Decimal('0.00'))

    # All purchases (IN transactions) sorted chronologically
    purchases = StockTransaction.objects.filter(
        inventory_item=item, 
        transaction_type='IN'
    ).order_by('timestamp', 'id')

    cogs = Decimal('0.00')
    remaining_to_sell = Decimal(str(quantity_to_sell))
    skipped_qty = Decimal('0.00')

    for purchase in purchases:
        purchase_qty = purchase.quantity
        purchase_cost = purchase.unit_cost

        # Determine if this purchase was already fully consumed by previous sales
        if skipped_qty + purchase_qty <= total_previously_sold:
            skipped_qty += purchase_qty
            continue
        
        # Calculate available quantity in this purchase after accounting for previous sales
        available_in_purchase = purchase_qty - (total_previously_sold - skipped_qty)
        # Advance skipped_qty so we do this once
        skipped_qty = total_previously_sold

        if available_in_purchase <= 0:
            continue

        # Consume from this purchase
        consume_qty = min(remaining_to_sell, available_in_purchase)
        cogs += consume_qty * purchase_cost
        remaining_to_sell -= consume_qty

        if remaining_to_sell <= Decimal('0.00'):
            break

    if remaining_to_sell > Decimal('0.00'):
        # Fallback to current avg_unit_cost if there's any float/rounding mismatch
        cogs += remaining_to_sell * item.avg_unit_cost

    return cogs


def calculate_avco_cogs(item, quantity_to_sell):
    """
    Computes Cost of Goods Sold (COGS) using AVCO (Average Cost).
    Simply utilizes the running average cost.
    """
    if item.stock_qty < quantity_to_sell:
        raise ValidationError(f"Insufficient stock for {item.sku}. Requested: {quantity_to_sell}, Available: {item.stock_qty}")
    
    return Decimal(str(quantity_to_sell)) * item.avg_unit_cost


@transaction.atomic
def process_invoice_posting(invoice_id):
    """
    Automates the double-entry accounting updates for Invoices (Customer Sales)
    and Bills (Vendor Purchases). Enforces ACID transaction bounds.
    """
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    if invoice.status != 'Draft':
        raise ValidationError("Only Draft invoices can be posted.")

    # Ensure Chart of Accounts is initialized
    coa = get_or_create_default_accounts()

    # Create Journal Entry header
    journal_entry = JournalEntry.objects.create(
        date=invoice.issue_date,
        description=f"Automated posting for {invoice.invoice_type} Invoice #{invoice.id}",
        reference=f"INV-{invoice.id}" if invoice.invoice_type == 'Customer' else f"BILL-{invoice.id}"
    )

    total_cogs = Decimal('0.00')

    if invoice.invoice_type == 'Customer':
        # --- Customer Invoice Posting Workflow ---
        # 1. Generate Stock Transactions & Compute COGS
        for line in invoice.lines.all():
            item = line.inventory_item
            
            # Determine COGS based on costing method
            if item.costing_method == 'FIFO':
                line_cogs = calculate_fifo_cogs(item, line.quantity)
            else: # AVCO
                line_cogs = calculate_avco_cogs(item, line.quantity)

            total_cogs += line_cogs
            unit_cogs = line_cogs / line.quantity if line.quantity > 0 else Decimal('0.00')

            # Create OUT stock transaction
            StockTransaction.objects.create(
                inventory_item=item,
                journal_entry=journal_entry,
                quantity=-line.quantity,  # Negative for sales
                unit_cost=unit_cogs,
                transaction_type='OUT'
            )

            # Decrement physical stock
            item.stock_qty -= line.quantity
            item.save()

        # 2. Build Transaction Lines (Debits and Credits)
        # Debit: Accounts Receivable (Asset) -> Increase
        TransactionLine.objects.create(
            journal_entry=journal_entry,
            account=coa[RECEIVABLES_CODE],
            debit=invoice.total_amount,
            credit=Decimal('0.00')
        )

        # Credit: Sales Revenue (Revenue) -> Increase
        TransactionLine.objects.create(
            journal_entry=journal_entry,
            account=coa[REVENUE_CODE],
            debit=Decimal('0.00'),
            credit=invoice.subtotal
        )

        # Credit: Output Tax Liability (Liability) -> Increase
        if invoice.tax_amount > 0:
            TransactionLine.objects.create(
                journal_entry=journal_entry,
                account=coa[OUTPUT_TAX_CODE],
                debit=Decimal('0.00'),
                credit=invoice.tax_amount
            )

        # Record COGS and Inventory asset movement
        if total_cogs > 0:
            # Debit: Cost of Goods Sold (Expense) -> Increase
            TransactionLine.objects.create(
                journal_entry=journal_entry,
                account=coa[COGS_CODE],
                debit=total_cogs,
                credit=Decimal('0.00')
            )
            # Credit: Inventory Asset (Asset) -> Decrease
            TransactionLine.objects.create(
                journal_entry=journal_entry,
                account=coa[INVENTORY_ASSET_CODE],
                debit=Decimal('0.00'),
                credit=total_cogs
            )

        # Update Partner's Receivable Balance
        partner = invoice.partner
        partner.balance += invoice.total_amount
        partner.save()

    else:
        # --- Vendor Bill Posting Workflow ---
        # 1. Generate Stock Transactions & Update Costings
        for line in invoice.lines.all():
            item = line.inventory_item

            # Create IN stock transaction
            StockTransaction.objects.create(
                inventory_item=item,
                journal_entry=journal_entry,
                quantity=line.quantity,
                unit_cost=line.unit_price,
                transaction_type='IN'
            )

            # Update Inventory average cost if AVCO
            if item.costing_method == 'AVCO':
                current_value = item.stock_qty * item.avg_unit_cost
                new_value = line.quantity * line.unit_price
                new_qty = item.stock_qty + line.quantity
                
                if new_qty > 0:
                    item.avg_unit_cost = (current_value + new_value) / new_qty
                else:
                    item.avg_unit_cost = line.unit_price
            else:
                # For FIFO, we update the average unit cost for display purposes
                new_qty = item.stock_qty + line.quantity
                if new_qty > 0:
                    current_value = item.stock_qty * item.avg_unit_cost
                    new_value = line.quantity * line.unit_price
                    item.avg_unit_cost = (current_value + new_value) / new_qty
                else:
                    item.avg_unit_cost = line.unit_price

            # Increment physical stock
            item.stock_qty += line.quantity
            item.save()

        # 2. Build Transaction Lines (Debits and Credits)
        # Debit: Inventory Asset (Asset) -> Increase
        TransactionLine.objects.create(
            journal_entry=journal_entry,
            account=coa[INVENTORY_ASSET_CODE],
            debit=invoice.subtotal,
            credit=Decimal('0.00')
        )

        # Debit: Input Tax Asset (Asset) -> Increase
        if invoice.tax_amount > 0:
            TransactionLine.objects.create(
                journal_entry=journal_entry,
                account=coa[INPUT_TAX_CODE],
                debit=invoice.tax_amount,
                credit=Decimal('0.00')
            )

        # Credit: Accounts Payable (Liability) -> Increase (reduces when we pay vendor)
        TransactionLine.objects.create(
            journal_entry=journal_entry,
            account=coa[PAYABLES_CODE],
            debit=Decimal('0.00'),
            credit=invoice.total_amount
        )

        # Update Partner's Payable Balance (we owe supplier)
        partner = invoice.partner
        partner.balance += invoice.total_amount
        partner.save()

    # Commit journal entry to General Ledger
    journal_entry.post_entry()

    # Update Invoice Status
    invoice.journal_entry = journal_entry
    invoice.status = 'Posted'
    invoice.save()

    return journal_entry

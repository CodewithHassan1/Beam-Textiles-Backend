#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_backend.settings')
django.setup()

from erp_core.models import Account, Partner, InventoryItem, Invoice, InvoiceLine, StockTransaction

# Delete all records from these models
Account.objects.all().delete()
print("[OK] Deleted all Account records")

Partner.objects.all().delete()
print("[OK] Deleted all Partner records")

InventoryItem.objects.all().delete()
print("[OK] Deleted all InventoryItem records")

InvoiceLine.objects.all().delete()
print("[OK] Deleted all InvoiceLine records")

Invoice.objects.all().delete()
print("[OK] Deleted all Invoice records")

StockTransaction.objects.all().delete()
print("[OK] Deleted all StockTransaction records")

print("\n[SUCCESS] All data cleared successfully!")

from rest_framework import serializers
from decimal import Decimal
from .models import (
    Account, Partner, JournalEntry, TransactionLine, 
    InventoryItem, StockTransaction, Invoice, InvoiceLine, BankStatementLine
)

class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ['id', 'code', 'name', 'account_type', 'balance']
        read_only_fields = ['balance']


class PartnerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Partner
        fields = ['id', 'name', 'partner_type', 'email', 'tax_id', 'balance']
        read_only_fields = ['balance']


class TransactionLineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source='account.code', read_only=True)
    account_name = serializers.CharField(source='account.name', read_only=True)

    class Meta:
        model = TransactionLine
        fields = ['id', 'account', 'account_code', 'account_name', 'debit', 'credit']


class JournalEntrySerializer(serializers.ModelSerializer):
    lines = TransactionLineSerializer(many=True)

    class Meta:
        model = JournalEntry
        fields = ['id', 'date', 'description', 'reference', 'posted', 'created_at', 'lines']
        read_only_fields = ['posted', 'created_at']

    def create(self, validated_data):
        lines_data = validated_data.pop('lines')
        journal_entry = JournalEntry.objects.create(**validated_data)
        for line_data in lines_data:
            TransactionLine.objects.create(journal_entry=journal_entry, **line_data)
        return journal_entry


class InventoryItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryItem
        fields = ['id', 'sku', 'name', 'costing_method', 'stock_qty', 'avg_unit_cost']
        read_only_fields = ['stock_qty', 'avg_unit_cost']


class StockTransactionSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source='inventory_item.sku', read_only=True)
    item_name = serializers.CharField(source='inventory_item.name', read_only=True)

    class Meta:
        model = StockTransaction
        fields = ['id', 'inventory_item', 'item_sku', 'item_name', 'journal_entry', 'quantity', 'unit_cost', 'transaction_type', 'timestamp']


class InvoiceLineSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source='inventory_item.sku', read_only=True)
    item_name = serializers.CharField(source='inventory_item.name', read_only=True)

    class Meta:
        model = InvoiceLine
        fields = ['id', 'inventory_item', 'item_sku', 'item_name', 'quantity', 'unit_price', 'total_price']
        read_only_fields = ['total_price']


class InvoiceSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source='partner.name', read_only=True)
    lines = InvoiceLineSerializer(many=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'partner', 'partner_name', 'invoice_type', 'journal_entry', 
            'issue_date', 'due_date', 'status', 'subtotal', 'tax_rate', 
            'tax_amount', 'total_amount', 'lines'
        ]
        read_only_fields = ['status', 'subtotal', 'tax_amount', 'total_amount', 'journal_entry']

    def create(self, validated_data):
        lines_data = validated_data.pop('lines')
        
        # Calculate subtotal, tax and total
        subtotal = Decimal('0.00')
        lines_to_create = []
        
        for line_data in lines_data:
            qty = line_data.get('quantity')
            price = line_data.get('unit_price')
            line_total = qty * price
            subtotal += line_total
            lines_to_create.append(line_data)
            
        tax_rate = validated_data.get('tax_rate', Decimal('15.00'))
        tax_amount = subtotal * (tax_rate / Decimal('100.00'))
        total_amount = subtotal + tax_amount

        invoice = Invoice.objects.create(
            subtotal=subtotal,
            tax_amount=tax_amount,
            total_amount=total_amount,
            **validated_data
        )

        for line_data in lines_to_create:
            InvoiceLine.objects.create(invoice=invoice, **line_data)

        return invoice


class BankStatementLineSerializer(serializers.ModelSerializer):
    matched_account_code = serializers.CharField(source='matched_transaction_line.account.code', read_only=True)
    matched_journal_id = serializers.IntegerField(source='matched_transaction_line.journal_entry.id', read_only=True)

    class Meta:
        model = BankStatementLine
        fields = ['id', 'date', 'description', 'amount', 'reconciled', 'matched_transaction_line', 'matched_account_code', 'matched_journal_id']

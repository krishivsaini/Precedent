"""Storage-record <-> pure-domain-object conversion.

`domain.matching.match_batch` operates on the domain layer's lean, zero-I/O value
objects, not the storage layer's full-schema records this package generates. Shared here
because both the builder tests and the eval runner (Ring 0.6) need the same conversion.
"""

from datetime import date, datetime

from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord
from precedent.domain import matching as domain_matching


def to_domain_payment(record: PaymentRecord) -> domain_matching.Payment:
    return domain_matching.Payment(
        payment_id=record.payment_id,
        order_id=record.order_id,
        amount_paise=record.amount_paise,
        captured_at=datetime.fromisoformat(record.captured_at),
        fee_paise=record.fee_paise,
        tax_paise=record.tax_paise,
    )


def to_domain_bank_line(record: BankLineRecord) -> domain_matching.BankLine:
    return domain_matching.BankLine(
        line_id=record.line_id,
        value_date=date.fromisoformat(record.value_date),
        amount_paise=record.amount_paise,
        direction=record.direction,
        narration=record.narration,
    )


def to_domain_ledger_entry(record: LedgerEntryRecord) -> domain_matching.LedgerEntry:
    return domain_matching.LedgerEntry(
        entry_id=record.entry_id,
        order_id=record.order_id,
        expected_amount_paise=record.expected_amount_paise,
        invoice_no=record.invoice_no,
        customer_name=record.customer_name,
    )

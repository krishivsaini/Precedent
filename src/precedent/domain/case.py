"""A reconciliation case — the bundle of records an exception is raised over — and its
rendering into text.

Two renderings, for two different consumers:

1. `summarize()` — the `case_summary` shown to the model in the investigate and deposit
   prompts. Every record, every amount, every narration.
2. `retrieval_query()` — what the corpus is searched with. The computed observations only.

An earlier version of this module used one string for both, on the reasoning that a system
retrieving against one description and reasoning about another would make precedent-precision
meaningless. That was wrong, and measurably so: the full dump retrieves *worse*, because its
token mass is per-record boilerplate — "processor fee", "invoice", "terms" — that matches the
wrong precedents. Three exception classes retrieved at exactly zero until the query was cut
back to the observations. A query is a query; what has to hold is that both renderings are
derived from the same records, deterministically. That is what keeps the numbers honest.

**The rendering is mechanical.** It states amounts, dates, counts, narrations, and what is
absent — facts read off the records. It never names the exception class, never suggests a
reason code, and never uses vocabulary like "TDS" or "netted" that appears in the seed
corpus as a class label. A summary that hinted at the answer would make retrieval look good
by leaking the label into the query, which is the failure mode that makes an ablation
meaningless.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord

#: Rates a proportional gap is tested against, as fractions. Statutory withholding rates
#: plus common discount rates — the point is to distinguish "a round percentage" from "an
#: arbitrary amount", not to identify *which* deduction it was. Naming the deduction is the
#: agent's job, and doing it here would leak the answer into the query.
ROUND_RATES = (
    Decimal("0.01"), Decimal("0.02"), Decimal("0.05"),
    Decimal("0.075"), Decimal("0.10"), Decimal("0.20"),
)

#: How close a gap must sit to a round rate to be called one. Wide enough to survive the
#: paise-level rounding a customer's own deduction calculation introduces.
ROUND_RATE_TOLERANCE = Decimal("0.0015")

_UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def number_in_words(value: int) -> str:
    """Spell an integer 0–99. Exists because BM25 tokenises '10' and 'ten' as unrelated
    terms, so a query that only ever states rates as digits cannot match a corpus that
    states them as words. See `ReconciliationCase.observations`."""
    if not 0 <= value <= 99:
        return str(value)
    if value < 20:
        return _UNITS[value]
    tens, units = divmod(value, 10)
    return _TENS[tens] if units == 0 else f"{_TENS[tens]}-{_UNITS[units]}"


def format_paise(amount_paise: int) -> str:
    """Integer paise to a readable rupee string. Presentation only — never arithmetic."""
    sign = "-" if amount_paise < 0 else ""
    rupees, paise = divmod(abs(amount_paise), 100)
    return f"{sign}INR {rupees:,}.{paise:02d}"


@dataclass(frozen=True)
class ReconciliationCase:
    """One unit of work: the records that should account for each other, and do not."""

    case_id: str
    payments: list[PaymentRecord] = field(default_factory=list)
    bank_lines: list[BankLineRecord] = field(default_factory=list)
    ledger_entries: list[LedgerEntryRecord] = field(default_factory=list)

    def net_settlement_paise(self) -> int:
        """What the payments should settle to after processor fee and tax on that fee.

        The distinction Ring 0 got wrong once already (see FAILURES.md): the bank credit is
        net, the ledger expectation is gross. Fees are per payment, so they are netted per
        payment and then summed — never summed and netted once.
        """
        return sum(p.amount_paise - p.fee_paise - p.tax_paise for p in self.payments)

    def expected_paise(self) -> int:
        return sum(entry.expected_amount_paise for entry in self.ledger_entries)

    def credited_paise(self) -> int:
        return sum(line.amount_paise for line in self.bank_lines if line.direction == "credit")

    def observations(self) -> list[str]:
        """Structural facts a rules engine can compute, stated in words.

        Every line here is derived arithmetic — counts, equalities, proportions — phrased as
        an observation rather than a conclusion. None names an exception class or a reason
        code; the agent still has to decide what the facts mean.

        **Why this exists.** Without it, three of the nine exception classes retrieve at
        exactly zero. They are separated from each other by arithmetic, not vocabulary: a
        withholding shortfall and a refund shortfall produce identical prose and differ only
        in whether the gap is a round percentage. Lexical retrieval cannot see that, and
        cannot bridge "10.00%" to "ten percent" either. Stating the computed relationship in
        words is what makes the corpus reachable at all.

        **The honest limitation.** These observations were chosen knowing which nine classes
        the dataset contains, so they are fitted to this taxonomy. A tenth exception class
        would arrive with no observation phrased to distinguish it, and would retrieve badly
        for the same reason these three did. That is a real ceiling on how far this
        generalises, and no eval number here measures it.
        """
        notes: list[str] = []

        orders = {payment.order_id for payment in self.payments}
        credits = [line for line in self.bank_lines if line.direction == "credit"]

        if len(self.payments) > 1 and len(orders) == 1:
            notes.append(
                f"{number_in_words(len(self.payments))} separate payments share the same "
                "order reference."
            )
        if len(self.payments) > 1 and len(orders) > 1 and len(credits) == 1:
            notes.append(
                f"{number_in_words(len(self.payments))} payments across "
                f"{number_in_words(len(orders))} distinct order references settle against a "
                "single bank credit; the credit matches no individual payment."
            )
        if len(self.ledger_entries) > 1 and len({e.order_id for e in self.ledger_entries}) == 1:
            notes.append(
                f"{number_in_words(len(self.ledger_entries))} open ledger entries share one "
                "order reference, so a single credit would have to close more than one."
            )
        if not self.payments and credits:
            notes.append(
                "No captured payment record exists behind this credit, so no processor fee "
                "was deducted and the amount that arrived is gross."
            )
        if self.payments and not credits:
            notes.append(
                "No bank statement line is present for a payment that was captured."
            )
        if not self.ledger_entries and self.payments:
            notes.append("No open ledger entry exists for this order reference.")
        if self.ledger_entries and not self.payments and not credits:
            notes.append("An invoice is open with neither a payment nor a credit against it.")

        # The counterparty, named. For most classes this is noise; for the counterparty
        # classes (negotiated_rebate, advance_adjusted) it is the *only* discriminating
        # evidence there is, because the shortfall itself is indistinguishable from a
        # refund. A precedent deposited about this customer can only be retrieved if the
        # query says who the customer is.
        #
        # This is a fact read off the ledger entry, not a label: it names who, never what
        # kind of case it is.
        counterparties = sorted({e.customer_name for e in self.ledger_entries if e.customer_name})
        if counterparties:
            notes.append(f"The counterparty on this invoice is {', '.join(counterparties)}.")

        # Two deductions operate at different levels and must never be conflated — the
        # mistake the `tds_and_psp_fee_stacked` seed precedent exists to warn about, and
        # which an earlier version of this method made:
        #
        #   ledger expects  --(what the customer withheld)-->  payment gross
        #   payment gross   --(processor fee + tax on it)   -->  bank credit
        #
        # Measuring ledger-to-bank in one step blends the two, and a withholding deduction
        # then stops looking like a round percentage of anything.
        expected = self.expected_paise()
        gross_paid = sum(payment.amount_paise for payment in self.payments)
        settled = self.net_settlement_paise()
        credited = self.credited_paise()
        duplicated_order = len(self.payments) > 1 and len(orders) == 1

        # Level 1 — customer side. What was invoiced against what was actually paid.
        if self.payments and expected > 0 and not duplicated_order:
            withheld = expected - gross_paid
            if withheld > 0:
                rate = Decimal(withheld) / Decimal(expected)
                match = next(
                    (r for r in ROUND_RATES if abs(rate - r) <= ROUND_RATE_TOLERANCE), None
                )
                if match is not None:
                    percent = match * 100
                    spelled = (
                        f"{number_in_words(int(percent))} percent"
                        if percent == int(percent)
                        else f"{match:.2%}"
                    )
                    notes.append(
                        f"The customer paid less than the invoiced amount, and the amount "
                        f"kept back is a round proportion of the invoice — {spelled} of it — "
                        f"not a fixed sum."
                    )
                elif withheld < 100:
                    notes.append(
                        "The customer paid under one rupee less than the invoiced amount, "
                        "with no proportional relationship to the value."
                    )
                else:
                    notes.append(
                        "The customer paid less than the invoiced amount by a figure that is "
                        "neither a round proportion of it nor under a rupee."
                    )
            elif withheld == 0:
                notes.append("The customer paid the invoiced amount in full.")

        # Level 2 — processor side. What was paid against what actually reached the bank.
        if self.payments and credits and not duplicated_order:
            unexplained = settled - credited
            if unexplained == 0:
                notes.append(
                    "The bank credit equals the total the payments settle to once each "
                    "payment's own fee and tax are deducted; nothing else was taken out."
                )
            elif unexplained > 0:
                notes.append(
                    "The bank credit is smaller than the payments settle to even after each "
                    "payment's own fee and tax are deducted. Something further reduced the "
                    "amount that arrived, and no fee rate accounts for it."
                )
            else:
                notes.append(
                    "More money arrived in the bank than the captured payments settle to."
                )

        return notes

    def retrieval_query(self) -> str:
        """What the corpus is searched with: the computed observations, nothing else.

        Deliberately *not* `summarize()`. The per-record detail there is mostly boilerplate
        shared by every case, and including it buries the few sentences that actually
        discriminate between exception classes. See the module docstring for the measurement
        that settled this.
        """
        return "\n".join(self.observations())

    def summarize(self) -> str:
        """Mechanical description. Facts only — no class names, no suggested reason code."""
        lines: list[str] = []

        if self.payments:
            lines.append(f"{len(self.payments)} captured payment(s):")
            # Secondary key on the id: ties on the primary key would otherwise let the caller's
            # input ordering leak into the query text and make retrieval unreproducible.
            for payment in sorted(self.payments, key=lambda p: (p.captured_at, p.payment_id)):
                lines.append(
                    f"  - order {payment.order_id}: gross {format_paise(payment.amount_paise)}, "
                    f"processor fee {format_paise(payment.fee_paise)}, "
                    f"tax on fee {format_paise(payment.tax_paise)}, "
                    f"settles to {format_paise(payment.amount_paise - payment.fee_paise - payment.tax_paise)}, "
                    f"captured {payment.captured_at}"
                )
        else:
            lines.append("No captured payment record exists for this case.")

        if self.bank_lines:
            lines.append(f"{len(self.bank_lines)} bank statement line(s):")
            for line in sorted(self.bank_lines, key=lambda b: (b.value_date, b.line_id)):
                narration = line.narration or "(no narration)"
                lines.append(
                    f"  - {line.direction} {format_paise(line.amount_paise)} "
                    f"value date {line.value_date}, narration {narration!r}"
                )
        else:
            lines.append("No bank statement line is present for this case.")

        if self.ledger_entries:
            lines.append(f"{len(self.ledger_entries)} open ledger entry/entries:")
            for entry in sorted(self.ledger_entries, key=lambda e: e.entry_id):
                lines.append(
                    f"  - order {entry.order_id}, invoice {entry.invoice_no}, "
                    f"customer {entry.customer_name!r}, terms {entry.terms}, "
                    f"expects {format_paise(entry.expected_amount_paise)}"
                )
        else:
            lines.append("No open ledger entry exists for this case.")

        lines.append(
            f"Totals: payments settle to {format_paise(self.net_settlement_paise())}; "
            f"bank credits total {format_paise(self.credited_paise())}; "
            f"ledger expects {format_paise(self.expected_paise())}."
        )

        gap = self.expected_paise() - self.credited_paise()
        if gap and self.expected_paise():
            share = gap / self.expected_paise()
            lines.append(
                f"Ledger expectation exceeds bank credits by {format_paise(gap)}, "
                f"which is {share:.2%} of the expected amount."
            )

        observations = self.observations()
        if observations:
            lines.append("Observations:")
            lines.extend(f"  - {note}" for note in observations)

        return "\n".join(lines)

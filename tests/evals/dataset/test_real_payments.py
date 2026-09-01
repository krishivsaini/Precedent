from evals.dataset.real_payments import load_real_payments


def test_loads_at_least_twenty_real_payments():
    payments = load_real_payments()
    assert len(payments) >= 20


def test_every_loaded_payment_is_tagged_as_the_real_source():
    payments = load_real_payments()
    assert all(p.source == "razorpay" for p in payments)


def test_every_loaded_payment_is_captured():
    payments = load_real_payments()
    assert all(p.status == "captured" for p in payments)


def test_payment_ids_are_unique():
    payments = load_real_payments()
    ids = [p.payment_id for p in payments]
    assert len(ids) == len(set(ids))

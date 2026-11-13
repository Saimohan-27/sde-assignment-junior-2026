from solution.pipeline import normalize_phone, parse_date, parse_decimal, parse_timestamp

def test_phone_formats():
    assert normalize_phone('91-77187-50036') == '+917718750036'
    assert normalize_phone('07718750036') == '+917718750036'
    assert normalize_phone('+91 77187 50036') == '+917718750036'
    assert normalize_phone('5718750036') is None
    assert normalize_phone('N/A') is None

def test_dates_day_first_and_impossible():
    assert parse_date('01-Dec-2026') == '2026-12-01'
    assert parse_date('03/10/2026') == '2026-10-03'
    assert parse_date('31/02/2026') is None

def test_decimal_currency():
    assert parse_decimal('₹12,345') == '12345.00'
    assert parse_decimal('Rs. 99.999') == '100.00'
    assert parse_decimal('-1') is None

def test_naive_timestamp_is_ist():
    assert parse_timestamp('2026-10-03 10:01:00').isoformat() == '2026-10-03T10:01:00+05:30'

from solution.pipeline import choose_effective_disposition, lead_bucket

def test_override_precedes_even_a_later_set():
    events = [
        ('2026-10-01T10:00:00+05:30', 'disposition.set', 'renewed'),
        ('2026-10-01T11:00:00+05:30', 'disposition.override', 'callback_requested'),
        ('2026-10-01T12:00:00+05:30', 'disposition.set', 'not_interested'),
    ]
    assert choose_effective_disposition(events) == 'callback_requested'

def test_bucket_priority():
    dispositions = {('wf','success'):(True,True), ('wf','callback'):(False,False)}
    assert lead_bucket([], {}, dispositions, 'wf') == 'yet_to_reach_out'
    calls = [{'telephony_failed': True}, {'telephony_failed': False}]
    assert lead_bucket(calls, {'c1':'success'}, dispositions, 'wf') == 'completed'
    assert lead_bucket([{'telephony_failed': True}], {}, dispositions, 'wf') == 'in_progress_telephony_failed'
    assert lead_bucket([{'telephony_failed': False}], {}, dispositions, 'wf') == 'in_progress_dialed'

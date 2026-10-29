from splitledger.split.edit import cents_of


# prices as people type them, with a dot or a comma, and nonsense refused
def test_cents_of():
    assert cents_of("15") == 1500
    assert cents_of("15.5") == 1550
    assert cents_of(" 3,50 ") == 350
    assert cents_of("$6.00") == 600
    assert cents_of("abc") is None
    assert cents_of("1.234") is None
    assert cents_of("") is None

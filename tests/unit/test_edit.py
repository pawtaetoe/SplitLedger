from splitledger.split.edit import cents_of, charges_of


# prices as people type them, with a dot or a comma, and nonsense refused
def test_cents_of():
    assert cents_of("15") == 1500
    assert cents_of("15.5") == 1550
    assert cents_of(" 3,50 ") == 350
    assert cents_of("$6.00") == 600
    assert cents_of("abc") is None
    assert cents_of("1.234") is None
    assert cents_of("") is None


# charges may be negative when the discount is bigger, prices may not
def test_charges_of():
    assert charges_of("8.80") == 880
    assert charges_of(" -5 ") == -500
    assert charges_of("-") is None
    assert charges_of("--5") is None
    assert cents_of("-5") is None

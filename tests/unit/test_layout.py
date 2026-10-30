from splitledger.channels import layout
from splitledger.channels.layout import fit, numbered, split_choice, with_descriptions

CHOICES = [("a", "50/30/20 — needs, wants, savings"), ("b", "Cancel")]


# a title splits into its label and description
def test_split_choice():
    assert split_choice("50/30/20 — needs, wants, savings") == ("50/30/20", "needs, wants, savings")
    assert split_choice("Cancel") == ("Cancel", None)


# described choices become bold bullets after a blank line, plain ones are left to the buttons
def test_with_descriptions():
    assert with_descriptions("*Pick a plan*", CHOICES) == (
        "*Pick a plan*\n\n- *50/30/20* — needs, wants, savings"
    )
    assert with_descriptions("Logged", [("u", "Undo")]) == "Logged"


# numbered choices show each option once with a hint at the end
def test_numbered():
    assert numbered("*Pick a plan*", CHOICES) == (
        "*Pick a plan*\n\n*1* · 50/30/20 — needs, wants, savings\n*2* · Cancel\n\n"
        "_Reply with a number_"
    )


# short labels stay whole, long ones are cut to the limit with an ellipsis
def test_fit():
    assert fit("Undo") == "Undo"
    cut = fit("12. Garlic butter prawns")
    assert cut == "12. Garlic butter p…"
    assert len(cut) == layout.BUTTON_CHARS
    assert fit("12. Garlic butter prawns", row=True) == "12. Garlic butter prawns"
    assert fit("Chilli crab with fried mantou", row=True) == "Chilli crab with fried…"

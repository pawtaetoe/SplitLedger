import json

from splitledger.plans.frameworks import Framework
from splitledger.split.tokens import ClaimTokens
from splitledger.web import plan as plan_module
from splitledger.web.plan import PlanForm, PlanPage, as_json

TOKENS = ClaimTokens("secret")
PAGE = PlanPage(None, None, TOKENS, None)


# presets, a chosen savings share and a signed drafted plan all become frameworks
def test_framework_of():
    fifty = PAGE.framework_of(PlanForm("503020"))
    assert fifty is not None and fifty.buckets == {"Needs": 50, "Wants": 30, "Savings": 20}
    fifteen = PAGE.framework_of(PlanForm("pyf", "15"))
    assert fifteen is not None and fifteen.buckets == {"Savings": 15, "Spending": 85}
    assert PAGE.framework_of(PlanForm("pyf", "33")) is None
    own = Framework("custom", "Your own plan", "", "", {"Savings": 30, "Food": 0},
                    {"Dining": "Food"}, {"Food": 40000})
    sealed = TOKENS.seal(json.dumps(as_json(own)))
    assert PAGE.framework_of(PlanForm("custom", custom=sealed)) == own
    edited = sealed.replace("30", "90", 1)
    assert PAGE.framework_of(PlanForm("custom", custom=edited)) is None


# the preview shows amounts, or the one thing to fix
def test_preview():
    ok = PAGE._preview(PlanForm("503020", pay="4,500"), "SGD")
    assert ok["problem"] is None
    assert ok["amounts"] == [
        ("Needs", 225000, False), ("Wants", 135000, False), ("Savings", 90000, True)
    ]
    weekly = PAGE._preview(PlanForm("503020", pay="4500", period="weekly"), "SGD")
    assert weekly["unit"] == "week" and weekly["amounts"][0][1] == 225000 * 12 // 52
    assert PAGE._preview(PlanForm("503020", pay="lots"), "SGD")["problem"] == plan_module.NEED_PAY
    assert PAGE._preview(PlanForm("custom", pay="4500"), "SGD")["problem"] == plan_module.NEED_DRAFT

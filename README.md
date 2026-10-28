# SplitLedger

**Your money, in the chat you already use.**

SplitLedger is a WhatsApp bot that does two jobs. First, it tracks your own spending: you log by
text, voice note or receipt photo, then ask questions about it, set a budget plan and check
whether you can afford something. Second, it splits group bills. Snap the receipt with the caption
`split` and forward one message. Each friend replies with the numbers of the dishes they had.
When everyone is in, the bill closes itself and everyone is told what they owe. Simple things
stay in the chat. Anything with a list (ticking dishes, the dashboard, the plan form) opens on a
web page from a link.

Built for the Meta Global AI Developer Hackathon (Indie track), Oct 27 to Nov 4 2026.

![Architecture](docs/architecture.svg)

## Try it in 30 seconds

| You send | You get |
| --- | --- |
| `kopi 2.20` | ✅ Logged SGD 2.20 · Dining, what is left in your plan, an Undo button |
| a voice note "grab to office twelve eighty" | Logged SGD 12.80 · Transport |
| a receipt photo | items and total, Save or Cancel |
| a receipt photo with caption `split` | a bill code and one message to forward to friends |
| friend: taps the link, sends `CLAIM K7Q2AB`, replies `2, 6` | their share so far, a dish picked by two is shared |
| `how much did I spend on food this month?` | a total and the top items, from the database |
| `can I afford a 300 hotel?` | spent, still due, really left, a rule of thumb |
| `menu` | every action as a tap choice, including My dashboard |

## Which Meta model does what

| Job | Model | Guard in our code |
| --- | --- | --- |
| Read any message: intent and expense | Muse Spark, JSON schema | 8 s timeout, keyword fallback |
| Receipt photo to items | Muse Spark vision | items must add up to the total |
| Voice note to text | Muse Voice Transcribe | goes through the same reader |
| Answer spending questions | Muse Spark tool calls | numbers come from SQL tools, 4 rounds max |
| "Anna and Ben shared the calamari" | Muse Spark, JSON schema | shown back with Looks right or Change first |
| Describe your own budget plan | Muse Spark, JSON schema | shares must total 100 |

The models read. Python checks every number. Money is stored in integer cents and every
bill split adds up to the cent.

## Run it locally

You need Docker, [uv](https://docs.astral.sh/uv/) and a Meta Model API key.

```bash
git clone <this repo> && cd SplitLedger
cp .env.example .env        # set LOCALE=sg, META_API_KEY and LLM_MODEL
make setup                  # install packages and git hooks
make dev                    # start postgres, run migrations, serve on :8000
open http://localhost:8000/dev/chat
```

`/dev/chat` is a WhatsApp-style simulator, so no phone is needed. To use real WhatsApp, follow
[docs/DEPLOY.md](docs/DEPLOY.md) (Render plus the Twilio sandbox, about 15 minutes).

Other commands:

- `make check`: lint, strict types, tests
- `make seed-dev PHONE=6581234567`: a month of sample history

## How the code is laid out

| Folder | What lives there |
| --- | --- |
| `channels/` | WhatsApp Cloud API, Twilio sandbox and the dev simulator, behind one Transport |
| `handlers/` | the router and one handler per job: log, ask, plan, bills, group bill, claim |
| `llm/` | the Meta Model API client, prompts and response schemas |
| `split/` | splitting money, claims, closing a bill into ledgers, dashboard, exchange rates |
| `plans/` | budget frameworks, quotas, affordability rules, upcoming bills |
| `web/` | the claim page, dashboard and plan form (Jinja and HTMX) |
| `jobs/` | the reminder loop: due bills and bill nudges |

Region specific bits (Singapore words like kopi and MRT, MAS loan rules) live in
`*_sg.py` packs picked by `LOCALE`. The core works in any currency.

## Known limits

- **No group bot.** WhatsApp has no API for bots in group chats, so the payer forwards one
  message and each friend talks to the bot one to one.
- **24 hour window.** The bot can only message someone within 24 hours of their last message.
  Reminders outside that window need a Meta approved template, which waits on business
  verification.
- **Test numbers only.** The Twilio sandbox only reaches phones that have joined it.
- **Not financial advice.** Affordability answers are rules of thumb with the numbers shown.

## Roadmap

| Next | What it adds |
| --- | --- |
| Pay links and settle up | One tap to pay a friend by PayNow, then the debt clears |
| Weekly summary | A Sunday message with the week's spending against the plan |
| Payment screenshots | Share a GPay or bank confirmation and it logs like a receipt |
| Google Wallet budget card | A pass showing what is left this week, refreshed after every log |
| More region packs | India (UPI), Malaysia (DuitNow), Thailand (PromptPay), Brazil (Pix) |
| Auto-import | Spending appears without typing, through bank open-banking APIs |

## Built during the hackathon

The first commit is `15c5adb` on 2026-10-27. Third party, not ours: Meta Muse Spark and Muse
Voice Transcribe, WhatsApp and Twilio, FastAPI, SQLAlchemy, Postgres, HTMX. Everything else in
`src/`, `tests/` and `scripts/` was written for this hackathon.

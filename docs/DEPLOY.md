# Deploy on Render

About 15 minutes. After this the bot runs without your laptop or ngrok, and
every push to `main` deploys by itself.

Railway works too (`railway.json` is kept), but its sign up is paused for now.

## 1. Create everything from the blueprint

1. Sign in at render.com with GitHub.
2. **New** → **Blueprint** → pick the SplitLedger repo.
   Render reads `render.yaml` and shows a web service `splitledger` and a
   database `splitledger-db`.
3. It asks for the values marked secret. Paste them from your `.env`:
   `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`,
   `META_API_KEY`, `LLM_MODEL`, `EXCHANGE_RATE_API_KEY`.
   Leave `PUBLIC_BASE_URL` empty for now.
4. **Apply**. The first build takes a few minutes.

`DATABASE_URL` and a fresh `APP_SECRET` are filled in for you, and the
database tables are created on every start.

## 2. Set the public address

Open the `splitledger` service. Its address is at the top, for example
`https://splitledger.onrender.com`. Copy it into **Environment** →
`PUBLIC_BASE_URL` → **Save**. Render redeploys.

Check it works: open `<address>/healthz`, it should say `{"ok":true}`.

## 3. Point WhatsApp at it

Twilio console → **Messaging** → **Try it out** → **WhatsApp sandbox
settings** → **When a message comes in**: `<address>/webhook/twilio`,
method POST, **Save**.

Later, with the Meta number: set `WHATSAPP_PROVIDER=meta` plus the
`WHATSAPP_*` values, and the webhook in the Meta app to
`<address>/webhook/whatsapp`.

## 4. Try it

From your phone send `coffee 4.50`. The logged reply should come back in a
few seconds. If not: service → **Logs**.

## Costs and limits

- The web service is on the Starter plan so it never sleeps. On the free
  plan it sleeps after 15 minutes without messages, takes about a minute to
  wake, and WhatsApp messages sent meanwhile are lost.
- The free database is deleted 30 days after it is created, which covers
  judging. Upgrade it in Render to keep the data after that.
- Links sent before the move still point at ngrok. Send `CLAIM <code>` or
  `dashboard` again for new ones.
- Run one copy only, reminders and auto close run inside the service.

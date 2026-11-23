# Free API keys for the token pool

<!-- Generated from provenance_core/pool_help.yaml by `python -m provenance_core.pool_help`. Edit the YAML. -->

How to get a free key from each provider the pool can use, most capacity for least effort first. Read from the providers' own public pages on 2026-11-23; free tiers and button labels change often, so where a page differs from this, the page is right. Paste each key into the app's field for that provider (or `PUT /pool/keys/{id}` with `{"values": {"<VARIABLE>": "<key>"}}`; a further key from another account of yours with `POST /pool/keys/{id}` `{"key": "<key>"}`). The same data is served as JSON by `GET /pool/help`.

Confidence: **seen** = the provider's current docs name the controls; **docs-only** = the docs say where the key is made but the page is behind a sign-in, so its labels are not given; **uncertain** = the only walkthrough is dated or the docs disagree.

| # | Provider | Free | Needs | Minutes | Confidence |
|---|---|---|---|---|---|
| 1 | [Kilo AI Gateway](#kilo) | works without a key | Nothing. No account, no key. | 0 | seen |
| 2 | [Google Gemini API (Google AI Studio)](#google) | free tier | A Google account, age 18 or over, a supported country. Some accounts are asked for a one-time phone check (scan a QR code; no phone number is given). No card. | 3 | seen |
| 3 | [Groq (GroqCloud)](#groq) | free tier | A Groq account (the sign-in options could not be read without signing in). No card for the Free plan. | 3 | docs-only |
| 4 | [OpenRouter](#openrouter) | free tier | An OpenRouter account, age 18 or over (the sign-in options could not be read without signing in). No card for the 50/day allowance; a one-time US$10 purchase for 1,000/day. | 4 | docs-only |
| 5 | [Cloudflare Workers AI](#cloudflare) | free tier | A Cloudflare account. No card on the Workers Free plan (the sign-up page itself could not be read). | 5 | seen |
| 6 | [Requesty](#requesty) | free tier | A Requesty account (the sign-in options could not be read without signing in). No card for the free models. | 3 | docs-only |
| 7 | [Z.ai (Zhipu) API](#zai) | free tier | A Z.ai account (email and password, or Google or GitHub sign-in). No card stated for the free model. | 3 | docs-only |
| 8 | [NVIDIA API Catalog (build.nvidia.com)](#nvidia) | free tier | An email address for an NVIDIA account (signing in joins the free NVIDIA Developer Program and accepts the NVIDIA Technology Access Terms of Use). NVIDIA may send a confirmation to that address that has to be approved before the sign-in goes on, so the mailbox must be at hand. A phone number that can receive a one-time code (required before a key can be generated; some countries are not offered yet). No card. | 8 | seen |
| 9 | [Cohere](#cohere) | free tier | A Cohere account (email and password, or Google or GitHub sign-in). No card. Not available from Belarus, China (with Hong Kong and Macau), Iran, North Korea, Russia or Syria. | 3 | docs-only |
| 10 | [SambaNova (SambaCloud)](#sambanova) | free tier | A SambaCloud account, with your first and last name, job title and company name. Possibly payment details (not known whether that step can be skipped). The docs say no card; linking one moves the account to the Developer Tier (60 requests/minute, 12,000 requests/day, pay per token). | 3 | docs-only |
| 11 | [Inception Labs (Mercury)](#inception) | one-time trial | An Inception Platform account you already have (sign-in by Continue with Google, Continue with GitHub, or email and password). New accounts cannot be made. No payment details. | 3 | seen |
| 12 | [OVHcloud AI Endpoints](#ovh) | works without a key | Nothing for anonymous use. | 0 | seen |

<a id="kilo"></a>
## 1. Kilo AI Gateway (`kilo`)

The gateway's free models (the `:free` Nemotron, ERNIE, Flint, Particle, Ling models and others) with no account and no key: 200 requests per hour per IP address, shared by all free models.

- **Needs:** Nothing. No account, no key.
- **Sign up:** <https://app.kilo.ai>
- **Key page:** <https://app.kilo.ai>
- **Goes into:** `KILO_API_KEY`
- **Key looks like:** Not needed. A Kilo account key is a JWT (three dot-separated parts, starting `eyJ`).

Steps:

1. Nothing to do. The pool calls Kilo's free models without a key and they work as soon as the router runs.
2. Optional, and no extra free capacity: if you have a Kilo account, its key can be saved and is then sent with the requests. Go to **app.kilo.ai**, open **Your Profile** on your personal account (not an organization), scroll to the bottom of the page and copy your API key.

Good to know:

- The 200 requests/hour limit is per IP address and is the same signed in or not ("All free model requests (both anonymous and authenticated) are rate-limited by IP address"), so a key adds nothing for free models, and neither does a second one.
- Kilo's docs on the NVIDIA free endpoints: "Trial use only - do not submit personal or confidential data. Your use is logged for security purposes and to improve NVIDIA products and services."
- Other free models may be routed to providers that log prompts and outputs and use them to improve their services.
- Which models are free changes; the live list is https://ai-gateway.kilo.ai/api/v1/models (all the catalog's Kilo models were on it on the checked date).

Terms: Kilo's terms allow one account per person or entity; if you route other people's requests through the gateway you are responsible for their compliance with the terms.

Keys from several accounts: the provider's terms forbid a second account. Kilo's terms: an individual or entity "may not have more than one Account" (employees of one organization may each have their own). The free limit is per IP address anyway, so a second key adds nothing.

Checked 2026-11-23 (seen). Sources: <https://kilo.ai/docs/gateway/authentication>, <https://kilo.ai/docs/gateway/usage-and-billing>, <https://kilo.ai/docs/gateway/models-and-providers>, <https://kilo.ai/docs/gateway/api-reference>, <https://kilo.ai/docs/getting-started/setup-authentication>, <https://kilo.ai/terms>, <https://ai-gateway.kilo.ai/api/v1/models>

<a id="google"></a>
## 2. Google Gemini API (Google AI Studio) (`google`)

The Gemini Flash and Flash-Lite models, Gemini 4 Hydrogen and Gemma, free of charge on the free tier. The per-model requests/minute and requests/day are unstated in the docs: they are shown per project on https://aistudio.google.com/rate-limit after signing in. Gemini 4 Carbon, Argon and Neon are not on the free tier.

- **Needs:** A Google account, age 18 or over, a supported country. Some accounts are asked for a one-time phone check (scan a QR code; no phone number is given). No card.
- **Sign up:** <https://aistudio.google.com>
- **Key page:** <https://aistudio.google.com/api-keys>
- **Goes into:** `GEMINI_API_KEY`
- **Key looks like:** Not documented. Keys made since 2026-05-28 are "auth keys"; expect one long string with no spaces.

Steps:

1. Open https://aistudio.google.com/api-keys and sign in with your Google account.
2. Accept the Terms of Service when asked. For a new user AI Studio then creates a default Google Cloud project and an API key by itself.
3. If no key is listed, click **Create API key**.
4. If AI Studio asks for verification, scan the QR code it shows with your phone and follow it through.
5. Copy the key from the API Keys page and paste it into the app.

Good to know:

- Free-tier prompts and answers are used by Google: "Google uses the content you submit to the Services and any generated responses to provide, improve, and develop Google products and services and machine learning technologies", and "human reviewers may read, annotate, and process your API input and output". Google says not to submit sensitive, confidential or personal information to the unpaid service.
- In the EEA, Switzerland and the UK the paid-service data terms apply to the free quota too (no such use of your content).
- Limits are per project, not per key, so a second key in the same project adds nothing.
- Limits are per project: a key from another Google account of yours (another project) has its own allowance and can be added as a further key. A paid-tier key has higher limits than the free numbers the pool assumes; say so when adding it (tier `paid`, and its limits).
- If you signed in to Gemini in the app, the app can open this page in that same signed-in Google window (`open_with` in GET /pool/help, POST /web/open). You still create and copy the key yourself; nothing on the page is clicked or read for you.
- If you already have a Google Cloud account, AI Studio creates no default project; import one under **Dashboard** > **Projects** > **Import projects**, then create the key on the **API Keys** page. An imported project may need the phone verification before it gets the free tier.
- Do not attach a Cloud Billing account to the project unless you mean to pay; with billing on, the project is on the paid tier.
- Flash models often answer 503 under load; the router treats that as temporary.

Terms: Google's terms say the API is "for developers building with Google AI models for professional or business purposes, not for consumer use", require age 18+, and allow only paid services when you make a client available to users in the EEA, Switzerland or the UK.

Keys from several accounts: read the terms first. The Gemini API terms and rate-limit page read on the checked date say nothing about several accounts ("Rate limits are applied per project, not per API key"). The Google APIs Terms of Service they incorporate were not read; read them before adding a second account's key.

Checked 2026-11-23 (seen). Sources: <https://aistudio.google.com/docs/api-key>, <https://aistudio.google.com/docs/verification>, <https://aistudio.google.com/docs/rate-limits>, <https://aistudio.google.com/docs/pricing>, <https://aistudio.google.com/docs/available-regions>, <https://aistudio.google.com/docs/terms>, <https://aistudio.google.com/docs/openai>

<a id="groq"></a>
## 3. Groq (GroqCloud) (`groq`)

openai/gpt-oss-120b, openai/gpt-oss-20b and qwen/qwen3.8-27b on the Free plan: each 30 requests/minute, 1,000 requests/day, 8,000 tokens/minute, 200,000 tokens/day.

- **Needs:** A Groq account (the sign-in options could not be read without signing in). No card for the Free plan.
- **Sign up:** <https://console.groq.com/home>
- **Key page:** <https://console.groq.com/keys>
- **Goes into:** `GROQ_API_KEY`
- **Key looks like:** Starts with `gsk_`.

Steps:

1. Open https://console.groq.com/keys and sign up or log in.
2. On the **API Keys** page click **Create API Key**.
3. Give the key a name such as `provenance` and confirm (the dialog's own labels could not be read without signing in).
4. Copy the key when it is shown and paste it into the app.

Good to know:

- Limits apply to the whole organization, not per key or per user.
- Keys belong to the project selected in the console; a new account has one, so there is nothing to choose.
- Groq says it does not retain inference data by default and may not use inputs or outputs for training unless you allow it.
- qwen/qwen3.8-27b is a Preview model, which Groq says may be discontinued at short notice.
- The docs' table of limits has a Free and a Developer tab; the numbers above are the table as served, which match the Free plan in the catalog. Your exact limits are on https://console.groq.com/settings/limits.

Terms: Groq's services agreement says the services "are not for consumer use", that an account may not be resold or leased, and forbids use "in a manner intended to avoid incurring Fees".

Keys from several accounts: the provider's terms forbid a second account. Groq's acceptable use policy forbids use "beyond published parameters, rate limits, or use limitations, including by registering multiple accounts or orchestrating usage between multiple organizations". Limits are per organization, so a second key of the same organization adds nothing either.

Checked 2026-11-23 (docs-only). Sources: <https://console.groq.com/docs/quickstart>, <https://console.groq.com/docs/rate-limits>, <https://console.groq.com/docs/models>, <https://console.groq.com/docs/billing-faqs>, <https://console.groq.com/docs/projects>, <https://console.groq.com/docs/your-data>, <https://console.groq.com/docs/production-readiness/security-onboarding>, <https://console.groq.com/docs/legal/services-agreement>, <https://console.groq.com/docs/legal/ai-policy>, <https://console.groq.com/docs/deprecations>, <https://console.groq.com/keys>

<a id="openrouter"></a>
## 4. OpenRouter (`openrouter`)

Every model whose id ends in `:free` (Nemotron, Gemma, ERNIE and others): 20 requests/minute and 50 requests/day in total, or 1,000 requests/day once the account has bought at least 10 credits (US$10) in all.

- **Needs:** An OpenRouter account, age 18 or over (the sign-in options could not be read without signing in). No card for the 50/day allowance; a one-time US$10 purchase for 1,000/day.
- **Sign up:** <https://openrouter.ai>
- **Key page:** <https://openrouter.ai/settings/keys>
- **Goes into:** `OPENROUTER_API_KEY`
- **Key looks like:** `sk-or-v1-` followed by 64 lowercase hexadecimal characters (73 characters in all).

Steps:

1. Open https://openrouter.ai/settings/keys and sign up or sign in.
2. Create a new key on that page (the button's label could not be read without signing in).
3. Give it a name such as `provenance` and choose a credit limit or no limit, as the form asks.
4. Copy the key and paste it into the app.
5. Open https://openrouter.ai/settings/privacy and, under the settings for free models, allow providers that may train on your data, if you accept that; without it many `:free` models are not routed to and the request fails.

Good to know:

- The 50 (or 1,000) requests/day is one allowance across all `:free` models, not per model.
- The pool assumes 50 a day. If your account has bought 10 credits, tell the app so for this key (tier `paid`, limits `rpd: 1000`) and it uses the 1,000.
- OpenRouter: "Making additional accounts or API keys will not affect your rate limits, as we govern capacity globally."
- Many free models are served by providers that log or train on prompts; OpenRouter only routes to them when you switch that on in the privacy settings ("There are separate settings for paid and free models").
- A negative credit balance causes errors on free models too.
- A key made for you during onboarding has a US$100 credit limit and expires after 180 days; a key you create yourself has the limit you give it.
- OpenRouter's terms say credits must be bought to call the API, while its docs describe the free allowance above; the docs are what the limits follow today.

Terms: OpenRouter's terms forbid creating several accounts to get round usage limits and reselling API access.

Keys from several accounts: the provider's terms forbid a second account. OpenRouter's terms forbid to "create multiple accounts as a single user, for purposes of bypassing or circumventing use limits on the Site or Service or for any other reason"; only an organization's members each have their own.

Checked 2026-11-23 (docs-only). Sources: <https://openrouter.ai/docs/api-reference/limits>, <https://openrouter.ai/docs/api-reference/authentication>, <https://openrouter.ai/docs/faq>, <https://openrouter.ai/docs/guides/privacy/provider-logging>, <https://openrouter.ai/docs/guides/privacy/data-collection>, <https://openrouter.ai/docs/guides/routing/model-variants/free>, <https://openrouter.ai/terms>, <https://openrouter.ai/api/v1/models>

<a id="cloudflare"></a>
## 5. Cloudflare Workers AI (`cloudflare`)

@cf/openai/gpt-oss-120b, @cf/qwen/qwen3.8-27b, @cf/moonshotai/kimi-k2.5, @cf/zai-org/glm-4.7-flash and @cf/google/gemma-4-26b-a4b-it: 10,000 Neurons per day for the whole account across all models (resets at 00:00 UTC), 300 requests/minute.

- **Needs:** A Cloudflare account. No card on the Workers Free plan (the sign-up page itself could not be read).
- **Sign up:** <https://dash.cloudflare.com/sign-up/workers-and-pages>
- **Key page:** <https://dash.cloudflare.com/?to=/:account/ai/workers-ai>
- **Goes into:** `CLOUDFLARE_API_TOKEN`
- **Key looks like:** The token starts with `cfut_` (a user token; `cfat_` for an account token) followed by 40 characters and a checksum. Tokens made before 2026 are 40 letters and digits with no prefix.
- **Also needed, Account ID** (`CLOUDFLARE_ACCOUNT_ID`): On the Workers AI page under **Use REST API** > **Get Account ID**. Also: **Workers & Pages** > the **Account Details** section > the copy button next to **Account ID**; or on Account home, search (Ctrl/Cmd + K) for **Copy account ID**. A string of letters and digits (not your email, not the token).

Steps:

1. Sign up or log in, then open the **Workers AI** page of the dashboard (https://dash.cloudflare.com/?to=/:account/ai/workers-ai).
2. Select **Use REST API**.
3. Select **Create a Workers AI API Token** and review the prefilled information (leave it as it is).
4. Select **Create API Token**, then **Copy API Token**. Paste it into the app as the token; it is shown only once.
5. Back on the same page, under **Get Account ID**, copy the value for **Account ID** and paste it into the app as the account id.

Good to know:

- Two values are needed. The account id goes into the address the router calls; the token alone does nothing.
- If you make the token by hand instead (**My Profile** > **API Tokens** > **Create Token**), it needs both `Workers AI - Read` and `Workers AI - Edit`; then **Continue to summary** > **Create Token**.
- The token secret is "only shown once". Token expiration is optional; leave it empty unless you want the key to stop working.
- On the Workers Free plan, requests fail once the day's 10,000 Neurons are used; nothing is billed.
- Cloudflare says it "does not use Customer Content processed by Workers AI to train AI models".
- Some newer models need a paid billing method; the five in the catalog were not on that list on the checked date. @cf/nvidia/nemotron-3-120b-a12b was deprecated on 2026-11-20 and is no longer in the pool.
- Each Cloudflare account has its own token and its own account id: a further key is added together with that account's id (`extras`: CLOUDFLARE_ACCOUNT_ID).

Keys from several accounts: read the terms first. Cloudflare's self-serve agreement forbids use "intended to circumvent Service-specific usage limits, quotas, or other restrictions"; it does not mention several accounts as such. Read it before adding a second account's token.

Checked 2026-11-23 (seen). Sources: <https://developers.cloudflare.com/workers-ai/get-started/rest-api/>, <https://developers.cloudflare.com/workers-ai/platform/pricing/>, <https://developers.cloudflare.com/workers-ai/platform/limits/>, <https://developers.cloudflare.com/workers-ai/platform/data-usage/>, <https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/>, <https://developers.cloudflare.com/fundamentals/api/get-started/create-token/>, <https://developers.cloudflare.com/fundamentals/api/get-started/token-formats/>, <https://developers.cloudflare.com/fundamentals/account/find-account-and-zone-ids/>, <https://developers.cloudflare.com/workers-ai/models/>, <https://www.cloudflare.com/terms/>

<a id="requesty"></a>
## 6. Requesty (`requesty`)

The gateway's free models (nvidia/nemotron-3-ultra-550b-a55b, google/gemma-4-31b-it, nvidia/nemotron-3.5-lightning-30b-a3b, novita/ling-3.1-flash and others): 200 requests per day and 20 per minute for a new organization, across all free models together. New in the pool on 2026-11-23 and not yet tried with a key.

- **Needs:** A Requesty account (the sign-in options could not be read without signing in). No card for the free models.
- **Sign up:** <https://app.requesty.ai/sign-up>
- **Key page:** <https://app.requesty.ai/api-keys>
- **Goes into:** `REQUESTY_API_KEY`
- **Key looks like:** Not documented; the quickstart's example starts `sk-`.

Steps:

1. Open https://app.requesty.ai/sign-up and sign up, or sign in.
2. Open the **API Keys** page (https://app.requesty.ai/api-keys) and create a key (the button's label could not be read without signing in).
3. Copy the key and paste it into the app.

Good to know:

- "These models are free for now. Pricing may change in the future, and any change will be announced in the changelog ahead of time."
- The 200 requests/day and 20/minute are one allowance per organization across all free models, not per model; an organization that pays gets 1,000 a day and 60 a minute.
- The same key also calls Requesty's paid models, which are billed. The pool only calls the free ones; set a spend limit of 0 on the key if the page offers one.
- Requesty's public model list marks the free NVIDIA Nemotron models as retaining data and using it for training ("NVIDIA API Trial Terms of Service apply."); gemma-4-31b-it as retained but not used for training; ling-3.1-flash as neither.
- Which models are free changes; the live list is https://docs.requesty.ai/features/free-models (one model on that page, nemotron-3-super-120b-a12b, was not in the public model list on the checked date and is left out).
- Requesty's terms page could not be read (it came back empty), so nothing is known here about commercial use or account rules.

Keys from several accounts: read the terms first. Requesty's terms could not be read on the checked date. The free limit is per organization, so a second key of the same organization adds nothing; read the terms before adding another account's key.

Checked 2026-11-23 (docs-only). Sources: <https://docs.requesty.ai/features/free-models>, <https://docs.requesty.ai/quickstart>, <https://router.requesty.ai/v1/models>, <https://www.requesty.ai/pricing>

<a id="zai"></a>
## 7. Z.ai (Zhipu) API (`zai`)

glm-4.7-flash, listed as Free on the pricing page (also GLM-4.5-Flash and GLM-4.6V-Flash). The rate limit is unstated in the docs: it is shown on https://z.ai/manage-apikey/rate-limits after signing in.

- **Needs:** A Z.ai account (email and password, or Google or GitHub sign-in). No card stated for the free model.
- **Sign up:** <https://z.ai/model-api>
- **Key page:** <https://z.ai/manage-apikey/apikey-list>
- **Goes into:** `ZAI_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Open https://z.ai/model-api, click **Login**, and register or log in.
2. Open the **API Keys** page (https://z.ai/manage-apikey/apikey-list).
3. Create a key under **Standard API Key** (not under **Personal Coding Plan**; the button's own label could not be read without signing in).
4. Copy the key and paste it into the app.

Good to know:

- The key type matters since 2026-10-16: a **Standard API Key** is what the general endpoint (api.z.ai/api/paas/v4) uses. A Coding Plan key is for Z.ai's supported coding tools only.
- A Standard key bills paid models against the account balance; with no balance only the free models answer. The pool uses only glm-4.7-flash.
- Z.ai's terms conflict on data use: for "enterprises and developers using API Services" it says it will not use your content to improve its services without your agreement, but for "individual users" it reserves the right to. Assume prompts may be used.
- The service is run from Singapore; the terms exclude a few sanctioned regions.

Terms: Z.ai's terms say not to share your key with others and that you are responsible if you use the service to serve third parties.

Keys from several accounts: nothing against it found. Nothing about several accounts was found in Z.ai's terms on the checked date, apart from registering again after an account was terminated, which they forbid.

Checked 2026-11-23 (docs-only). Sources: <https://docs.z.ai/guides/overview/quick-start>, <https://docs.z.ai/guides/overview/pricing>, <https://docs.z.ai/api-reference/introduction>, <https://docs.z.ai/devpack/notice/api-key-management>, <https://docs.z.ai/help/faq>, <https://docs.z.ai/legal-agreement/terms-of-use>, <https://docs.z.ai/legal-agreement/privacy-policy>

<a id="nvidia"></a>
## 8. NVIDIA API Catalog (build.nvidia.com) (`nvidia`)

The models with a "Free Endpoint" on build.nvidia.com (38 on the checked date; the chat ones in the pool are moonshotai/kimi-k3.1-pro, nvidia/nemotron-3-ultra-550b-a55b, mistralai/mistral-large-4-1t-a52b, nvidia/nemotron-3.5-super-vl-120b-a12b, minimaxai/minimax-m3.1-flash, nvidia/nemotron-3.5-lightning-30b-a3b and z-ai/glm-5.3-flash on the list but not in the pool), free "for prototyping" for NVIDIA Developer Program members. The rate limit is unstated: NVIDIA says it varies per model and is shown under your account on build.nvidia.com. Tried with a key on the checked date: six of the seven answered; z-ai/glm-5.3-flash timed out and was left out.

- **Needs:** An email address for an NVIDIA account (signing in joins the free NVIDIA Developer Program and accepts the NVIDIA Technology Access Terms of Use). NVIDIA may send a confirmation to that address that has to be approved before the sign-in goes on, so the mailbox must be at hand. A phone number that can receive a one-time code (required before a key can be generated; some countries are not offered yet). No card.
- **Sign up:** <https://build.nvidia.com>
- **Key page:** <https://build.nvidia.com>
- **Goes into:** `NVIDIA_API_KEY`
- **Key looks like:** Starts with `nvapi-`.

Steps:

1. Open https://build.nvidia.com and click **Login** at the top right ("Sign In to Get Started with NVIDIA AI").
2. Type your email into **Enter your email ID** and click **Next**.
3. On **Your NVIDIA Account**, enter your password, or use **Log In With Passkey** or **More Login Options** (Log In With Google, Apple, Microsoft, Discord or Facebook; **Show More** for others).
4. If NVIDIA shows a page saying it has sent a confirmation to your email, open that email and approve it, then come back.
5. NVIDIA may offer **Create A Passkey** (choose **Maybe Later**, then **OK**) and "Secure my account" (choose **Not now**); both can be skipped.
6. On "Almost done!" leave the two optional boxes unticked if you do not want recommendations or developer news, and click **Submit**.
7. On **Create an NVIDIA Cloud Account** type any name (for example `provenance`) and click **Create NVIDIA Cloud Account**.
8. Back on build.nvidia.com a dialog says **We'll need to verify your phone number**: choose your **Location**, type your **Phone Number**, click **Send Code to Phone** and enter the code. (A bar at the top says "Please verify your account to get API access" with **Verify** until this is done.)
9. Generate the API key; it is shown once, with its ID, name and expiration. Copy it and paste it into the app.

Good to know:

- The steps above were walked on the checked date up to the phone verification; the last step (generating the key) was done by the account's owner. NVIDIA's only written walkthrough is old: it puts **Get API Key** in the right-hand pane of a model's page, but a model page now shows a **Deploy** pane with paid partner endpoints instead.
- Only models marked **Free Endpoint** are free: the Models page has that filter (https://build.nvidia.com/models?filters=nimType%3Anim_type_preview). A model's page says so under **Model Availability**; meta/llama-3.3-70b-instruct now says "Free Endpoint: Deprecated" and is no longer in the pool.
- The current model ids are on https://integrate.api.nvidia.com/v1/models, which needs no key.
- Access is "for prototyping, research, development and testing purposes only", not production, which NVIDIA defines as "any non-testing activity including activity serving real end-users".
- The key is shown only once, is "for API testing use only" and is valid for 6 months: set a reminder to make a new one.
- NVIDIA's own pages do not say how free-tier prompts are used; through Kilo, NVIDIA's free endpoints are described as logged "to improve NVIDIA products and services".

Terms: Free access through the Developer Program is for prototyping and testing only; production use needs an NVIDIA AI Enterprise license.

Keys from several accounts: read the terms first. NVIDIA's API catalog terms could not be read on the checked date (the page timed out); read them before adding a second account's key.

Checked 2026-11-23 (seen). Sources: <https://docs.api.nvidia.com/nim/docs/api-quickstart>, <https://docs.api.nvidia.com/nim/docs/product>, <https://docs.api.nvidia.com/nim/reference/llm-apis>, <https://build.nvidia.com/>, <https://forums.developer.nvidia.com/t/nvidia-nim-faq/300317>, <https://build.nvidia.com/models>, <https://integrate.api.nvidia.com/v1/models>

<a id="cohere"></a>
## 9. Cohere (`cohere`)

command-a-plus-05-2026 with a trial key: 1,000 API calls a month in total and 20 requests/minute on Chat.

- **Needs:** A Cohere account (email and password, or Google or GitHub sign-in). No card. Not available from Belarus, China (with Hong Kong and Macau), Iran, North Korea, Russia or Syria.
- **Sign up:** <https://dashboard.cohere.com/welcome/register>
- **Key page:** <https://dashboard.cohere.com/api-keys>
- **Goes into:** `COHERE_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Open https://dashboard.cohere.com/welcome/register and sign up with **Continue with Google**, **Continue with Github**, or an email and password.
2. A trial key is created for you automatically. Open **API Keys** in the dashboard (https://dashboard.cohere.com/api-keys).
3. Copy the trial key (not a production key, which is billed) and paste it into the app.
4. Optional: under **Settings** > **Data Controls**, switch the toggle off so your prompts are not used to train Cohere's models.

Good to know:

- Cohere: trial keys "are not permitted to be used for production or commercial purposes".
- 1,000 calls a month is about 33 a day.
- Prompts and generations may be used to train Cohere's models unless you opt out under **Data Controls**; Cohere says it deletes them after 30 days.
- A production key is pay-as-you-go; the pool only needs the trial key.

Terms: Trial keys are for evaluation, not production or commercial use; Cohere's agreement also forbids sharing access with others.

Keys from several accounts: the provider's terms forbid a second account. Cohere's terms of use: you agree to "not register for more than one account".

Checked 2026-11-23 (docs-only). Sources: <https://docs.cohere.com/docs/rate-limits>, <https://docs.cohere.com/docs/going-live>, <https://docs.cohere.com/docs/compatibility-api>, <https://docs.cohere.com/docs/models>, <https://cohere.com/pricing>, <https://cohere.com/data-usage-policy>, <https://cohere.com/saas-agreement>, <https://cohere.com/terms-of-use>, <https://dashboard.cohere.com/welcome/register>

<a id="sambanova"></a>
## 10. SambaNova (SambaCloud) (`sambanova`)

gpt-oss-120b and gemma-4-31B-it on the Free Tier: each 20 requests/minute, 20 requests/day and 200,000 tokens/day. Small, and a new account may now be asked for payment details (see below).

- **Needs:** A SambaCloud account, with your first and last name, job title and company name. Possibly payment details (not known whether that step can be skipped). The docs say no card; linking one moves the account to the Developer Tier (60 requests/minute, 12,000 requests/day, pay per token).
- **Sign up:** <https://cloud.sambanova.ai>
- **Key page:** <https://cloud.sambanova.ai/apis>
- **Goes into:** `SAMBANOVA_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Open https://cloud.sambanova.ai/apis and sign up or log in (**Login/Signup**).
2. A new account lands on "Welcome! Set up your account in a few quick steps": **Profile**, **Payment**, **Credits**. On **Profile**, fill in **First Name**, **Last Name**, **Job Title** and **Company Name** (Email and Country are filled in, Contact Number is optional) and click **Save**.
3. On **Payment**: whether this step can be skipped is not known. Do not enter a card unless you mean to pay: a linked payment method moves the account off the Free Tier. If it cannot be skipped, stop here and skip this provider.
4. On the **API Keys** page generate a key (the button's label could not be read without signing in).
5. Copy the key at once and paste it into the app; it cannot be viewed again.

Good to know:

- 20 requests a day per model is the whole free allowance.
- The onboarding (Profile, Payment, Credits) was seen on the checked date up to the Profile form; what Payment and Credits ask for was not.
- The Free Tier applies only while no payment method is linked to the account.
- SambaNova: all SambaCloud models are Preview models, "intended for evaluation purposes and developer experimentation only", and "may be removed at short notice".
- A key is shown once; an account can have up to 25 keys.
- How free-tier prompts are used is not stated on the pages read (the terms page linked from older docs is gone).

Keys from several accounts: nothing against it found. Nothing about several accounts or getting round limits was found in the SambaCloud terms of service on the checked date.

Checked 2026-11-23 (docs-only). Sources: <https://docs.sambanova.ai/docs/en/models/rate-limits>, <https://docs.sambanova.ai/docs/en/get-started/api-keys-urls>, <https://docs.sambanova.ai/cloud/docs/get-started/quickstart>, <https://docs.sambanova.ai/cloud/docs/get-started/supported-models>, <https://cloud.sambanova.ai/plans/pricing>, <https://sambanova.ai/cloud-end-user-license-agreement>

<a id="inception"></a>
## 11. Inception Labs (Mercury) (`inception`)

TEMPORARY, until 2026-12-28, and only for existing accounts: Inception is winding the Mercury API down, new sign-ups are closed, and the pool stops using it after that day. Until then, with an account you already have, mercury-3: what is left of the account's 100 million free tokens, at up to 1,000 requests/minute, 1,000,000 input tokens/minute and 100,000 output tokens/minute.

**Temporary: used until 2026-12-28.** After that day the router stops offering its models.

- **Needs:** An Inception Platform account you already have (sign-in by Continue with Google, Continue with GitHub, or email and password). New accounts cannot be made. No payment details.
- **Sign up:** <https://platform.inceptionlabs.ai>
- **Key page:** <https://platform.inceptionlabs.ai/dashboard/api-keys>
- **Goes into:** `INCEPTION_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Nothing to do unless you already have an Inception account: the sign-up page says "New sign-ups for the Inception API are closed. Existing accounts can still sign in."
2. If you have an account, open https://platform.inceptionlabs.ai and log in.
3. Open the API keys page of the dashboard (https://platform.inceptionlabs.ai/dashboard/api-keys) and generate a key (the labels there could not be read).
4. Copy the key and paste it into the app.
5. Optional: under **User Settings**, set **Improve the model for everyone** to OFF so your prompts are not used for training.

Good to know:

- Signing in with a Google account that has no Inception account just returns to the login page; it does not create one.
- The service stops after 2026-12-28: "After this date you will no longer have access to the service or to any data submitted to the platform." Only worth setting up for the weeks that are left.
- Inception's terms: it may use what you submit to "train our models" unless you turn **Improve the model for everyone** off.
- When the free tokens run low the platform asks for payment details under Billing; without them requests stop.
- Whether the free tokens expire before the shutdown is not stated.

Terms: Inception's terms license the service "for your own internal, personal use, and not on behalf of or for the benefit of any third party" and forbid sharing the account.

Keys from several accounts: nothing against it found. Nothing about several accounts was found in Inception's terms on the checked date (an account may not be transferred or shared). The service ends on 2026-12-28 in any case.

Checked 2026-11-23 (seen). Sources: <https://docs.inceptionlabs.ai/get-started/get-started>, <https://docs.inceptionlabs.ai/support/wind-down>, <https://docs.inceptionlabs.ai/get-started/rate-limits>, <https://docs.inceptionlabs.ai/get-started/models>, <https://docs.inceptionlabs.ai/get-started/authentication>, <https://www.inceptionlabs.ai/docs/terms-of-use>, <https://www.inceptionlabs.ai/docs/privacy-policy>

<a id="ovh"></a>
## 12. OVHcloud AI Endpoints (`ovh`)

Without a key: the OVH models at a published 2 requests/minute per IP address and per model (in practice the router measured far less on 2026-11-21: one request per model per year). With a key: 400 requests/minute per project and per model, but paid per token; there is no free allowance with a key.

- **Needs:** Nothing for anonymous use.
- **Sign up:** <https://www.ovhcloud.com/en-gb/public-cloud/ai-endpoints/>
- **Key page:** <https://www.ovhcloud.com/en-gb/public-cloud/>
- **Goes into:** `OVH_AI_ENDPOINTS_ACCESS_TOKEN`
- **Key looks like:** Not documented.

Steps:

1. Nothing to do. The pool calls OVH without a key; there is no free key to get.

Good to know:

- Getting a key is not part of setting the pool up: an OVH key is pay-per-token (it needs a Public Cloud project with a payment method, and every request with it is billed). If you already have one and accept paying, it can be saved and is then used.
- The anonymous allowance is tiny, so OVH adds little to the pool without a key.
- OVH says "we do not store user data" on AI Endpoints.

Keys from several accounts: read the terms first. OVH keys are paid, per Public Cloud project; OVHcloud's terms were not read for this.

Checked 2026-11-23 (seen). Sources: <https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-getting-started>, <https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-capabilities>, <https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-billing>, <https://www.ovhcloud.com/en/public-cloud/ai-endpoints/>

## Removed from the pool

A key saved for one of these is ignored.

| Provider | Removed | Why |
|---|---|---|
| Mistral AI (`mistral`) | 2026-11-23 | No free API tier any more. Mistral's docs: free mode "doesn't include API access. Creating API keys and using the API require billing activation and pay-as-you-go." |
| Cerebras Inference (`cerebras`) | 2026-11-23 | Needs a payment card and has no standing free tier. Cerebras: "Is there a permanently free tier?" "No. The Free Trial is time- and credit-bounded: $5 in credits that expire 30 days after they're granted." |

## Looked at and not added

Other providers checked on 2026-11-23 for a standing free API allowance that needs no card.

| Provider | Free allowance today | Why not | Source |
|---|---|---|---|
| Vercel AI Gateway | A monthly included credit (amount unstated) | A payment method must be added before the free credit can be used. | <https://vercel.com/docs/ai-gateway/faq> |
| Pollinations | Anonymous text: one request at a time, at least 45 seconds apart, per IP; gpt-oss-20b only | Too little to matter, and its terms could not be read. Worth another look if the limits grow. | <https://raw.githubusercontent.com/pollinations/pollinations/master/APIDOCS.md> |
| GitHub Models | None | Retired on 2026-07-30. | <https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models> |
| Hugging Face Inference Providers | None for free users ($2 a month with PRO, which is paid) | Free users must buy credits. | <https://huggingface.co/docs/inference-providers/pricing> |
| Together AI | None | A US$15 credit purchase is required before a key can be made. | <https://docs.together.ai/docs/billing> |
| Fireworks AI | A one-time US$1 credit | One-time; the account is suspended afterwards until a payment method is added. | <https://docs.fireworks.ai/guides/quotas_usage/rate-limits> |
| DeepInfra | None | A card or a prepayment is required. | <https://deepinfra.com/pricing> |
| Chutes | None | "We do not offer a free tier at this time." | <https://chutes.ai/pricing> |
| Nebius Token Factory | None | Setting up billing with a bank card is mandatory. | <https://docs.tokenfactory.nebius.com/other-capabilities/billing-new> |
| Scaleway Generative APIs | Not read | Its pages refuse automated reading (HTTP 403), so nothing could be confirmed. | <https://www.scaleway.com/en/docs/generative-apis/faq/> |
| Hyperbolic | A one-time US$1 credit after phone verification | One-time, and needs a phone number. | <https://hyperbolic.ai/docs/general/account-management> |
| Novita AI | A sign-up voucher (amount unstated) | One-time, size and expiry unstated. | <https://novita.ai/pricing> |
| SiliconFlow | US$1 in credits to start; free models exist but their limits are unstated | One-time credit, and its terms forbid commercial use. | <https://www.siliconflow.com/pricing> |
| Alibaba Cloud Model Studio (Qwen) | Typically 1,000,000 tokens per model, valid 90 days from activation | One-time and expiring, not a standing tier; requests are billed once it is used up unless Free Quota Only is switched on. | <https://www.alibabacloud.com/help/en/model-studio/new-free-quota> |
| Moonshot AI (Kimi) | None | A recharge of at least US$1 is required to start. | <https://platform.kimi.ai/docs/pricing/limits> |
| DeepSeek | None stated | No free tier on its pricing or rate-limit pages. | <https://api-docs.deepseek.com/quick_start/pricing> |
| xAI | None | Credits must be bought before the API can be used. | <https://docs.x.ai/developers/quickstart> |
| LLM7 | None stated | Needs a token from its dashboard and states no free allowance; its terms forbid several accounts. | <https://github.com/chigwell/llm7.io/blob/main/TERMS.md> |
| AI/ML API | None | "The Free Tier is currently paused." | <https://docs.aimlapi.com/faq/free-tier> |

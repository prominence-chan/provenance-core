# Free API keys for the token pool

<!-- Generated from provenance_core/pool_help.yaml by `python -m provenance_core.pool_help`. Edit the YAML. -->

How to get a free key from each provider the pool can use, most capacity for least effort first. Read from the providers' own public pages on 2026-11-23; free tiers and button labels change often, so where a page differs from this, the page is right. Paste each key into the app's field for that provider (or `PUT /pool/keys/{id}` with `{"values": {"<VARIABLE>": "<key>"}}`). The same data is served as JSON by `GET /pool/help`.

Confidence: **seen** = the provider's current docs name the controls; **docs-only** = the docs say where the key is made but the page is behind a sign-in, so its labels are not given; **uncertain** = the only walkthrough is dated or the docs disagree.

| # | Provider | Free | Needs | Minutes | Confidence |
|---|---|---|---|---|---|
| 1 | [Kilo AI Gateway](#kilo) | works without a key | Nothing. No account, no key. | 0 | seen |
| 2 | [Google Gemini API (Google AI Studio)](#google) | free tier | A Google account, age 18 or over, a supported country. Some accounts are asked for a one-time phone check (scan a QR code; no phone number is given). No card. | 3 | seen |
| 3 | [Groq (GroqCloud)](#groq) | free tier | A Groq account (the sign-in options could not be read without signing in). No card for the Free plan. | 3 | docs-only |
| 4 | [OpenRouter](#openrouter) | free tier | An OpenRouter account, age 18 or over (the sign-in options could not be read without signing in). No card for the 50/day allowance; a one-time US$10 purchase for 1,000/day. | 4 | docs-only |
| 5 | [Cloudflare Workers AI](#cloudflare) | free tier | A Cloudflare account. No card on the Workers Free plan (the sign-up page itself could not be read). | 5 | seen |
| 6 | [Inception Labs (Mercury)](#inception) | one-time trial | An Inception Platform account (the sign-in options could not be read). No payment details. | 3 | docs-only |
| 7 | [Z.ai (Zhipu) API](#zai) | free tier | A Z.ai account (email and password, or Google or GitHub sign-in). No card stated for the free model. | 3 | docs-only |
| 8 | [NVIDIA API Catalog (build.nvidia.com)](#nvidia) | free tier | An email address for an NVIDIA account (signing up joins the free NVIDIA Developer Program). Some accounts are reported to need a further verification before a key works. No card stated. | 5 | uncertain |
| 9 | [SambaNova (SambaCloud)](#sambanova) | free tier | A SambaCloud account (the sign-in options could not be read). No card; linking one moves the account to the Developer Tier (60 requests/minute, 12,000 requests/day, pay per token). | 3 | docs-only |
| 10 | [Cohere](#cohere) | free tier | A Cohere account (email and password, or Google or GitHub sign-in). No card. Not available from Belarus, China (with Hong Kong and Macau), Iran, North Korea, Russia or Syria. | 3 | docs-only |
| 11 | [Cerebras Inference](#cerebras) | one-time trial | A Cerebras Cloud account and a verified payment method (adding it is not charged, but the credit is only given after it). The sign-in options could not be read. | 6 | seen |
| 12 | [OVHcloud AI Endpoints](#ovh) | works without a key | Nothing for anonymous use. For a key, an OVHcloud customer account and a Public Cloud project with a payment method (a project in Discovery mode, without one, cannot use the service). | 10 | seen |
| 13 | [Mistral AI (Studio)](#mistral) | no free tier | A Mistral account with a payment method (billing activation, pay-as-you-go). The sign-in options could not be read. | 8 | seen |

<a id="kilo"></a>
## 1. Kilo AI Gateway (`kilo`)

The gateway's free models (the `:free` Nemotron, ERNIE, Flint, Particle, Ling models and others) with no account and no key: 200 requests per hour per IP address, shared by all free models.

- **Needs:** Nothing. No account, no key.
- **Sign up:** <https://app.kilo.ai>
- **Key page:** <https://app.kilo.ai>
- **Key looks like:** Not needed. A Kilo account key is a JWT (three dot-separated parts, starting `eyJ`).

Steps:

1. Nothing to do. The pool calls Kilo's free models without a key and they work as soon as the router runs.
2. Optional, and not used by the pool: a Kilo account has a key. Go to **app.kilo.ai**, open **Your Profile** on your personal account (not an organization), scroll to the bottom of the page and copy your API key.

Good to know:

- The 200 requests/hour limit is per IP address and is the same signed in or not, so a key adds nothing for free models.
- Kilo's docs on the NVIDIA free endpoints: "Trial use only - do not submit personal or confidential data. Your use is logged for security purposes and to improve NVIDIA products and services."
- Other free models may be routed to providers that log prompts and outputs and use them to improve their services.
- Which models are free changes; the live list is https://ai-gateway.kilo.ai/api/v1/models (all the catalog's Kilo models were on it on the checked date).

Terms: Kilo's terms allow one account per person or entity; if you route other people's requests through the gateway you are responsible for their compliance with the terms.

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
- If you already have a Google Cloud account, AI Studio creates no default project; import one under **Dashboard** > **Projects** > **Import projects**, then create the key on the **API Keys** page. An imported project may need the phone verification before it gets the free tier.
- Do not attach a Cloud Billing account to the project unless you mean to pay; with billing on, the project is on the paid tier.
- Flash models often answer 503 under load; the router treats that as temporary.

Terms: Google's terms say the API is "for developers building with Google AI models for professional or business purposes, not for consumer use", require age 18+, and allow only paid services when you make a client available to users in the EEA, Switzerland or the UK.

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

Checked 2026-11-23 (docs-only). Sources: <https://console.groq.com/docs/quickstart>, <https://console.groq.com/docs/rate-limits>, <https://console.groq.com/docs/models>, <https://console.groq.com/docs/billing-faqs>, <https://console.groq.com/docs/projects>, <https://console.groq.com/docs/your-data>, <https://console.groq.com/docs/production-readiness/security-onboarding>, <https://console.groq.com/docs/legal/services-agreement>, <https://console.groq.com/keys>

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
- Many free models are served by providers that log or train on prompts; OpenRouter only routes to them when you switch that on in the privacy settings ("There are separate settings for paid and free models").
- A negative credit balance causes errors on free models too.
- A key made for you during onboarding has a US$100 credit limit and expires after 180 days; a key you create yourself has the limit you give it.
- OpenRouter's terms say credits must be bought to call the API, while its docs describe the free allowance above; the docs are what the limits follow today.

Terms: OpenRouter's terms forbid creating several accounts to get round usage limits and reselling API access.

Checked 2026-11-23 (docs-only). Sources: <https://openrouter.ai/docs/api-reference/limits>, <https://openrouter.ai/docs/api-reference/authentication>, <https://openrouter.ai/docs/faq>, <https://openrouter.ai/docs/guides/privacy/provider-logging>, <https://openrouter.ai/docs/guides/privacy/data-collection>, <https://openrouter.ai/docs/guides/routing/model-variants/free>, <https://openrouter.ai/terms>, <https://openrouter.ai/api/v1/models>

<a id="cloudflare"></a>
## 5. Cloudflare Workers AI (`cloudflare`)

@cf/openai/gpt-oss-120b, @cf/qwen/qwen3.8-27b, @cf/nvidia/nemotron-3-120b-a12b and @cf/zai-org/glm-4.7-flash: 10,000 Neurons per day for the whole account across all models (resets at 00:00 UTC), 300 requests/minute.

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
- Some newer models need a paid billing method; the four in the catalog were not on that list on the checked date.

Checked 2026-11-23 (seen). Sources: <https://developers.cloudflare.com/workers-ai/get-started/rest-api/>, <https://developers.cloudflare.com/workers-ai/platform/pricing/>, <https://developers.cloudflare.com/workers-ai/platform/limits/>, <https://developers.cloudflare.com/workers-ai/platform/data-usage/>, <https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/>, <https://developers.cloudflare.com/fundamentals/api/get-started/create-token/>, <https://developers.cloudflare.com/fundamentals/api/get-started/token-formats/>, <https://developers.cloudflare.com/fundamentals/account/find-account-and-zone-ids/>

<a id="inception"></a>
## 6. Inception Labs (Mercury) (`inception`)

mercury-3: 100 million free tokens for a new account, at up to 1,000 requests/minute, 1,000,000 input tokens/minute and 100,000 output tokens/minute. ENDS 2026-12-28: Inception is winding the Mercury API down.

- **Needs:** An Inception Platform account (the sign-in options could not be read). No payment details.
- **Sign up:** <https://platform.inceptionlabs.ai>
- **Key page:** <https://platform.inceptionlabs.ai/dashboard/api-keys>
- **Goes into:** `INCEPTION_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Open https://platform.inceptionlabs.ai and sign up or log in.
2. Open the API keys page of the dashboard (https://platform.inceptionlabs.ai/dashboard/api-keys) and generate a key (the labels there could not be read).
3. Copy the key and paste it into the app.
4. Optional: under **User Settings**, set **Improve the model for everyone** to OFF so your prompts are not used for training.

Good to know:

- The service stops after 2026-12-28: "After this date you will no longer have access to the service or to any data submitted to the platform." Only worth setting up for the weeks that are left.
- Inception's terms: it may use what you submit to "train our models" unless you turn **Improve the model for everyone** off.
- When the free tokens run low the platform asks for payment details under Billing; without them requests stop.
- Whether the free tokens expire before the shutdown is not stated.

Terms: Inception's terms license the service "for your own internal, personal use, and not on behalf of or for the benefit of any third party" and forbid sharing the account.

Checked 2026-11-23 (docs-only). Sources: <https://docs.inceptionlabs.ai/get-started/get-started>, <https://docs.inceptionlabs.ai/support/wind-down>, <https://docs.inceptionlabs.ai/get-started/rate-limits>, <https://docs.inceptionlabs.ai/get-started/models>, <https://docs.inceptionlabs.ai/get-started/authentication>, <https://www.inceptionlabs.ai/docs/terms-of-use>, <https://www.inceptionlabs.ai/docs/privacy-policy>

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

Checked 2026-11-23 (docs-only). Sources: <https://docs.z.ai/guides/overview/quick-start>, <https://docs.z.ai/guides/overview/pricing>, <https://docs.z.ai/api-reference/introduction>, <https://docs.z.ai/devpack/notice/api-key-management>, <https://docs.z.ai/help/faq>, <https://docs.z.ai/legal-agreement/terms-of-use>, <https://docs.z.ai/legal-agreement/privacy-policy>

<a id="nvidia"></a>
## 8. NVIDIA API Catalog (build.nvidia.com) (`nvidia`)

meta/llama-3.3-70b-instruct and the DeepSeek models on NVIDIA's hosted endpoints, free "for prototyping" for NVIDIA Developer Program members. The rate limit is unstated: NVIDIA says it varies per model and is shown under your account on build.nvidia.com.

- **Needs:** An email address for an NVIDIA account (signing up joins the free NVIDIA Developer Program). Some accounts are reported to need a further verification before a key works. No card stated.
- **Sign up:** <https://build.nvidia.com>
- **Key page:** <https://build.nvidia.com>
- **Goes into:** `NVIDIA_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Open https://build.nvidia.com and open the page of any chat model, for example Llama 3.3 70B Instruct.
2. In the right-hand pane of the model page click **Get API Key**.
3. Enter your email in the pop-up and click **Next**; sign in, or set a password to create the NVIDIA account, and follow the on-screen instructions.
4. Back on the model page a pop-up shows the new key. Click **Copy Key** and paste it into the app.

Good to know:

- NVIDIA's only written walkthrough is old (its screenshots show an earlier model), so the labels above may have moved; look for **Get API Key** on a model page or under your account at the top right.
- Access is "for prototyping, research, development and testing purposes only", not production, which NVIDIA defines as "any non-testing activity including activity serving real end-users".
- Whether the key expires and whether it is shown only once are not stated; save it when it appears.
- NVIDIA's own pages do not say how free-tier prompts are used; through Kilo, NVIDIA's free endpoints are described as logged "to improve NVIDIA products and services".

Terms: Free access through the Developer Program is for prototyping and testing only; production use needs an NVIDIA AI Enterprise license.

Checked 2026-11-23 (uncertain). Sources: <https://docs.api.nvidia.com/nim/docs/api-quickstart>, <https://docs.api.nvidia.com/nim/docs/product>, <https://docs.api.nvidia.com/nim/reference/llm-apis>, <https://build.nvidia.com/>, <https://forums.developer.nvidia.com/t/nvidia-nim-faq/300317>

<a id="sambanova"></a>
## 9. SambaNova (SambaCloud) (`sambanova`)

gpt-oss-120b and gemma-4-31B-it on the Free Tier: each 20 requests/minute, 20 requests/day and 200,000 tokens/day. Small.

- **Needs:** A SambaCloud account (the sign-in options could not be read). No card; linking one moves the account to the Developer Tier (60 requests/minute, 12,000 requests/day, pay per token).
- **Sign up:** <https://cloud.sambanova.ai>
- **Key page:** <https://cloud.sambanova.ai/apis>
- **Goes into:** `SAMBANOVA_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Open https://cloud.sambanova.ai/apis and sign up or log in (**Login/Signup**).
2. On the **API Keys** page generate a key (the button's label could not be read without signing in).
3. Copy the key at once and paste it into the app; it cannot be viewed again.

Good to know:

- 20 requests a day per model is the whole free allowance.
- The Free Tier applies only while no payment method is linked to the account.
- SambaNova: all SambaCloud models are Preview models, "intended for evaluation purposes and developer experimentation only", and "may be removed at short notice".
- A key is shown once; an account can have up to 25 keys.
- How free-tier prompts are used is not stated on the pages read (the terms page linked from older docs is gone).

Checked 2026-11-23 (docs-only). Sources: <https://docs.sambanova.ai/docs/en/models/rate-limits>, <https://docs.sambanova.ai/docs/en/get-started/api-keys-urls>, <https://docs.sambanova.ai/cloud/docs/get-started/quickstart>, <https://docs.sambanova.ai/cloud/docs/get-started/supported-models>, <https://cloud.sambanova.ai/plans/pricing>

<a id="cohere"></a>
## 10. Cohere (`cohere`)

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

Checked 2026-11-23 (docs-only). Sources: <https://docs.cohere.com/docs/rate-limits>, <https://docs.cohere.com/docs/going-live>, <https://docs.cohere.com/docs/compatibility-api>, <https://docs.cohere.com/docs/models>, <https://cohere.com/pricing>, <https://cohere.com/data-usage-policy>, <https://cohere.com/saas-agreement>, <https://dashboard.cohere.com/welcome/register>

<a id="cerebras"></a>
## 11. Cerebras Inference (`cerebras`)

gpt-oss-120b on a Free Trial: US$5 of credit that expires 30 days after it is granted, at 5 requests/minute and 1,000,000 tokens/day for that model. There is no standing free tier.

- **Needs:** A Cerebras Cloud account and a verified payment method (adding it is not charged, but the credit is only given after it). The sign-in options could not be read.
- **Sign up:** <https://cloud.cerebras.ai>
- **Key page:** <https://cloud.cerebras.ai>
- **Goes into:** `CEREBRAS_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Open https://cloud.cerebras.ai and sign up or log in.
2. Add a payment method in the console's billing settings to receive the US$5 trial credit.
3. Go to **API Keys** in the left nav.
4. Click **Generate API Key**, give it a name such as `provenance`, and confirm.
5. The key appears in the list; click the copy icon next to it and paste the key into the app.

Good to know:

- A card (or other payment method) is required: "New accounts receive $5 in free credits after adding a verified payment method."
- "Is there a permanently free tier?" "No." API access pauses when the credit is used up or 30 days have passed, unless you buy credit.
- Keys do not expire, and the full key can be copied again later from the list.
- Cerebras's terms say it has no right to use your content "for the purpose of training or fine-tuning models".
- Trial credits "may be withheld or revoked in cases of fraud, abuse, or duplicate accounts".

Terms: Cerebras's terms forbid buying, selling or transferring API keys without its written consent.

Checked 2026-11-23 (seen). Sources: <https://inference-docs.cerebras.ai/console/api-keys>, <https://inference-docs.cerebras.ai/console/account-billing>, <https://inference-docs.cerebras.ai/support/rate-limits>, <https://inference-docs.cerebras.ai/models/overview>, <https://inference-docs.cerebras.ai/quickstart>, <https://www.cerebras.ai/pricing>, <https://www.cerebras.ai/terms-of-service>

<a id="ovh"></a>
## 12. OVHcloud AI Endpoints (`ovh`)

Without a key: the OVH models at a published 2 requests/minute per IP address and per model (in practice the router measured far less on 2026-11-21: one request per model per year). With a key: 400 requests/minute per project and per model, but paid per token; there is no free allowance with a key.

- **Needs:** Nothing for anonymous use. For a key, an OVHcloud customer account and a Public Cloud project with a payment method (a project in Discovery mode, without one, cannot use the service).
- **Sign up:** <https://www.ovhcloud.com/en-gb/public-cloud/ai-endpoints/>
- **Key page:** <https://www.ovhcloud.com/en-gb/public-cloud/>
- **Goes into:** `OVH_AI_ENDPOINTS_ACCESS_TOKEN`
- **Key looks like:** Not documented.

Steps:

1. Nothing to do for the free, anonymous use; the pool calls OVH without a key.
2. Only if you accept paying per token: log in to the OVHcloud Control Panel and go to **Public Cloud** > select your project (it must have a payment method).
3. In the left-hand menu click **AI Endpoints** under **AI**.
4. Click **Generate my first access key**; if you already have keys, open the **API keys** tab and click **Create an API key**.
5. Fill in **Name** (for example `provenance`); **Description** and **Expiration (optional)** can stay empty. Click **Create**.
6. Copy the key before clicking **Close** (it is displayed only once) and paste it into the app.

Good to know:

- A key is not free; every request with it is billed by input and output tokens to the Public Cloud project.
- "Access keys created from Public Cloud projects in Discovery mode (without a payment method) cannot use the service."
- The anonymous allowance is tiny, so OVH adds little to the pool without a key.
- OVH says "we do not store user data" on AI Endpoints.
- If you set an expiration, the key stops working on that date.

Checked 2026-11-23 (seen). Sources: <https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-getting-started>, <https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-capabilities>, <https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-billing>, <https://www.ovhcloud.com/en/public-cloud/ai-endpoints/>

<a id="mistral"></a>
## 13. Mistral AI (Studio) (`mistral`)

Nothing for free any more. Mistral's docs today say the free mode "doesn't include API access"; a key for mistral-large-latest and mistral-small-latest needs billing switched on and is paid per token.

- **Needs:** A Mistral account with a payment method (billing activation, pay-as-you-go). The sign-in options could not be read.
- **Sign up:** <https://console.mistral.ai>
- **Key page:** <https://console.mistral.ai/api-keys>
- **Goes into:** `MISTRAL_API_KEY`
- **Key looks like:** Not documented.

Steps:

1. Skip this provider unless you accept paying per token.
2. Sign in at https://console.mistral.ai and set up billing (add a payment method) so the account is on a plan with API access.
3. Open the Studio console and go to **API Keys** (https://console.mistral.ai/api-keys).
4. Select **New key**.
5. Enter a **Name** such as `provenance`, set an **Expiration** date, and for **Connector access scope** choose **Shared connectors only**.
6. Create the key, then copy it from the confirmation dialog (the full key appears only once) and paste it into the app.

Good to know:

- No free API tier: "Free mode includes a monthly usage budget for trying Vibe and Studio. It doesn't include API access. Creating API keys and using the API require billing activation and pay-as-you-go."
- The key stops working on the expiration date you set.
- "The full key appears only once. You can't retrieve it after you close the confirmation dialog."
- The earlier free "Experiment" plan is not mentioned anywhere in the current docs or pricing page.

Checked 2026-11-23 (seen). Sources: <https://docs.mistral.ai/admin/billing-usage/usage-limits>, <https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key>, <https://mistral.ai/pricing>

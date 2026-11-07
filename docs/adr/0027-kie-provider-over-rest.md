# ADR-0027: Kie.ai as a whole-ad provider, over REST, priced from published credits

- Status: Accepted (2026-11-07)

## Context
The Higgsfield balance is $0.50 and one 8 s Seedance 2.0 generation there costs ~$2.4, so no
video made by the system alone could exist for the 2026-11-08 meeting. Kie.ai serves the same
ByteDance Seedance 2.0 reference-to-video model, publishes a per-second price, and gives a new
account 80 free credits. The user decided: add Kie next to Higgsfield (fal stays the scene-path
provider), one account only, budget at the higher published rate and settle at what Kie reports,
and never resubmit a paid task automatically.

Facts checked on docs.kie.ai and kie.ai on 2026-11-07 (no call was made):
- `POST https://api.kie.ai/api/v1/jobs/createTask` `{model: "bytedance/seedance-2", input: {...}}`
  → `data.taskId`. `input`: `prompt` (3–20,000 chars), `reference_image_urls` (≤ 9), `generate_audio`,
  `resolution` (480p|720p|1080p|4k), `aspect_ratio` (incl. 9:16), `duration` (4–15 or −1). No seed,
  no watermark field, **no idempotency key**, no published rate limits.
- **HTTP 200 on errors**: the body `code` carries 401/402/408/422/429/455/500/501/505.
- `GET /api/v1/jobs/recordInfo?taskId=` → `state` waiting|queuing|generating|success|fail;
  `resultJson` is a JSON string `{"resultUrls": [...]}`; `creditsConsumed` = credits deducted (0
  when failed; "failed tasks are never charged"); files kept 14 days.
- `GET /api/v1/chat/credit` → remaining credits (free).
- Upload on another host: `POST https://kieai.redpandaai.co/api/file-stream-upload` (multipart
  `file`, `uploadPath`) → `data.downloadUrl`; uploaded files are deleted after 24 h; free.
- Price (kie.ai/seedance-2, no video input): 480p 11.7 credits/s, 720p 24.8 credits/s; 1 credit
  ≈ $0.005. The model page's header names `seedance-2-fast` and kie.ai/pricing says seedance-2
  "from 11.5/s": ambiguous, so we budget at the higher figures. No 1080p/4k price was found.

## Decision
1. **REST with httpx** (`providers/media/kie.py`): `KieQueue` makes one request per step, checks
   every body `code`, maps it with `classify_kie` (402 → account pause, 429 → rate limited,
   408/455/500 → transient, 401/422/501 → permanent, moderation words → content policy). Retries
   live only in `agent/recovery`. A rate limit or a transient error while polling keeps the task
   handle, so the retry polls the same task (a test found the first draft dropped it on 429).
2. **Price from the catalog.** `[kie.video.endpoints.kie_seedance_20.credit_pricing]` holds the
   rates; `credit_quote` prices a job locally (no estimate call exists). Only priced resolutions
   (480p, 720p) are offered; the body is validated before any HTTP.
3. **Settle at `creditsConsumed`** × $0.005 (the price check logs computed vs consumed credits);
   when Kie reports none, at the computed price.
4. **No double spend without an idempotency key.** The task id is persisted at submit
   (`on_submitted`) and a fresh process polls it. A connect error (never sent) is released and
   transient. Any other transport error after sending, or a 200 without a task id, is an
   **ambiguous submit**: the reservation stays in the wallet, the queue pauses, the run pauses
   (`AmbiguousSubmitError` is an account-level refusal), and on resume the generation runner fails
   that version without submitting (the user's rule: never resubmit automatically). The human checks
   the console with `adsmith kie-balance`, then `regenerate-ad` or `settle-actual`.
5. **Its own wallet**: ledger rows `provider = "kie"`, ceiling `ADSMITH_KIE_BALANCE_USD` (USD).
   `settle-actual` books on the run's own provider wallet.
6. **Selection**: `ADSMITH_AD_PROVIDER=kie` (+ `ADSMITH_KIE_VIDEO_MODEL`, `ADSMITH_KIE_RESOLUTION`).
   Catalog keys are unique across providers (checked at load), so `ad_endpoint(settings, key,
   provider)` finds either; a mock run plans with the limits of the provider it stands in for.
7. **The key** (`KIE_API_KEY`, `SecretStr`) goes only in the `Authorization: Bearer` header of
   calls to the two Kie hosts, never to the result download or into any message.

## Alternatives considered
- Resubmit once after an ambiguous submit (as Higgsfield does with its idempotency key): without a
  key it can pay twice; the user chose pause.
- Callbacks (`callBackUrl`) instead of polling: needs a public endpoint; polling is documented.
- Seedance 2.0 Fast on Kie: cheaper, but its price line is the ambiguous one; not added now.

## Consequences
+ A 6 s 480p ad costs 70.2 credits (≈ $0.35): one generation fits the 80 free credits.
+ The wallet matches Kie's console without a manual step.
− An ambiguous submit leaves a held reservation until a human resolves it (fail-safe).
− Kie's rate limits are unpublished: a 429 is retried by the recovery layer's backoff.

## Addendum (2026-11-07): the standard price was refused; Seedance 2.0 Fast

- The first real submit (`run_01M6TBP6H8VTXWGXFJR6XPVQ7M`, 6 s 480p on `bytedance/seedance-2`)
  was refused with "402 Credits insufficient" on an 80-credit account although our computed price
  was 70.2 credits; nothing was charged. The 11.7 / 24.8 credits/s card sits under a page header
  "seedance-2-fast"; kie.ai/pricing lists the standard model "from 11.5/s" (its cheapest option)
  and Fast "from 6.8/s" (Fast's with-video 480p rate on that card). So the card is Fast's.
- `kie_seedance_20_fast` (`bytedance/seedance-2-fast`, 480p/720p, 11.7/24.8 credits/s, inferred
  and to be verified from `creditsConsumed`) is now the Kie default. The standard entry carries
  `unverified_price`: the gate refuses to quote it.
- `adsmith set-ad-model <run> <key>` moves an unpaid generation (no request id, no video) to
  another endpoint: same prompt, a new version, estimated again, waiting at the gate.

## Addendum (2026-11-07): a network error after the submit keeps the task

- Observed on the first Fast generation: Kie accepted task `97b589d0e1df103ad60002cc5cec8b60`,
  then every status poll failed (ConnectTimeout, ConnectError) until the recovery layer's tries
  ran out, and the runner marked the generation failed although the task finished (success,
  creditsConsumed 70.2). The wallet reservation was kept (fail-safe), nothing was resubmitted.
- Now a transient or rate-limit error after the request id was persisted marks the attempt
  "interrupted", releases only the run-budget reservation, keeps the approval and the wallet
  reservation, and stops the run resumable: `adsmith resume` polls the same task, settles at
  `creditsConsumed` and continues. A transient error before any submit still fails the attempt.
- `resume` also reopens a generation that was failed this way before the fix (last attempt with
  a request id, transient kind, no video), so the task is polled, never paid twice.

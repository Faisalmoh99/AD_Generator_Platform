# ADR-0022: Higgsfield as a second media provider, over REST, estimate first

- Status: Accepted (2026-11-06)

## Context
Higgsfield-made ads differ from ours in three ways: one Seedance reference-to-video generation
holds several shots with native audio, it starts from a real product photo, and the user's
creative layer sits on top. The decision to add Higgsfield next to fal was taken by the user.
What remained open was how to call it without breaking the brain's rules: the recovery layer owns
every retry (rule 6), a paid job is never submitted twice, every paid call is reserved against a
prepaid ceiling, and a crashed process resumes a job by polling it.

Facts checked on docs.higgsfield.ai and the SDK source on 2026-11-06 (no call was made):
- `POST /bytedance/seedance-2.0/reference-to-video` takes `prompt`, `image_urls` (1–9 public URLs),
  `duration` (4–15), `resolution`, `aspect_ratio`, `generate_audio`; 2.5 allows 4–30 s and 30
  images, no 4k. No seed, no negative prompt. No published price: `POST /estimate/<endpoint>`
  with the same body answers `{"usd": "…"}`; the docs do not say it is free.
- Submit → `request_id`, `status_url`, `cancel_url`; statuses `queued | in_progress | completed |
  failed | nsfw | canceled`; `failed` and `nsfw` are not charged; outputs are deleted after 7 days;
  `Idempotency-Key` is supported on submit.
- Inputs must be public URLs. Higgsfield has an upload endpoint (`/files/generate-upload-url`
  → presigned PUT → `public_url`, temporary retention, URL valid 1 h).
- The official SDK (`higgsfield-client` 0.2.0) retries every request, including the POST submit,
  3 times on 408/429/5xx with no idempotency key, and has no public switch to turn that off.

## Decision
1. **REST with httpx, not the SDK** (`providers/media/higgsfield.py`). One HTTP request per step,
   errors classified by `classify_higgsfield_http`, retries only in `agent/recovery`.
2. **Estimate first.** A `ReferenceVideoJob` carries the estimate a human approved; the provider
   refuses to submit without one, reserves exactly that amount, and refuses an estimate above the
   catalog's `max_usd_per_second` sanity cap.
3. **No double spend.** `Idempotency-Key` = sha256(endpoint, body, variant). A transient error
   after the submit keeps the request in memory; a retry polls it. The request id is reported
   through `RemoteControl.on_submitted` before polling, so a fresh process resumes it. An
   ambiguous submit timeout keeps the reservation, and the retry reuses both the key and that
   reservation. A resumed request that answers 404 is resubmitted once (same key); a resumed
   request that `failed` is not.
4. **Its own wallet.** The same ledger file as fal, rows under `provider = "higgsfield"`, ceiling
   `ADSMITH_HF_BALANCE_USD`. A run's media cap is the sum of the wallets it draws on.
5. **Separate setting.** `ADSMITH_AD_PROVIDER = mock | higgsfield` selects who renders a whole-ad
   generation; `ADSMITH_MEDIA_PROVIDER` keeps serving the per-scene keyframe/clip path. The two
   paths never share a provider object, so fal and mock runs are untouched.
6. **The product photo** is uploaded through Higgsfield's own upload endpoint right before a job
   (cached by content hash for 50 minutes). The run stores the photo as a CAS artifact, never the
   URL. The key goes only into the `Authorization` header of calls to the API host: never to the
   presigned PUT, the CDN download, a message, a handle or an event.

## Alternatives considered
- The SDK with its private transport replaced by `NoRetry`: unsupported, and still no idempotency
  key or status codes on its exceptions.
- fal storage or an S3/R2 bucket for the public photo URL: a second provider or new credentials
  for something Higgsfield already offers.
- Settling at an assumed per-second price: there is none published; the estimate is the price.

## Consequences
+ The no-double-spend, wallet and crash-resume guarantees carry over from fal unchanged.
+ Zero-key tests cover every path (`tests/unit/test_higgsfield.py`, httpx `MockTransport`).
− The credit-refusal body of a generation endpoint is undocumented (402 assumed, plus markers);
  verified at the first real call. The prompt length limit is unknown until probed.
− We settle at the estimate; if Higgsfield bills less, the ledger overstates spend (fail-safe).

## Addendum (2026-11-06): /estimate returns a pricing description, not a number

Observed on the first approved call: for Seedance 2.0, `POST /estimate/...` returned
`{"type": "description", "pricing_description": "Token-metered pricing. Billable video tokens =
ceil((input + generated video seconds) × width × height × 24 fps / 1024) ... $0.014 per 1,000
tokens (480p/720p/1080p), $0.008 (4K) ... before any applicable customer discount"}`. The call was
free (console balance unchanged at $5).

- The formula lives in `config/models.toml` (`[...seedance_20_ref.pricing]`: rates, fps, divisor,
  the ASSUMED short side per resolution, `margin = 0.10`). The provider prices a description with
  it (`PriceQuote`: computed price, reserved = price × 1.10, basis text) and refuses if the live
  description stops naming the configured rates, `24 fps` or `/ 1024` (config drift).
- A numeric `{"usd": ...}` answer still works and is used as is (no margin: nothing assumed).
- The gate shows both numbers; the wallet reserves the margin and settles at the computed price.
- After a generation, a decision event logs the computed price next to the ffprobe size and
  duration, and the price at the real size if it differs, to check the frame assumption against
  the console by hand.
- Seedance 2.5 has no observed pricing: a description for it is refused.

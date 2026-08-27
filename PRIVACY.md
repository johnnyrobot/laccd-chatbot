# Privacy and Data-Flow Notes

This document describes the behavior available in this source code. A deployer
chooses which providers to enable and is responsible for its own user notice,
retention settings, access controls, contracts, and legal review.

## User input

The web application does not require an account and is not designed to access
student records. Chat text and recent conversation context may nevertheless
contain information a user chooses to enter. Users should not submit student
IDs, Social Security numbers, passwords, medical information, financial
information, or other sensitive data.

## Configurable external processing

Depending on deployment configuration and the feature used:

- Google Gemini may receive chat prompts and retrieved context to generate
  answers.
- Gemini Live and LiveKit process live voice-session audio and transport.
- OpenRouter may receive chat prompts only when the configured fallback is
  enabled and the primary model path has a qualifying transient failure.
- Langfuse may receive masked traces when tracing credentials are configured.
  The application masks common email, phone, and long numeric identifier
  patterns before export, but masking cannot guarantee removal of every kind of
  volunteered personal information.
- Live schedule, articulation, catalog, and program services receive the query
  parameters needed for their specific lookup.

Firecrawl and Crawl4AI are used for content ingestion from approved public
college sites; they are not intended to receive student chat messages.

## Application storage and logs

The backend does not intentionally maintain a first-party transcript database.
It uses an in-memory client key for rate limiting and writes a keyed,
pseudonymous client tag in content-free operational events. Some diagnostic
paths may log PII-masked query text. Actual retention depends on the deployer's
container logs, observability services, provider settings, and infrastructure.

The Android application can run model inference on the device. Public builds do
not download model weights or collect model-provider credentials; model files
must be imported or sideloaded by the user. Cloud-backed tools send only the
request needed for the selected tool to the configured backend. Android backup
is disabled for the application. Local conversations, model files, profile
fields, and planner data remain in app storage unless the user explicitly
exports data. Android speech recognition is used as a dictation fallback and
may process audio off-device depending on the device's speech-service settings.

## Deployment responsibility

Anyone operating this software should disclose the providers they enable,
configure appropriate retention periods, restrict log access, protect runtime
tokens, and provide an alternative channel for consequential student questions.

# Depth 5: model-assisted fine-grained review

Accepted product direction, 28 September 2026. Requested by the project owner while reviewing the engineering presentation.

## Definition

**The next Depth 5 stage is LLM fine-grained review of the original PDF against the candidate extraction.** It is not "a fifth eligible local reader" and is not a guarantee of exact correctness.

Depths 1–4 allocate up to the requested number of eligible additional local observations. Known upstream readers are excluded; a scan starts with OCR. Poppler, Tesseract, pdfplumber, PDFium and pypdf remain the local toolkit. At Depth 5, retain up to four usable local observations and add a separate model review. A missing or failed local observation stays missing; the model does not make the local count equal five.

## Evidence contract

For a selected region provide the original page image/crop plus enough neighbouring context, its physical page number and PDF-coordinate bounding box, the original candidate block/cell with stable IDs, the local observations and located discrepancies. Include source hash, input hash and version bindings. Compare numbers, units, negation, table-cell ownership and omissions against the image, not just two extracted text strings. Region selection and sampling coverage must be explicit; sampled review cannot claim full-document verification.

Return one structured finding per checked claim: page and region, upstream IDs, observed source wording/value, candidate wording/value, discrepancy kind, concise evidence-grounded explanation and a disposition (`supported`, `conflict`, `uncertain`). A source citation is required; an invalid location, malformed response or timeout is not agreement. Keep raw response, provider/model identifier, prompt version, latency and cost metadata when available. Model confidence is not calibrated correctness.

Record model review status separately from local `effective_depth`: `not_configured`, `pending`, `completed`, `failed` or `unavailable`. Never mix the model's judgment into the existing local score without a separately specified and tested aggregation policy. `calibrated_correctness` stays null without independent labels. Conflicts and uncertainty enter the human review queue; no automatic repair of either input.

## Implementation status

**Design accepted; model adapter not implemented or connected.** The current v0.1 CLI still uses the legacy `--depth 1..5` local-reader count in `pdf_validator/evidence.py`. Its historical reports remain valid under that legacy definition. No existing timing record is an LLM measurement. This document is the canonical next-version design, not a runtime configuration switch.

No model/provider has been selected and no documents have been sent externally. The presentation remains completely offline; it illustrates this planned stage. The eventual validator adapter can use a separately configured local or remote multimodal model.

## Next implementation and acceptance

1. Version the depth policy and report schema so new Depth 5 cannot silently reinterpret legacy reports.
2. Add an explicit provider configuration and region-review adapter with missing-configuration and failure paths.
3. Validate location bindings and structured responses; retain the input evidence and response audit trail.
4. Use synthetic injected number/negation/cell-location errors, irrelevant model answers, invalid citations, timeouts and no-provider cases to test the contract.
5. Independently label representative real cases before claiming accuracy, cost-effectiveness or an optimal depth. Record native Windows acceptance separately.

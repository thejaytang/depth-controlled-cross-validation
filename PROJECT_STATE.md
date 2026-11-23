# Current state

Updated 28 September 2026.

The local v0.1 prototype is unchanged. Current code still interprets Depth 1–5 as an additional-local-reader budget. This turn did not run the validator or add a model adapter.

The owner has set **Depth 5 = LLM fine-grained review** for the next version. The canonical definition, evidence contract, failure states and implementation acceptance are in [the Depth 5 design](project-support/depth-5-model-review.md). It is a design requirement, not an already-connected model or runtime setting. Depths 1–4 retain local checks; the five existing local tools remain available.

Next: version the depth policy/report schema, select and configure a provider, implement the adapter and test source-bound judgments. Historical local timings must not be relabelled as model timings.

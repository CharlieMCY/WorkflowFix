"""Baselines for RQ7 — three alternatives to WORKFLOWBP.

  copy_paste            verbatim git-apply of the source diff onto the
                        target file
  dependabot_style      extract only `uses:` upgrades from the source
                        diff and apply each, mirroring a single-dependency
                        updater
  llm                   prompt Claude/GPT with (source_before, source_after,
                        target_before); parse and verify the LLM's output

Each baseline exposes `apply(...)` that returns a `(patched_text,
diagnostics)` tuple in the same shape so rq7_comparison can iterate over
them uniformly.
"""

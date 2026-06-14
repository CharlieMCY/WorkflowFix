"""Evaluation harnesses for §V (RQ5--RQ7).

  rq5_capability       on the 4,776 unpatched (fix, branch) pairs, count
                       scanner-verified successes for WORKFLOWBP.
  rq6_reproducibility  on the 242 confirmed historical backports, score
                       WORKFLOWBP's output against the maintainer-written
                       backport (byte / AST / effect / divergent).
  rq7_comparison       run the three baselines (verbatim copy-paste,
                       dependabot-style, LLM) on the same RQ5 pair set;
                       report side-by-side rates and LLM SHA hallucination.

The pipeline these modules build on:

  pattern_miner    -> clean fixes + before/after blobs (output/clean_fixes/)
  backport_gaps    -> gap (fix, branch) pairs              (gaps.jsonl)
                   -> history-classified branches          (gaps_with_history.jsonl)
  backport_ir      -> WORKFLOWBP compile / apply / oracle  (programs/, patches/)
"""

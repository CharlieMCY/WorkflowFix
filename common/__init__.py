"""Cross-package infrastructure shared by all pipeline stages.

Two modules:

  dataset   Locates the dataset-tagged output directory. Every stage
            (pattern_miner, backport_gaps, backport_ir, analysis_tools)
            routes its file I/O through here so several datasets (e.g.
            10k and 50k samples, or a holdout split) can coexist under
            output/.
  cache     Dataset-independent caches for expensive external work
            (GitHub file fetches, LLM API calls). The cache key is the
            content of the request, so a 50k run hits cache entries
            populated by a previous 10k run for the same (repo, ref,
            file) or the same (model, prompt).
"""

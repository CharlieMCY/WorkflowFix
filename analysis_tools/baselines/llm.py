"""LLM baseline: prompt Claude with (source_before, source_after, target_before),
parse the response, and verify against ground truth + scanners.

Beyond the standard apply/oracle path, this baseline measures the
distinctive failure mode highlighted in §II-D: **SHA hallucination**.
For every `actions/<owner>/<repo>@<40-hex-SHA>` in the LLM output, we
ask the live GitHub API whether that SHA exists in that repository.
Fabricated SHAs are reported separately from semantic failures so the
paper can call out the LLM-only error mode.

Uses the Anthropic Python SDK (`anthropic` package). To skip the
network and just dry-run the prompt construction, pass `dry_run=True`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_SHA_PIN_RE = re.compile(r"\buses:\s*([A-Za-z0-9._/\-]+)@([0-9a-fA-F]{40})\b")

SYSTEM_PROMPT = """You are backporting a GitHub Actions workflow security fix.
You will be given the workflow file BEFORE and AFTER the master-branch fix,
plus the same file's CURRENT contents on a divergent release branch.

Your job: produce the patched release-branch file that incorporates the
SAME security fix. Preserve all release-branch-specific structure
(different job names, step ordering, runtime versions). Output ONLY
the patched YAML, no commentary, no fences."""


USER_PROMPT_TEMPLATE = """Source-branch file BEFORE the fix:
```yaml
{source_before}
```

Source-branch file AFTER the fix:
```yaml
{source_after}
```

Release-branch file (CURRENT state, please patch this):
```yaml
{target_before}
```

Output the patched release-branch YAML:"""


@dataclass
class LLMResult:
    patched_text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fabricated_shas: list[tuple[str, str]] = field(default_factory=list)
    verified_shas: list[tuple[str, str]] = field(default_factory=list)
    error: str = ""


def build_prompt(source_before: str, source_after: str,
                 target_before: str) -> tuple[str, str]:
    return SYSTEM_PROMPT, USER_PROMPT_TEMPLATE.format(
        source_before=source_before,
        source_after=source_after,
        target_before=target_before,
    )


def apply(
    source_before: str,
    source_after: str,
    target_before: str,
    *,
    model: str = "claude-opus-4-7",
    sha_resolver=None,
    dry_run: bool = False,
    client: Any = None,
) -> LLMResult:
    """Run the LLM, parse, and check SHA hallucination.

    `sha_resolver(action, ref) -> sha|None` is called for every
    `uses: action@<HEX40>` in the LLM output. If it returns the same
    SHA, the pin is verified; if it returns a different SHA or None,
    the pin is fabricated.
    """
    sys_prompt, user_prompt = build_prompt(source_before, source_after,
                                            target_before)
    if dry_run:
        return LLMResult(patched_text="", prompt_tokens=0, completion_tokens=0,
                          error="dry_run: no LLM call made")

    if client is None:
        try:
            import anthropic
        except ImportError:
            return LLMResult(patched_text="", error=("anthropic SDK not "
                "installed; pip install anthropic"))
        client = anthropic.Anthropic()

    def _invoke(model_: str, system_: str, user_: str) -> dict:
        resp = client.messages.create(
            model=model_,
            max_tokens=4096,
            system=system_,
            messages=[{"role": "user", "content": user_}],
        )
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text")
        return {"text": text,
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens}

    # Cache by (model, system, user); repeat runs (even under different
    # DATASET_TAG) reuse the prior response and pay zero API tokens.
    from common.cache import llm_call_cached
    try:
        record = llm_call_cached(_invoke, model=model,
                                  system=sys_prompt, user=user_prompt)
    except Exception as e:
        return LLMResult(patched_text="", error=f"API error: {e}")

    text = record["text"]
    in_tok = record.get("input_tokens", 0)
    out_tok = record.get("output_tokens", 0)

    fabricated: list[tuple[str, str]] = []
    verified: list[tuple[str, str]] = []
    if sha_resolver is not None:
        for action, claimed_sha in _SHA_PIN_RE.findall(text):
            real = sha_resolver(action, claimed_sha)
            if real and real.lower() == claimed_sha.lower():
                verified.append((action, claimed_sha))
            else:
                fabricated.append((action, claimed_sha))

    return LLMResult(
        patched_text=text,
        prompt_tokens=in_tok,
        completion_tokens=out_tok,
        fabricated_shas=fabricated,
        verified_shas=verified,
    )

"""Bedrock token prices per region, from the AWS Price List Bulk API.

The price files are public JSON (no AWS credentials, no IAM permission). The only egress needed is
the host in PRICING_URL (default pricing.us-east-1.amazonaws.com). Two offers hold Bedrock models:
- AmazonBedrockFoundationModels (Anthropic, OpenAI, Cohere...): usage types like
  EUW3-MP:EUW3_input_tokens_standard-Units, per 1M tokens. *_global_standard rows are global
  cross-region inference; *_long_ctx_* and *_1h_* rows are long-context and 1-hour cache rates.
- AmazonBedrock (Amazon Nova, Mistral, Meta...): usage types like EUW3-NovaPro-input-tokens, per 1K tokens.
  Flex, priority and batch rows (other suffixes) are skipped.
Only the standard on-demand rate of each kind is kept: long-context and 1-hour-cache rows also say "Input tokens"
in inferenceType, so rows are told apart by their usage type, never by inferenceType.
The files name models ("Claude Sonnet 5.5"), not model IDs: match() maps an ID to a name.

    uv run python -m app.pricing            # print prices for BEDROCK_REGION as JSON
"""
import json
import os
import re
import time
import urllib.request

PRICING_URL = os.environ.get("PRICING_URL", "https://pricing.us-east-1.amazonaws.com").rstrip("/")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-3")
OFFERS = ("AmazonBedrockFoundationModels", "AmazonBedrock")
GLOBAL = " (global)"  # suffix of the global cross-region rates of a model

_ROW = re.compile(r"_(input_tokens|output_tokens|cache_read_tokens|cache_write_tokens)_(global_)?standard-Units$")
_KIND = {"input_tokens": "input", "output_tokens": "output", "cache_read_tokens": "cache_read", "cache_write_tokens": "cache_write"}
_ROW_1K = re.compile(r"-(input-tokens|output-tokens|cache-read-input-token-count|cache-write-input-token-count)$")
_KIND_1K = {"input-tokens": "input", "output-tokens": "output",
            "cache-read-input-token-count": "cache_read", "cache-write-input-token-count": "cache_write"}
_PER_M = {"1M tokens": 1, "1K tokens": 1000}
_cache: dict[str, tuple[float, dict]] = {}


def parse(offer: dict) -> dict[str, dict]:
    """Price file -> {"Claude Sonnet 5.5": {"input": 2.2, "output": 11.0, "cache_read": .., "cache_write": ..}} in USD per 1M tokens.
    Global cross-region rates are under "Claude Sonnet 5.5 (global)"."""
    prices: dict[str, dict] = {}
    for sku, product in offer["products"].items():
        a = product["attributes"]
        if a.get("feature") != "On-demand Inference" or "model" not in a:
            continue
        if m := _ROW.search(a.get("usagetype", "")):
            name, kind = a["model"] + (GLOBAL if m.group(2) else ""), _KIND[m.group(1)]
        elif m := _ROW_1K.search(a.get("usagetype", "")):
            name, kind = a["model"], _KIND_1K[m.group(1)]
        else:
            continue
        for term in offer["terms"].get("OnDemand", {}).get(sku, {}).values():
            for dim in term["priceDimensions"].values():
                if dim["unit"] in _PER_M:
                    prices.setdefault(name, {})[kind] = round(float(dim["pricePerUnit"]["USD"]) * _PER_M[dim["unit"]], 8)
    return {name: p for name, p in prices.items() if {"input", "output"} <= p.keys()}


def fetch(region: str = BEDROCK_REGION, max_age: float = 3600) -> dict[str, dict]:
    """Both Bedrock price files of the region, reused for max_age seconds (they change a few times a month)."""
    if region in _cache and time.monotonic() - _cache[region][0] < max_age:
        return _cache[region][1]
    prices = {}
    for offer in OFFERS:
        with urllib.request.urlopen(f"{PRICING_URL}/offers/v1.0/aws/{offer}/current/{region}/index.json", timeout=60) as r:
            prices |= parse(json.load(r))
    _cache[region] = (time.monotonic(), prices)
    return prices


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def match(model_id: str, names) -> str | None:
    """Bedrock model ID or profile (anthropic.claude-sonnet-5-5, eu.amazon.nova-pro-v1:0,
    global.anthropic.claude-haiku-4-5-20251001-v1:0) -> its name in the price file, or None.
    Global profiles map to the "(global)" rates."""
    profile = model_id.rsplit("/", 1)[-1]
    prefix, _, rest = profile.partition(".")
    is_global = prefix == "global"
    bare = rest if prefix in {"eu", "us", "apac", "au", "jp", "global", "us-gov"} else profile
    vendor, _, name = bare.partition(".")
    name = re.sub(r"-\d{8}(?=-|$)", "", name)  # release date: claude-haiku-4-5-20251001-v1:0
    version = re.search(r"-v(\d+)(?::\d+)?$", name)
    name = name[:version.start()] if version else name
    if version and version.group(1) != "1":  # claude-3-5-sonnet-...-v2:0 is "Claude 3.5 Sonnet v2"
        name += f"-v{version.group(1)}"
    index = {_key(n): n for n in names if n.endswith(GLOBAL) == is_global}
    suffix = _key(GLOBAL) if is_global else ""
    for candidate in (name, f"{vendor}-{name}", re.sub(r"-\d{4}$", "", name)):  # mistral-large-2402 -> Mistral Large
        found = index.get(_key(candidate + " " + suffix))
        if found:
            return found
    return None


if __name__ == "__main__":
    print(json.dumps(fetch(), indent=2, sort_keys=True))

"""Bedrock token prices per region, from the AWS Price List Bulk API.

The price files are public JSON (no AWS credentials, no IAM permission). The only egress needed is
the host in PRICING_URL (default pricing.us-east-1.amazonaws.com). Claude models are in the
AmazonBedrockFoundationModels offer; in-region ("standard") on-demand rows have usage types like
EUW3-MP:EUW3_input_tokens_standard-Units, priced per 1M tokens. *_global_standard rows are for
global cross-region inference, *_long_ctx_* and *_1h_* rows are long-context and 1-hour cache rates.

    uv run python -m app.pricing            # print prices for BEDROCK_REGION as JSON
"""
import json
import os
import re
import urllib.request

PRICING_URL = os.environ.get("PRICING_URL", "https://pricing.us-east-1.amazonaws.com").rstrip("/")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-3")
OFFER = "AmazonBedrockFoundationModels"

_ROW = re.compile(r"_(input_tokens|output_tokens|cache_read_tokens|cache_write_tokens)_standard-Units$")
_KIND = {"input_tokens": "input", "output_tokens": "output", "cache_read_tokens": "cache_read", "cache_write_tokens": "cache_write"}
_PER_M = {"1M tokens": 1, "1K tokens": 1000}


def parse(offer: dict) -> dict[str, dict]:
    """Price file -> {"Claude Sonnet 5.5": {"input": 2.2, "output": 11.0, "cache_read": .., "cache_write": ..}} in USD per 1M tokens."""
    prices: dict[str, dict] = {}
    for sku, product in offer["products"].items():
        a = product["attributes"]
        m = _ROW.search(a.get("usagetype", ""))
        if not m or a.get("feature") != "On-demand Inference":
            continue
        for term in offer["terms"].get("OnDemand", {}).get(sku, {}).values():
            for dim in term["priceDimensions"].values():
                if dim["unit"] in _PER_M:
                    prices.setdefault(a["model"], {})[_KIND[m.group(1)]] = float(dim["pricePerUnit"]["USD"]) * _PER_M[dim["unit"]]
    return prices


def fetch(region: str = BEDROCK_REGION) -> dict[str, dict]:
    url = f"{PRICING_URL}/offers/v1.0/aws/{OFFER}/current/{region}/index.json"
    with urllib.request.urlopen(url, timeout=60) as r:
        return parse(json.load(r))


if __name__ == "__main__":
    print(json.dumps(fetch(), indent=2, sort_keys=True))

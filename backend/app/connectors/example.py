"""Example adapter contract. Copy and replace field mapping for a real LIMS/QMS."""
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import json


def fetch_export(product: str, site: str, year: int, category: str, settings) -> dict:
    """Return export_id, count and rows with columns from SCHEMAS[category]."""
    parsed = urlparse(settings.source_base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"source", "localhost", "127.0.0.1"}):
        raise ValueError("Source API must use HTTPS outside the local demo")
    query = urlencode({"product": product, "site": site, "year": year, "category": category})
    request = Request(settings.source_base_url.rstrip("/") + "/exports?" + query,
                      headers={"Authorization": f"Bearer {settings.source_api_key}",
                               "Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=12) as response:
        data = json.load(response)
    # Map the vendor's field names and statuses here before returning the payload.
    return {"export_id": data["export_id"], "count": data["count"], "rows": data["rows"]}

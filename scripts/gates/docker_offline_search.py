"""Run inside the server container of a stack whose network has no route out (the smoke
checks that first): the server embeds with the local model its image carries, and a
question that shares no content word with a stored fact finds it."""

import json
import urllib.request

BASE = "http://127.0.0.1:7777"
IDENTITY = {"org": "dev", "namespace": "local", "agent": "docker-offline"}
FACTS = {
    "offline/refunds": "Customers who got a damaged item have their money back within five days.",
    "offline/deploys": "The staging cluster is redeployed every Tuesday night.",
    "offline/espresso": "The office espresso machine is descaled once a month.",
}
QUESTION = "how quickly is a broken purchase reimbursed"


def request(path: str, body: dict | None = None) -> dict:
    data = json.dumps({**IDENTITY, **body}).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def main() -> None:
    served = request("/v1/admin/models")
    assert served["embedding_model"] == "local", served
    for key, value in FACTS.items():
        request("/v1/memory.set", {"memory_key": key, "value": value})
    hits = request("/v1/memory.search", {"query": QUESTION, "limit": 3})
    keys = [hit["memory_key"] for hit in hits["results"]]
    assert keys and keys[0] == "offline/refunds", keys
    print("PASS offline: local embedder, a paraphrase finds its fact")


if __name__ == "__main__":
    main()

# Jev API integration

jevscan uses the HTTP wire contract checked against TypeSafe's **Python SDK v0.6.0**, rather than assuming that earlier SDK examples still apply. The SDK is not a runtime dependency of jevscan.

## Request

```http
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <TYPESAFE_API_KEY>
Content-Type: application/json
```

```json
{
  "model": "jev-latest",
  "state": {
    "language": "python",
    "source": "def add(a, b):\n    return a + b\n"
  },
  "questions": {
    "mixed-work": {
      "type": "noul",
      "instructions": "Does source interleave unrelated responsibilities?"
    }
  }
}
```

Several rules can inspect one state in one request. Noul returns `noul`; Choice returns `choice`, `confidence`, and a label probability map; Score returns `score`, `confidence`, and a zero-based rubric probability map. Score maps use string keys in raw JSON. The response also names the model and can include token usage.

jevscan validates response types, question IDs, option labels, rubric ranges, finite probabilities, and probability sums at the network/cache boundary. The rest of the pipeline operates on that validated contract. A malformed response is an operational failure, not a clean result or a reason to invent missing answers.

## Minimal standalone Python call

HTTPX is already a project dependency:

```python
import asyncio
import os

import httpx


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.typesafe.ai/v1/systemone",
            headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"},
            json={
                "model": os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest"),
                "state": {"source": "def add(a, b):\n    return a + b\n"},
                "questions": {
                    "mixed-work": {
                        "type": "noul",
                        "instructions": "Does source interleave unrelated responsibilities?",
                    }
                },
            },
        )
        response.raise_for_status()
        print(response.json()["answers"]["mixed-work"]["noul"])


if __name__ == "__main__":
    asyncio.run(main())
```

This snippet illustrates the protocol; the production client in `core/client.py` additionally validates responses, manages pooled connections, limits every attempt, handles retry headers and transient failures, and rejects oversized requests before submission.

## Lifecycle and limitations

The production endpoint is `/v1/systemone`, **not** `/v1/system_one`. Bearer authentication and the API origin follow the SDK's request-building contract. `TYPESAFE_BASE_URL` changes the trusted endpoint origin, not the request path. Remote endpoints must use HTTPS; loopback HTTP is allowed for integration testing. Redirects are not followed.

The default model alias is convenient for starting. For reproducibility, choose an explicit model ID available to your account and retest rules when changing it. jevscan does not hard-code a claimed current model release or account quota. Its byte limits are local resource controls rather than token counts.

Requests can incur provider charges. Offline inventory makes no requests; the automated HTTP tests use mock transport and do not require a key. There was **no live, authenticated API verification** in the delivery environment.

## Primary protocol references

Read on 18 September 2026:

- [SDK v0.6.0 async client](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/client/aio/client.py)
- [Endpoint request body](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/endpoints.py)
- [Endpoint and header constants](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/constants.py)
- [API origin, model alias, and environment variables](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/constants.py)
- [Question types](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/question_types.py)
- [Authentication and transport](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/transport.py)
- [Response types](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/response_types.py)

The code and tests are grounded in those published schemas; mock tests are not evidence of current provider availability or semantic accuracy.

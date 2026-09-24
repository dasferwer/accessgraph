import json
import os
import time
import uuid
from urllib.request import Request, urlopen

base = os.environ.get("API_URL", "http://localhost:8091")
key = os.environ.get("API_KEY", "local-demo-key")
suffix = uuid.uuid4().hex[:8]


def call(path, data, method="POST"):
    request = Request(
        base + path,
        data=json.dumps(data).encode(),
        method=method,
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    with urlopen(request, timeout=10) as response:
        return json.load(response)


member = {"resource": "team:" + suffix, "relation": "member", "subject": "user:" + suffix}
for edge in [
    member,
    {"resource": "folder:" + suffix, "relation": "editor", "subject": "team:" + suffix},
    {"resource": "document:" + suffix, "relation": "parent", "subject": "folder:" + suffix},
]:
    call("/relations", edge)
body = {"user": "user:" + suffix, "resources": ["document:" + suffix]}
assert call("/check", body)["checks"]["document:" + suffix]["allowed"]
assert call("/check", body)["checks"]["document:" + suffix]["allowed"]
call("/relations", member, "DELETE")
start = time.monotonic()
result = call("/check", body)
assert not result["checks"]["document:" + suffix]["allowed"]
print(
    json.dumps(
        {
            "denied": True,
            "check_after_revoke_ms": round((time.monotonic() - start) * 1000, 2),
            "revision": result["revision"],
        },
        indent=2,
    )
)

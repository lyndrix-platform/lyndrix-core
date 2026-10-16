"""Log redaction: env-var style ``{key, value}`` pairs must never print a secret value."""
from core.logger import mask_secrets


def test_env_var_pairs_keep_names_and_mask_values():
    payload = {
        "operation": "docker:spawn",
        "args": {
            "image": "ghcr.io/x/runner:latest",
            "env_vars": [
                {"key": "LYNDRIX_AI_BRIDGE_TOKEN", "value": "s3cr3t"},
                {"key": "LYNDRIX_API_URL", "value": "http://core:8081"},
            ],
        },
    }
    out = mask_secrets(payload)
    env = out["args"]["env_vars"]
    assert env[0] == {"key": "LYNDRIX_AI_BRIDGE_TOKEN", "value": "********"}
    assert env[1] == {"key": "LYNDRIX_API_URL", "value": "********"}
    assert out["args"]["image"] == "ghcr.io/x/runner:latest"
    assert "s3cr3t" not in repr(out) and "8081" not in repr(out)


def test_pair_keys_that_are_not_env_names_are_masked_too():
    # a secret sitting in a "key" field (identifier-like) must not become readable
    out = mask_secrets({"items": [{"key": "lyk_SDvV2DuC0abc", "value": "x", "label": "runner"},
                                  {"key": "color", "value": "blue"}]})["items"]
    assert out[0] == {"label": "runner", "key": "********", "value": "********"}
    assert out[1] == {"key": "********", "value": "********"}


def test_plain_sensitive_keys_still_masked():
    out = mask_secrets({"token": "t", "nested": {"password": "p", "name": "n"}, "key": "k"})
    assert out == {"token": "********", "nested": {"password": "********", "name": "n"}, "key": "********"}

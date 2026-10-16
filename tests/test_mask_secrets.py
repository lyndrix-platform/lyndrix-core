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
    # inside an env list every value is masked, the name stays readable
    assert env[1] == {"key": "LYNDRIX_API_URL", "value": "********"}
    assert out["args"]["image"] == "ghcr.io/x/runner:latest"
    assert "s3cr3t" not in repr(out)


def test_pairs_outside_env_lists_mask_by_name_only():
    items = [{"key": "GITLAB_TOKEN", "value": "abc"}, {"key": "color", "value": "blue"}]
    out = mask_secrets({"items": items})["items"]
    assert out[0]["value"] == "********" and out[1]["value"] == "blue"


def test_plain_sensitive_keys_still_masked():
    out = mask_secrets({"token": "t", "nested": {"password": "p", "name": "n"}, "key": "k"})
    assert out == {"token": "********", "nested": {"password": "********", "name": "n"}, "key": "********"}

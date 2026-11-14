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


# --- SC-05: substring key matching, env containers, free text, formatter -----------
import logging  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

from core.logger import EnterpriseFormatter, _sensitive_name, mask_text  # noqa: E402

FAKE = "dummy-token-not-real"


@pytest.mark.parametrize("key", [
    "github_token", "GITLAB_TOKEN", "api_key", "apiKey", "API-KEY", "apikey", "client_secret",
    "clientSecret", "access_token", "accessToken", "refresh_token", "bind_password",
    "LDAP_BIND_PASSWORD", "db_passwd", "webhook_url", "webhook", "Authorization",
    "proxy_authorization", "private_key", "ssh_private_key", "master_key", "LYNDRIX_MASTER_KEY",
    "unseal_keys", "root_token", "system_api_key", "secret_value", "credentials", "passphrase",
    "session_cookie", "auth_header", "basic_auth", "x-gitlab-token", "token", "key", "auth",
    "password", "secret",
])
def test_secret_bearing_names_are_recognised(key):
    assert _sensitive_name(key), key
    assert mask_secrets({key: FAKE}) == {key: "********"}


@pytest.mark.parametrize("key", [
    "token_count", "tokens_used", "max_tokens", "has_token", "token_type", "token_ttl",
    "token_expires", "token_endpoint", "secret_ref", "secret_configured", "password_min_length",
    "password_policy", "requires_password", "keyboard", "sort_key", "cache_key", "primary_key",
    "key_name", "keys", "monkey", "author", "authors", "auth_type", "auth_provider",
    "authenticated", "tokenizer", "tokenizer_model", "name", "url", "status", "webhook_id",
    "credential_type", "is_secret", "apikey_configured",
])
def test_ordinary_names_are_left_alone(key):
    assert not _sensitive_name(key), key
    assert mask_secrets({key: "plain value"}) == {key: "plain value"}


def test_nested_headers_and_values_under_sensitive_keys():
    out = mask_secrets({"headers": {"Authorization": f"Bearer {FAKE}", "Accept": "application/json"},
                        "client": {"client_secret": {"nested": FAKE}}})
    assert out == {"headers": {"Authorization": "********", "Accept": "application/json"},
                   "client": {"client_secret": "********"}}


def test_env_containers_mask_every_value_shape():
    out = mask_secrets({
        "env": {"GITLAB_TOKEN": FAKE, "API_URL": "http://core:8081"},
        "environment": [f"GITLAB_TOKEN={FAKE}", "PLAIN=1", FAKE],
        "env_vars": [{"key": "A_TOKEN", "value": FAKE}],
    })
    assert out["env"] == {"GITLAB_TOKEN": "********", "API_URL": "********"}
    assert out["environment"] == ["GITLAB_TOKEN=********", "PLAIN=********", "********"]
    assert out["env_vars"] == [{"key": "A_TOKEN", "value": "********"}]
    assert FAKE not in repr(out)


def test_string_values_are_scrubbed_inside_payloads():
    out = mask_secrets({"url": f"https://oauth2:{FAKE}@gitlab.example.invalid/g/r.git",
                        "error": f"fatal: could not read from https://x:{FAKE}@host.invalid/r"})
    assert FAKE not in repr(out)
    assert out["url"] == "https://***@gitlab.example.invalid/g/r.git"


@pytest.mark.parametrize("text", [
    f"Cloning from https://oauth2:{FAKE}@gitlab.example.invalid/x.git",
    f"Cloning from https://{FAKE}@github.com/o/r.git",
    f"Authorization: Bearer {FAKE}",
    f"authorization: token {FAKE}",
    f"headers={{'Authorization': 'Basic {FAKE}'}}",
    f"PRIVATE-TOKEN: {FAKE}",
    f"X-Gitlab-Token={FAKE}",
    f"sent Bearer {FAKE} to upstream",
    f"github_token={FAKE}",
    f"api_key: {FAKE}",
    f'{{"client_secret": "{FAKE} with spaces"}}',
    f"password='{FAKE}'",
    f"GET /hook?token={FAKE}&x=1",
    f"?key={FAKE}",
    f"'key': '{FAKE}'",
    "created key lyk_" + "A1b2C3d4E5f6",
    "pat ghp_" + "a1B2c3D4e5F6g7H8i9J0",
    "pat github_pat_" + "11AAAAAAA0_abcdefghij",
    "glpat-" + "a1B2c3D4e5F6g7H8i9J0 was rejected",
    "vault token hvs." + "CAESIabcdefghijklmnop",
    "model key sk-" + "abcdefghijklmnopqrstuvwx",
])
def test_free_text_secrets_are_masked(text):
    masked = mask_text(text)
    assert FAKE not in masked
    for fragment in ("A1b2C3d4E5f6", "a1B2c3D4e5F6g7H8i9J0", "11AAAAAAA0", "CAESIabcdefghij", "abcdefghijklmnopqrstuvwx"):
        assert fragment not in masked
    assert "****" in masked or "***@" in masked


@pytest.mark.parametrize("text", [
    "AUTH: Token restored during runtime.",
    "Key: Value pairs loaded",
    "token_count=42 max_tokens: 4096",
    "keyboard=de monkey=1 author: marvin",
    "Cloning from https://gitlab.example.invalid/g/r.git",
    "ssh://git@github.com/o/r.git",
    "EVENT: plugin:installed | Data: {'repo': 'x', 'path': '/app/plugins/x'}",
    "task-runner started; risk-level=low; sk-learn",
    "time=12:30:01 status: ok ratio=3/4",
    "has_token=True auth_type=token secret_ref=plugins/x/git",
    "No colon or equals here at all",
])
def test_ordinary_text_is_not_touched(text):
    assert mask_text(text) == text


def _format(msg, *args, exc_info=None):
    record = logging.LogRecord("Core:Test", logging.INFO, __file__, 1, msg, args, exc_info)
    return EnterpriseFormatter("%(name)s | %(message)s").format(record), record


def test_formatter_masks_percent_args():
    line, record = _format("clone %s with password=%s", f"https://u:{FAKE}@h.invalid/r", FAKE)
    assert FAKE not in line
    assert "password=********" in line
    # the record itself is left intact for other consumers
    assert record.args == (f"https://u:{FAKE}@h.invalid/r", FAKE)


def test_formatter_masks_exception_text_and_traceback():
    try:
        raise RuntimeError(f"Cmd('git') failed: git clone https://oauth2:{FAKE}@gitlab.example.invalid/r.git")
    except RuntimeError:
        line, _ = _format("sync failed", exc_info=sys.exc_info())
    assert "Traceback" in line and "RuntimeError" in line
    assert FAKE not in line


def test_formatter_masks_dict_messages():
    line, record = _format({"github_token": FAKE, "repo": "x"})
    assert FAKE not in line and "'repo': 'x'" in line
    assert record.msg == {"github_token": FAKE, "repo": "x"}  # record left intact


def test_formatter_output_is_cached_per_record():
    formatter = EnterpriseFormatter("%(message)s")
    record = logging.LogRecord("Core:Test", logging.INFO, __file__, 1, "token=%s", (FAKE,), None)
    first = formatter.format(record)
    assert formatter.format(record) is first
    assert first == "token=********"


def test_logger_name_is_never_masked():
    line, _ = _format("ready")
    assert line == "Core:Test | ready"

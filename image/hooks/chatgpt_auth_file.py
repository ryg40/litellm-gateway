"""LiteLLM 1.101.0 and 1.103.0: one ChatGPT account per deployment.

A chatgpt deployment names its auth file with `chatgpt_auth_file` in
`litellm_params`, as in open upstream PR #41928. Remove this hook when the
pinned upstream has the key; install() stops the start when it does.

The hook is off by default. It changes LiteLLM only when the environment
of the process sets CHATGPT_AUTH_FILE_HOOK=on (1, true, yes, on). The switch
must be in the environment of the container: `environment_variables` in the
proxy configuration is applied after the interpreter start. With the hook
off, a proxy started with --config stops when a deployment has the key,
because upstream would ignore the key and use the process account.

Rules:
- Only the admin configuration sets the key. The proxy rejects a client
  request that carries it anywhere in the body with HTTP 400. A string is
  read as JSON text too, except the prompt fields of CONTENT_KEYS: a prompt
  that quotes the key is not a parameter.
- The gateway never starts the device-code login for a deployment account.
  A missing, invalid or unrefreshable auth file fails the request at once.
- get_llm_provider() no longer reads a token. The token comes from the
  deployment in validate_environment(), so the process needs no account.
  A chatgpt deployment without the key fails. CHATGPT_AUTH_FILE_REQUIRED=false
  lets such a deployment use the upstream process account.
- The account headers Authorization and ChatGPT-Account-Id always win over
  client headers (`extra_headers`, `headers`): AccountHeaders keeps them
  through each merge, and sign_request() sets them again as the last step.
- One authenticator per auth file. A refresh writes only its own file.
- The auth file must be inside CHATGPT_AUTH_FILE_ROOT (default
  CHATGPT_TOKEN_DIR, then the upstream default directory).
- No token value goes into a log line or an error message. The error for
  the client names the deployment and the account, not the path of the auth
  file; the gateway log names the path.
"""
from importlib import import_module
import inspect
import json
import os
import sys
import tempfile
import threading

from litellm_versions import TESTED_VERSIONS, untested  # noqa: F401  (TESTED_VERSIONS for the tests)

KEY = "chatgpt_auth_file"
SWITCH_ENV = "CHATGPT_AUTH_FILE_HOOK"
REQUIRED_ENV = "CHATGPT_AUTH_FILE_REQUIRED"
ROOT_ENV = "CHATGPT_AUTH_FILE_ROOT"
_MAX_DEPTH = 32

_authenticators = {}
_authenticators_lock = threading.Lock()


class StopStart(SystemExit):
    """Stop the interpreter at start.

    site.py reports an Exception from sitecustomize and goes on without the
    hook; upstream would then ignore the key and log in with the process
    account. SystemExit is not caught there, so the process stops.
    """


class AuthFileError(Exception):
    """The configured auth file is not usable. The message names no token.

    reason is for the client. str() adds the path, for the gateway log only.
    """

    def __init__(self, reason, path=None):
        super().__init__(f"{reason}: {path}" if path else reason)
        self.reason = reason


class TooDeep(Exception):
    """A client value nests too deeply to check it for the key."""


def _upstream_has_key():
    from litellm.llms.chatgpt import authenticator
    from litellm.types.router import GenericLiteLLMParams

    return (
        KEY in GenericLiteLLMParams.model_fields
        or hasattr(authenticator, "get_cached_authenticator")
        or "auth_file" in inspect.signature(authenticator.Authenticator.__init__).parameters
    )


def enabled():
    return os.getenv(SWITCH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def active():
    """True when install() changed LiteLLM in this process."""
    upstream = sys.modules.get("litellm.llms.chatgpt.responses.transformation")
    return bool(upstream and getattr(upstream.ChatGPTResponsesAPIConfig, "_gateway_auth_file", False))


def _config_path(argv):
    args = argv[1:]
    for i, arg in enumerate(args):
        if arg in ("--config", "-c") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return os.getenv("CONFIG_FILE_PATH")


def config_uses_key(path, _seen=None):
    """True when a deployment of the YAML file, or of its include files, has the key."""
    import yaml

    seen = set() if _seen is None else _seen
    real = os.path.realpath(path)
    if real in seen:
        return False
    seen.add(real)
    try:
        with open(path) as f:
            config = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return False  # LiteLLM reports the file itself.
    if not isinstance(config, dict):
        return False
    for deployment in config.get("model_list") or []:
        if isinstance(deployment, dict) and KEY in (deployment.get("litellm_params") or {}):
            return True
    base = os.path.dirname(os.path.abspath(path))
    return any(config_uses_key(os.path.join(base, name), seen) for name in config.get("include") or []
               if isinstance(name, str))


def _check_config_without_hook():
    path = _config_path(sys.argv)
    if path and config_uses_key(path):
        raise StopStart(
            f"{path} sets {KEY}, but {SWITCH_ENV} is off: upstream LiteLLM would ignore the key and "
            f"use the process account. Set {SWITCH_ENV}=on in the environment of the container; "
            "environment_variables in the configuration is applied too late.")


def _required():
    """On unless CHATGPT_AUTH_FILE_REQUIRED is false (also 0, no, off)."""
    return os.getenv(REQUIRED_ENV, "true").strip().lower() not in ("0", "false", "no", "off")


def allowed_root():
    root = os.getenv(ROOT_ENV) or os.getenv("CHATGPT_TOKEN_DIR") or "~/.config/litellm/chatgpt"
    return os.path.realpath(os.path.expanduser(root))


def resolve_auth_file(value):
    """Return the real path of a configured auth file, or raise AuthFileError."""
    if not isinstance(value, str) or not value.strip():
        raise AuthFileError(f"{KEY} must be a non-empty path")
    if "\0" in value:
        raise AuthFileError(f"{KEY} contains a NUL character", repr(value))
    if ".." in value.replace("\\", "/").split("/"):
        raise AuthFileError(f"{KEY} contains '..'", repr(value))
    root = allowed_root()
    try:
        path = os.path.expanduser(value)
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        real = os.path.realpath(path)
        outside = real == root or os.path.commonpath([root, real]) != root
    except ValueError as exc:
        raise AuthFileError(f"{KEY} is not a valid path", f"{value!r} ({exc})") from None
    if outside:
        raise AuthFileError(f"{KEY} is outside the allowed directory", f"{value!r} (root {root})")
    return real


def account_name(value):
    """Name of the account for messages: the directory of the auth file, else the file name."""
    if not isinstance(value, str):
        return "?"
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".")]
    name = parts[-2] if len(parts) > 1 else (parts[0] if parts else "?")
    return "".join(c if c.isprintable() else "?" for c in name)[:64]


def auth_file_from(litellm_params):
    if litellm_params is None:
        return None
    if isinstance(litellm_params, dict):
        value = litellm_params.get(KEY)
    else:
        value = getattr(litellm_params, KEY, None)
        if value is None:
            extra = getattr(litellm_params, "model_extra", None) or {}
            value = extra.get(KEY)
    return value if value not in (None, "") else None


def _authenticator_class():
    from litellm.llms.chatgpt.authenticator import Authenticator, _optional_str
    from litellm.llms.chatgpt.common_utils import RefreshAccessTokenError

    class AccountAuthenticator(Authenticator):
        """Upstream Authenticator bound to one file. It never logs in."""

        def __init__(self, auth_file):
            # Do not call the upstream __init__: it reads the process
            # environment and creates directories.
            self.auth_file = auth_file
            self.token_dir = os.path.dirname(auth_file)
            self.lock = threading.Lock()

        def get_access_token(self):
            with self.lock:
                auth_data = self._read_auth_file()
                if not auth_data:
                    raise AuthFileError("auth file is missing or not valid", self.auth_file)
                access_token = _optional_str(auth_data.get("access_token"))
                if access_token and not self._is_token_expired(auth_data, access_token):
                    return access_token
                refresh_token = _optional_str(auth_data.get("refresh_token"))
                if not refresh_token:
                    raise AuthFileError("auth file has no usable token", self.auth_file)
                try:
                    return self._refresh_tokens(refresh_token)["access_token"]
                except RefreshAccessTokenError as exc:
                    # The upstream message can hold the token response body.
                    raise AuthFileError(
                        f"token refresh for the auth file failed (HTTP {getattr(exc, 'status_code', 'error')})",
                        self.auth_file) from None

        def _read_auth_file(self):
            try:
                with open(self.auth_file) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return None
            return data if isinstance(data, dict) else None

        def _write_auth_file(self, data):
            # Atomic, mode 0600, and only this file.
            fd, tmp = tempfile.mkstemp(dir=self.token_dir, prefix=".auth-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.auth_file)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        def _login_device_code(self):
            raise AuthFileError("device-code login is disabled in the gateway", self.auth_file)

        def _wait_for_access_token(self, timeout_seconds):
            return None

    return AccountAuthenticator


def get_authenticator(real_path):
    with _authenticators_lock:
        found = _authenticators.get(real_path)
        if found is None:
            found = _authenticators[real_path] = _authenticator_class()(real_path)
        return found


def _deployment(model, litellm_params):
    model_info = (litellm_params.get("model_info") if isinstance(litellm_params, dict)
                  else getattr(litellm_params, "model_info", None))
    deployment_id = model_info.get("id") if isinstance(model_info, dict) else None
    return f"{model} (id {deployment_id})" if deployment_id else model


def _account(model, litellm_params):
    """Return (token, account_id) for the deployment, or None for the process account."""
    from litellm._logging import verbose_logger
    from litellm.exceptions import AuthenticationError

    model = _deployment(model, litellm_params)
    value = auth_file_from(litellm_params)
    if value is None:
        if _required():
            raise AuthenticationError(
                message=f"chatgpt deployment {model} has no {KEY}. Set {REQUIRED_ENV}=false "
                        "to use the process account.",
                llm_provider="chatgpt", model=model)
        return None
    try:
        authenticator = get_authenticator(resolve_auth_file(value))
        return authenticator.get_access_token(), authenticator.get_account_id()
    except AuthFileError as exc:
        name = account_name(value)
        # The path goes to the gateway log only.
        verbose_logger.warning("chatgpt deployment %s, account %s: %s", model, name, exc)
        raise AuthenticationError(
            message=f"chatgpt deployment {model}, account {name}: {exc.reason}. "
                    f"Run scripts/login-codex-account.sh {name}.",
            llm_provider="chatgpt", model=model) from None


FIXED_HEADERS = ("authorization", "chatgpt-account-id")


class AccountHeaders(dict):
    """Request headers of one chatgpt deployment. The account values always win.

    LiteLLM merges the client headers (`extra_headers` and `headers` of the
    request body) into the result of validate_environment() with dict.update().
    Each write to this dict sets the account values again and drops other
    spellings of the two names. sign_request() sets them once more as the
    last step before the request goes out.
    """

    def __init__(self, headers, pinned):
        super().__init__(headers)
        self.pinned = dict(pinned)
        self._pin()

    def _pin(self):
        pinned = getattr(self, "pinned", None)  # copy and pickle set items before the attribute
        if not pinned:
            return
        for name in [k for k in dict.keys(self) if isinstance(k, str) and k.lower() in FIXED_HEADERS
                     and k not in pinned]:
            dict.__delitem__(self, name)
        dict.update(self, pinned)

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        self._pin()

    def update(self, *args, **kwargs):
        dict.update(self, *args, **kwargs)
        self._pin()

    def setdefault(self, key, default=None):
        dict.setdefault(self, key, default)
        self._pin()
        return self.get(key)

    def __ior__(self, other):
        self.update(other)
        return self

    def copy(self):
        return AccountHeaders(self, self.pinned)


def _account_headers(headers, token, account_id, litellm_params):
    from litellm.llms.chatgpt.common_utils import ensure_chatgpt_session_id, get_chatgpt_default_headers

    own = get_chatgpt_default_headers(token, account_id, ensure_chatgpt_session_id(litellm_params))
    pinned = {"Authorization": own["Authorization"]}
    if account_id:
        pinned["ChatGPT-Account-Id"] = account_id
    rest = {k: v for k, v in (headers or {}).items() if k.lower() not in FIXED_HEADERS}
    return AccountHeaders({**own, **rest}, pinned)


# Prompt fields at the top level of a request. LiteLLM sends them to the model
# and does not read their strings as parameters, so a JSON text there is text.
CONTENT_KEYS = frozenset({"messages", "input", "prompt", "instructions", "system"})


def _walk(value, depth=0, text=False):
    """Yield True when a client value carries the key. Raise TooDeep when it nests too deeply.

    text=True: the value is prompt content; its strings are not read as JSON.
    """
    if depth > _MAX_DEPTH:
        raise TooDeep()
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and (key == KEY or f"[{KEY}]" in key):
                yield True
                return
            yield from _walk(item, depth + 1, text or (depth == 0 and key in CONTENT_KEYS))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item, depth + 1, text)
    elif isinstance(value, str) and KEY in value and not text:
        try:
            parsed = json.loads(value)
        except RecursionError:
            raise TooDeep() from None
        except ValueError:
            return
        yield from _walk(parsed, depth + 1)


def client_sets_key(request_body):
    return any(_walk(request_body))


def install():
    if not enabled():
        # Change nothing. Only read the configuration of a proxy start.
        _check_config_without_hook()
        return
    problem = untested()
    if problem:
        raise StopStart(problem)
    if _upstream_has_key():
        raise StopStart("Upstream LiteLLM has chatgpt_auth_file; remove the chatgpt_auth_file hook")

    from fastapi import HTTPException
    from litellm.exceptions import AuthenticationError
    from litellm.litellm_core_utils import get_litellm_params as params_module
    from litellm.llms.chatgpt.chat.transformation import ChatGPTConfig
    from litellm.llms.chatgpt.common_utils import GetAccessTokenError
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
    from litellm.proxy.auth import auth_utils
    from litellm.types.utils import all_litellm_params

    if getattr(ChatGPTResponsesAPIConfig, "_gateway_auth_file", False):
        return

    # Keep the key in litellm_params and out of the provider request body.
    # completion() forwards FORWARDED_KWARGS_KEYS to get_litellm_params(); the
    # chat-to-responses bridge then passes litellm_params on to responses().
    if KEY not in all_litellm_params:
        all_litellm_params.append(KEY)
    params_module.OPTIONAL_KWARGS_KEYS = params_module.OPTIONAL_KWARGS_KEYS | {KEY}
    params_module._OPTIONAL_KWARGS_KEYS = params_module.OPTIONAL_KWARGS_KEYS
    main_module = import_module("litellm.main")
    main_module.FORWARDED_KWARGS_KEYS = main_module.FORWARDED_KWARGS_KEYS | {KEY}

    # Client requests never set the key. Not even with an admin opt-in.
    original_safe = auth_utils.is_request_body_safe

    def is_request_body_safe(request_body, general_settings, llm_router, model):
        try:
            found = client_sets_key(request_body)
        except (TooDeep, RecursionError):
            raise HTTPException(status_code=400, detail={
                "error": f"Rejected Request: the request nests values too deeply to check it for {KEY}."})
        if found:
            raise HTTPException(status_code=400, detail={
                "error": f"Rejected Request: {KEY} is not allowed in the request. Only the proxy configuration sets it."})
        return original_safe(request_body, general_settings, llm_router, model)

    auth_utils.is_request_body_safe = is_request_body_safe

    # get_llm_provider() runs before the deployment is known. Read no token here.
    def _get_openai_compatible_provider_info(self, model, api_base, api_key, custom_llm_provider):
        return self.authenticator.get_api_base(), None, custom_llm_provider

    def credentials(config, model, litellm_params, api_key=None):
        """(token, account_id) of the deployment account, else of the process account."""
        account = _account(model, litellm_params)
        if account is not None:
            return account
        if not api_key:
            try:
                api_key = config.authenticator.get_access_token()
            except GetAccessTokenError as e:
                raise AuthenticationError(model=model, llm_provider="chatgpt", message=str(e))
        return api_key, config.authenticator.get_account_id()

    def chat_validate_environment(self, headers, model, messages, optional_params, litellm_params,
                                  api_key=None, api_base=None):
        token, account_id = credentials(self, model, litellm_params, api_key)
        validated = super(ChatGPTConfig, self).validate_environment(
            headers, model, messages, optional_params, litellm_params, token, api_base)
        return _account_headers(validated, token, account_id, litellm_params)

    def responses_validate_environment(self, headers, model, litellm_params):
        token, account_id = credentials(self, model, litellm_params)
        return _account_headers(headers, token, account_id, litellm_params)

    def pinned_sign_request(original):
        # The handler calls sign_request after it has merged the client headers
        # and built the body: the last step before the request goes out.
        def sign_request(self, headers, *args, **kwargs):
            pinned = getattr(headers, "pinned", None)
            if pinned is None:
                # LiteLLM replaced the dict of validate_environment(): fail closed.
                raise AuthenticationError(
                    message="chatgpt request headers lost the account values; review chatgpt_auth_file",
                    llm_provider="chatgpt", model=kwargs.get("model") or "")
            signed, body = original(self, headers, *args, **kwargs)
            return dict(AccountHeaders(signed, pinned)), body
        return sign_request

    ChatGPTConfig._get_openai_compatible_provider_info = _get_openai_compatible_provider_info
    ChatGPTConfig.validate_environment = chat_validate_environment
    ChatGPTConfig.sign_request = pinned_sign_request(ChatGPTConfig.sign_request)
    ChatGPTResponsesAPIConfig.validate_environment = responses_validate_environment
    ChatGPTResponsesAPIConfig.sign_request = pinned_sign_request(ChatGPTResponsesAPIConfig.sign_request)
    ChatGPTResponsesAPIConfig._gateway_auth_file = True

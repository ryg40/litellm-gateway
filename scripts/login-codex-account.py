#!/usr/bin/env python3
"""Device-code login for one gateway account. Run only in the login container.

scripts/login-codex-account.sh starts it. The gateway hook never logs in.
"""
import os
import stat
import sys


def main(authenticator_class=None):
    # sitecustomize.py imports the hook in each process; it stays off here.
    hook = sys.modules.get("chatgpt_auth_file")
    if hook is not None and hook.active():
        sys.exit("The chatgpt_auth_file hook is active. Run scripts/login-codex-account.sh instead.")
    os.umask(0o077)
    if authenticator_class is None:
        from litellm.llms.chatgpt.authenticator import Authenticator as authenticator_class
    auth = authenticator_class()
    auth.get_access_token()
    os.chmod(auth.auth_file, 0o600)
    if stat.S_IMODE(os.stat(auth.auth_file).st_mode) != 0o600:
        sys.exit("The auth file does not have mode 0600.")
    print("Login complete. The auth file stays in this account's token directory.")


if __name__ == "__main__":
    main()

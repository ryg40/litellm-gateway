#!/usr/bin/env python3
"""Run inside one isolated Codex service. Never import CLI refresh tokens."""
import os
from litellm.llms.chatgpt.authenticator import Authenticator

os.umask(0o077)
auth = Authenticator()
auth.get_access_token()
print("Login complete. Credentials stay in this account's token directory.")

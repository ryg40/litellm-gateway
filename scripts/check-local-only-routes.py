#!/usr/bin/env python3
"""Exit 1 when a route in LOCAL_ONLY_ROUTES can reach a model outside the private network.

Run this after a hand edit of the host gateway file and before a restart of the gateway.
A scripted write through gateway_config.save runs the same check.
"""
import sys

import gateway_config

routes = gateway_config.local_only_routes(gateway_config.REPO)
if not routes:
    print("LOCAL_ONLY_ROUTES is empty. Nothing to check.")
    sys.exit(0)
_, config = gateway_config.load(gateway_config.REPO)
problems = gateway_config.local_only_problems(config, routes)
for problem in problems:
    print(problem)
if problems:
    sys.exit(1)
print(f"{len(routes)} local-only routes are clean.")

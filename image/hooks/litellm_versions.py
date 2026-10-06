"""The LiteLLM versions that the hooks of this image are tested with.

One list for all hooks. To test another version: add it here, run the tests
with that base image (scripts/build.sh test, tests/README.md), and keep the
version only when the tests pass.
"""
from importlib.metadata import version

TESTED_VERSIONS = ("1.101.0", "1.103.0")


def untested():
    """Return the stop message when the installed LiteLLM is not tested, else None."""
    found = version("litellm")
    if found in TESTED_VERSIONS:
        return None
    return (f"LiteLLM {found} is not tested with the hooks of this image "
            f"(tested: {', '.join(TESTED_VERSIONS)}). Add the version to TESTED_VERSIONS in "
            "image/hooks/litellm_versions.py, run scripts/build.sh test with this BASE_IMAGE, "
            "and keep the version only when the tests pass.")

"""Disposable follow-up for review-stop acceptance. Do not merge."""

def normalized_names(names):
    return [name.strip().casefold() for name in names]

# Test release 20261103: cancellation diagnostic acceptance.
DIAGNOSTIC_ACCEPTANCE_ROUND = 4

WAIT_EXIT_ACCEPTANCE_ROUND = 3

import logging

# Upstream-failure tests trigger warning logs on purpose; keep test output clean.
# (test_api.ApiHygieneTests.test_failures_are_logged checks that they are emitted.)
logging.getLogger("fuel").setLevel(logging.CRITICAL)

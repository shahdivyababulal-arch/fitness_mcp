import os

# Never export telemetry from a test run. Cloud Trace is now the only backend,
# so with application default credentials present a test would write spans
# into the real project alongside production traffic.
os.environ["OTEL_ENABLED"] = "false"

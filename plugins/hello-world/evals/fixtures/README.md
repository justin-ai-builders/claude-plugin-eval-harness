# Fixtures

Fixtures are committed, synthetic inputs copied into a temporary directory for each test. The harness never runs against the committed copy.

`workspace/README.md` is intentionally simple. The example suites use `file_unchanged` to prove the hello skill did not modify it.

Real plugins can add representative project files, input documents, or other synthetic context here. Never commit production data, credentials, personal information, or material that should not appear verbatim in an eval report.

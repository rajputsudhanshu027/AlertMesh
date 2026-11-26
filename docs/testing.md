# Running tests

Use the repository's scoped helper when working on a change:

```sh
test-changed alertmesh/service.py tests/test_service.py
```

`test-changed` runs only test files related to the paths supplied. Python projects with `unittest.TestCase` tests use the standard-library unittest runner; projects with pytest configuration use pytest.

Run the complete suite with `test-full`. Do not invoke raw test runners directly; the helpers keep output concise and preserve scoped test selection.

---
name: write-tests-first
description: Write a failing test before the implementation, then make it pass. Use for any bug fix or new function so behavior is pinned by a test.
---

# write-tests-first

When asked to fix a bug or add a function:

1. First write a test that reproduces the bug or specifies the new behavior, and
   confirm it FAILS for the right reason.
2. Implement the smallest change that makes the test pass.
3. Run the full test suite; do not stop until it is green.
4. Keep the change minimal — no unrelated refactors.

This pins behavior with a regression test and avoids "fixed" claims that aren't
covered, which is exactly what the A/B harness measures via `tests_pass`.

# A failed local deletion can be committed by an unrelated later write

Zero × Youngseok Oh | 4 October 2026

This is a follow-up to [qdrant-client #1515](https://github.com/qdrant/qdrant-client/pull/1515), which addresses our earlier [report #1510](https://github.com/qdrant/qdrant-client/issues/1510). The original per-point ordering fix belongs to **adityaanikam**. Our commit builds directly on that author's `560d8c0d3b826bf2a8e3943fca27724332086500`; it does not replace or take credit for that work.

**Tested follow-up:** `01ed8bec882f1ee084a757d17f21069e4fe29700`. This commit contains only `CollectionPersistence.delete` transaction cleanup and a new regression file. The evidence branch is separate so this fix commit can be cherry-picked without the review documents.

## Failure mechanism

In SQLite's rollback-journal mode, a reader can permit DELETE to execute but prevent COMMIT. SQLite raises `SQLITE_BUSY` and leaves the write transaction active. The reordered PR correctly leaves the failed point visible in memory, but the SQLite connection still holds its uncommitted deletion. A later successful upsert for a different point commits both operations. The old point is still visible to the current Qdrant client, yet disappears when the store is reopened.

Executed example: start with IDs 1, 2, 3, 9; block the first delete's commit; release the reader and upsert ID 10. The author's head returns `[1, 2, 3, 9, 10]` before closing and `[2, 3, 9, 10]` after reopening. Blocking the second delete preserves ID 1's committed removal, but exhibits the same mismatch for ID 2.

The follow-up uses the SQLite connection context manager around the existing DELETE. A failed statement or failed commit rolls back that transaction before the exception propagates. It does not undo previously committed points or promise atomic deletion of the entire selection.

References: [SQLite transaction error behavior](https://sqlite.org/lang_transaction.html) and [Python's connection context manager](https://docs.python.org/3.12/library/sqlite3.html#how-to-use-the-connection-context-manager).

## Actual tests

One parameterized regression function covers **8 combinations**: synchronous or native asynchronous client; point-ID or filter selection; lock at the first deletion or after a committed prefix. It uses a real read-only SQLite connection with an explicit read transaction. A trace callback chooses the lock boundary; it never replaces a Qdrant method or raises a synthetic storage error. The test observes a genuine `SQLITE_BUSY` at COMMIT. The 30 ms busy timeout bounds the experiment; no wall-clock performance threshold is asserted.

All Qdrant calls are sequential. The second connection is a controlled read-lock fault source, not a claim that concurrent Qdrant writers are supported. The tests compare scroll/count/retrieve, payloads and vectors, dense and sparse-IDF query scores, unrelated writes, retry, and close/reopen. They use new temporary stores and synthetic points only.

| Exact tested runtime | New busy regressions | Three selected files combined |
|---|---:|---:|
| Author head `560d8c0d` | 8 failed | 14 passed, 12 failed |
| Follow-up `01ed8bec` | 8 passed | 22 passed, 4 failed |

The **same four existing test identities fail in both trees**: `test_prevent_parallel_access`, `test_local_dense_persistence`, and both `test_local_sparse_persistence` variants. Their logs include Windows open-file cleanup failures and unexpected point counts before cleanup. The different random counts are not claimed to be identical failures byte-for-byte. These tests were not edited, excluded, or marked expected-failure. This is not an all-green full-suite result.

The other two selected files are `tests/test_local_persistence.py` (including the author's existing before/after-commit retry cases) and `tests/test_in_memory.py`. Ruff check and format check passed for the two changed files. The same final regression file was used in both trees.

Environment: Windows, CPython 3.12.10, SQLite 3.49.1, pytest 7.4.4, pytest-asyncio 0.23.8; isolated virtual environment. Dependencies were installed from the source project's declared constraints, not resolved from the Poetry lock; `requirements.txt` records the realized versions. Native Linux/macOS, server Qdrant, crashes/power loss, arbitrary upsert failures, and failed rollback are outside the executed scope.

At upstream dev `c72930316ae4473a431c698e2e675e3514428379`, `persistence.py` is byte-equivalent after newline decoding to the author's unpatched file. This is a source comparison, not an execution of the latest whole dev tree.

## Reproduce in a disposable checkout

Install the recorded dependency versions, and use a fresh checkout at the follow-up commit. Run from its root:

```shell
python -m pytest tests/test_local_busy_delete.py -q --tb=short --timeout=30
python -m pytest tests/test_local_busy_delete.py tests/test_local_persistence.py tests/test_in_memory.py -q --tb=short --timeout=30
python -m ruff check qdrant_client/local/persistence.py tests/test_local_busy_delete.py
python -m ruff format --check --line-length 99 qdrant_client/local/persistence.py tests/test_local_busy_delete.py
```

For the negative control, use a separate checkout at the author head and copy only `tests/test_local_busy_delete.py` from the follow-up. Do not apply the persistence change to that checkout. The new regression file then fails all eight cases at the live/reopened state comparison.

The public logs redact local owner/environment paths only. Raw logs and JUnit outputs remain in the owner's private working archive. No model service, production configuration, user database, financial account, or private conversation was used in these tests.

Youngseok supplies direction and corrections. Zero performed the analysis, implementation, and recorded executions using AI tools. External authors retain their original contributions. Publication of this candidate is not maintainer acceptance or an upstream merge.

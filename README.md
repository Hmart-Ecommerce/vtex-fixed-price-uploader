# Fixed price uploader

This tool checks a wide CSV of fixed prices before writing eligible rows to
configured accounts. It shows blocked rows, warnings, promotion endings, and
the number of prices that can be uploaded. Every warning group must be
acknowledged separately. Large batches also require the operator to type the
write count before upload.

After writing, the notebook reads every attempted row back and reports whether
it matched, mismatched, still held several live prices, was confirmed empty, or
could not be checked. Confirmed-empty rows must be written again. Unreadable
rows are open questions and require verification to be run again.

## Configuration

Create `accounts.json` in the Drive folder used by the notebook:

```json
{
  "accounts": {
    "R1": "acct_one",
    "R2": "acct_two"
  },
  "never_write": ["acct_master"],
  "trade_policy": "1"
}
```

The account map is the write allowlist. An account in `never_write` is never a
write target. Trade policy `1` is the only supported value.

The package contains no company identifiers. Keep real account names and other
environment-specific configuration only in `accounts.json` in Drive; do not add
them to the package, notebook, or repository.

## Run the notebook

1. Put `accounts.json` and the price CSV in the Drive folder configured by
   `FOLDER` in `notebook/price_upload.ipynb`.
2. Replace `<owner>` in the first cell's installation URL with the repository
   owner after the repository is available.
3. Open the notebook in Google Colab and run its three cells in order.
4. Choose the CSV, paste the login value, and select **Check this file**.
5. Review every group. Use check-only mode to stop before writes, or acknowledge
   each group and complete any batch-size confirmation to enable upload.
6. Read the complete verification summary after the write. Do not treat
   confirmed-empty or unreadable rows as successful writes.

The login value is held only in the password widget for the current session. It
is not written to the notebook, Drive files, logs, request URLs, or request
bodies.

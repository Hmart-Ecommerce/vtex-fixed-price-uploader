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
2. Open the notebook in Google Colab and run its three cells in order.
3. Choose the CSV, paste the login value, and select **Check this file**.
4. Review every group. Use check-only mode to stop before writes, or acknowledge
   each group and complete any batch-size confirmation to enable upload.
5. Read the complete verification summary after the write. Do not treat
   confirmed-empty or unreadable rows as successful writes.

## What the screen shows

The verdict block states the file total and reconciles the three numbers:
written plus blocked equals the rows in the file. The "rows need your
attention" count deliberately overlaps both, because blocked rows raise
warnings too, so it is labelled as an overlap and never added to the total.
The headline does not claim the file is ready to upload while any row is
blocked.

Every finding names the regions it stands for, and the full detail - rule ids,
one line per region - is offered as a CSV download next to the report.

The upload button restates the whole scope where the click happens: how many
prices, to how many accounts, and how many existing prices will be removed.
The typed confirmation is required when the writes and removals together
cross the threshold, not only when either one does on its own.

## After a write

The read-back runs whether the upload finished or stopped part-way. A run that
halts has usually already written rows, and the read-back is the only evidence
those rows landed. Rows that failed to write and rows skipped as already
written are always reported, including when the count is zero.

*Check what landed (no writing)* re-runs the read-back later without touching
production - use it to settle unreadable rows.

*Put the previous prices back* restores the prices that were in VTEX before the last
upload. It first states what will be put back and for how many pairs, and
writes nothing until you confirm. Pairs with no usable saved copy are left
exactly as they are.

If an earlier run was interrupted and its log is still open, the next upload
resumes it. Tick *Abandon the unfinished upload log* to start a new one
instead; rows already written stay written in VTEX.

Failures are shown as a sentence with a next step. No traceback reaches the
screen.

## Handling the login value

The login is held in a password widget for the session, and this package never
writes it to a log, a Drive file, a request URL, or a request body.

The package cannot make the same promise about the notebook file. Jupyter and
Colab can serialise widget state - including the value in the password field -
into the `.ipynb` metadata, and this notebook lives in Drive, which autosaves.

Treat it as a handling rule:

1. Do not save the notebook while the login field still holds a value.
2. Clear the field when you finish, then save.
3. If the notebook was saved with the field filled, treat that login as
   exposed and get a fresh one.

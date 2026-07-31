# bookscan-ops

Read-only status mirror for a local long-running download/verification
operation. A local agent POSTs JSON snapshots to `/api/push` (bearer token);
the dashboard at `/` (access token) displays them. No data originates here —
if pushes stop, the dashboard says the local machine is down. No book files,
keys, or paths beyond what the snapshot includes are ever stored here.

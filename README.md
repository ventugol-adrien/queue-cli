# Queue CLI

A small Bash job queue for running commands asynchronously, tracking their state, and retrying completed jobs. The repository also includes Python helpers for Gmail notifications and templates for delivery and workload commands.

## How it works

The queue stores each command as a file and moves it through four lanes:

```text
pending -> processing -> done
							|
							+-> status/<job-id>.jsonl (audit history)
```

`worker.sh` watches the pending directory, takes the next available job, runs the stored command, records the result, and moves the job to `done`. Jobs are ordered by timestamp-based filenames.

## Requirements

- Bash
- `jq`
- `inotifywait` from `inotify-tools`
- Python 3.14 or newer and `uv` for the optional Gmail helpers

On Debian or Ubuntu, install the shell dependencies with:

```bash
sudo apt install jq inotify-tools
```

The worker does not require a desktop notification session. Notifications are handled by workflow deliveries.

## Installation

From the repository root, make the shell scripts executable if necessary:

```bash
chmod +x queue.sh enqueue.sh status.sh restart.sh worker.sh
```

The scripts can be run from this directory. For convenient use from anywhere, create a command or symlink in a directory on your `PATH`.

`queue.sh` optionally sources:

```text
~/.config/bash/functions/queue-cli.sh
```

That file may define a `queue-cli` shell function used by the local setup. The dispatch wrapper supports `enqueue`, `status`, and `restart`. If that function is not part of your environment, use `enqueue.sh`, `status.sh`, and `restart.sh` directly, or add a compatible `queue-cli` function.

## Start the worker

Run the worker in a terminal or as a user service:

```bash
./worker.sh
```

It prints the directory it is watching and waits for new jobs. Leave it running before enqueueing work. The worker processes one job at a time.

## Enqueue a command

Using the wrapper:

```bash
./queue.sh enqueue <command...>
```

Or call the implementation directly:

```bash
./enqueue.sh <command...>
```

Examples:

```bash
./queue.sh enqueue echo "hello from the queue"
./queue.sh enqueue bash -lc 'sleep 5; printf "finished\n"'
```

The enqueue command prints the new job's initial JSON status record. Save the job ID from that output if you need to inspect or restart the job later.

Workflow filenames are resolved first as supplied, then relative to `WORKFLOWS_DIR`, and finally relative to `$HOME/.local/queue/workflows`. This applies to `./enqueue.sh -e t2i.yaml` and to a leading `.yaml` or `.yml` argument. Edit mode opens a copy in `$EDITOR` (default: `vim`) and queues it with `task`.

Commands are shell-escaped when written to disk and evaluated by the worker. Treat enqueued commands as trusted local code: the worker executes them with the permissions and environment of the user running it.

## Scaleway Commands

The Python package exposes these standalone lifecycle commands:

```bash
scw-start render-s.yaml --dry-run
scw-start render-s.yaml
scw-save --dry-run
scw-save
scw-stop --dry-run
scw-stop
```

`scw-start` accepts an instance definition path or a filename from the existing
template search paths, including `~/.config/queue/instances/*/`. It selects and
creates the instance using the same models as `task`, records its configuration,
and prints the server ID. The server stays running until explicitly stopped.

`scw-save [server-id]` stops the server and saves a reusable image without
terminating it. `scw-stop [server-id]` honors the recorded `save` setting, then
terminates the server and its volumes/IP and prints the estimated compute cost.
As with the workflow context, termination is attempted even if saving fails.

Create a reusable block volume separately from instance startup:

```bash
scw-volume create sd_t2i_illustrious.yaml
```

The recipe is loaded from the current directory, `~/.config/scw/volume_definitions/`,
`~/.config/queue/volume_definitions/`, or the bundled `templates/volume_definitions/`.
It defines the volume name, bucket, size, and one or more prefixes.
`scw-volume create`
provisions the configured volume worker, runs its `build-volume` command,
terminates the worker, and prints the volume ID and a ready-to-paste instance
snippet. Prefixes are synced from
`users/$USER/<prefix>`; each requested prefix must contain files. The worker
image needs `build-volume`, `scw`, `aws`, `pass`, `jq`, and passwordless sudo,
plus Scaleway credentials and read-only Object Storage access. The Object
Storage secret is read on the worker with
`pass show services/scaleway/access/secret_key`.

Instance definitions reference an existing volume and mountpoint only:

```yaml
volume:
	name: models-20260928
	mount_path: /home/adrien/.local/share/models
```

Use `id` instead of `name` when preferred. `scw-start` resolves and attaches
that already-formatted volume, then cloud-init mounts it at `mount_path` without
formatting it. `scw-stop` continues to delete the instance and everything
attached to it. Delete a standalone volume with either
`scw-volume delete --volume-name NAME --yes` or
`scw-volume delete --volume-id ID --yes`.

If provisioning fails after an operation is created, use the printed recovery
command to clean up partial resources:

```bash
scw-stop --operation-id OPERATION_ID
```

When no ID is given, save/stop print and select the oldest non-deleted record
from `~/.local/share/scaleway/instances.db`. Stopped-but-not-terminated servers
remain eligible. Use `--zone fr-par-2` to filter selection or specify an ID:

```bash
scw-save SERVER_ID --zone fr-par-2
scw-stop SERVER_ID --zone fr-par-2
```

Dry runs print lifecycle commands without changing resources or the database;
start still performs read-only catalog/image lookups. Volume dry runs do not
require S3 credentials or access the bucket. Save/stop restore the
recorded configuration without selecting a new type. Records from before
configuration persistence cannot be restored by these commands.

Reinstall the editable Python package to register new entry points:

```bash
uv pip install --python python-env/.venv/bin/python --no-deps -e ./python-env
```

The scripts are installed in `python-env/.venv/bin`; activate that environment
or put that directory on PATH.

## Check status

### Instance Accounting

Instance records are stored in `~/.local/share/<provider>/instances.db`
(`scaleway` for Scaleway). The `instances` table retains server and backup image
IDs, `created_at`, `stopped_at`, `terminated_at` (Unix seconds), `uptime_seconds`,
`hourly_price`, `currency`, and `run_cost`.

Uptime is measured locally from the start of a successful create command until
stop completes (when saving) or termination completes. Active records contain
the elapsed time at their last update, not a continuously refreshed counter.
Run cost is an estimate: `uptime_seconds * hourly_price / 3600`, using the
catalog rate captured at selection. It excludes storage, IP charges, taxes,
and provider billing adjustments. Missing historical timing or pricing stays
NULL rather than being reported as zero. Dry runs write no records.

On first use, if the new database does not exist, the old
`~/.local/state/<provider>/instances.db` is copied and its schema upgraded.
The original database is retained.

```bash
sqlite3 -header -column ~/.local/share/scaleway/instances.db \
	'SELECT server_id, uptime_seconds, run_cost, currency, deleted FROM instances;'
```

Show counts for each queue lane:

```bash
./queue.sh status
```

Show the complete JSON audit history for a job:

```bash
./queue.sh status <job-id>
```

A job's history normally includes `pending`, `running`, and either `success` or `failure`. Restarting a completed job adds a `restarted` record and sends the job back to `pending`.

## Restart a completed job

Only jobs currently in the `done` directory can be restarted:

```bash
./queue.sh restart <job-id>
```

The job is moved back to `pending` without creating a new job ID, so its existing audit history is preserved.

## Queue storage

By default, data is stored under:

```text
~/.local/share/queue/
├── pending/      # queued command files
├── processing/   # job currently being executed
├── done/         # completed command files
└── status/       # one JSONL audit log per job
```

The scripts use these environment variables:

| Variable | Used by | Default |
| --- | --- | --- |
| `QUEUE_BASE` | `status.sh`, `restart.sh` | `$HOME/.local/share/queue` |
| `PENDING_DIR` | `enqueue.sh`, `worker.sh` | `$HOME/.local/share/queue/pending` |
| `STATUS_DIR` | `enqueue.sh`, `worker.sh`, `restart.sh` | `$HOME/.local/share/queue/status` |
| `PROC_DIR` | `worker.sh` | `$HOME/.local/share/queue/processing` |
| `DONE_DIR` | `worker.sh` | `$HOME/.local/share/queue/done` |
| `WORKFLOWS_DIR` | `enqueue.sh` | `$HOME/.config/queue/workflows` |

For a separate queue instance, set all directory variables consistently before starting the worker and submitting jobs:

```bash
export QUEUE_BASE="$HOME/.local/share/queue-work"
export PENDING_DIR="$QUEUE_BASE/pending"
export STATUS_DIR="$QUEUE_BASE/status"
export PROC_DIR="$QUEUE_BASE/processing"
export DONE_DIR="$QUEUE_BASE/done"
```

## Templates and examples

The YAML files are reference templates for external delivery or job tooling. They are not parsed by the Bash queue scripts directly.

`delivery_template.yaml` describes a delivery type:

```yaml
type:
	email:
```

`job_template.yaml` describes a command job:

```yaml
type: command
location:
```

`examples/t2i.txt` documents a Stable Diffusion text-to-image command format:

```text
[MODEL] [HEIGHT] [ASPECT RATIO] [PROMPT]
illustrious 40 1024 1:1 "scenery,mountain,anime,masterpiece,best quality"
```

These files are starting points for commands or integrations that you enqueue yourself.

### Stable Diffusion notification defaults

[The desktop envelope](templates/envelopes/stable-diffusion_desktop.yaml.j2)
provides success/failure notification titles, tags, Markdown bodies, output
links, and attachments. Select it with
`template: "envelopes/stable-diffusion_desktop.yaml.j2"`, as shown in
[the T2I workflow](definitions/tasks/t2i.yaml).

Add a notification to `values.deliveries` to use these defaults:

```yaml
deliveries:
  - type: notification
    destination: laptop-adrien
```

Individual delivery fields such as `title`, `body`, `tags`, `output`, and
`attachment` still override the defaults. Empty strings disable the corresponding
text/link/attachment fields, and `tags: []` disables tags. Omitting deliveries or
setting `deliveries: []` sends no notifications.

The envelope preserves its result-dependent Jinja expressions until the task has
finished, when `definition`, `task`, and `result` are available. Successful jobs
attach the first result output (falling back to `task.out`); failed jobs send the
error body without an output link or attachment, so a missing output file cannot
prevent the failure notification.

## Gmail notification helpers

The `python-env` directory contains optional Python helpers:

- `python-env/src/auth.py` performs Google OAuth authorization.
- `python-env/src/send_email.py` sends a plain-text Gmail message and can attach a file.

Set up the Python environment:

```bash
cd python-env
uv sync
```

### Authorize Gmail

1. Create or select a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the Gmail API.
3. Configure the OAuth consent screen.
4. Create an OAuth client for a desktop application and download its JSON file.
5. Save the downloaded file as `~/.config/queue/credentials.json`.

Authorize the account once:

```bash
uv run python src/auth.py
```

The helper opens a browser, listens on `http://localhost:8080/oauth2callback`, and saves the token to `~/.config/queue/token.json`. The requested Gmail scope is `gmail.send`.

Do not commit or share `credentials.json` or `token.json`.

### Send an email

From the `python-env` directory:

```bash
uv run python src/send_email.py \
	--to recipient@example.com \
	--subject "Queue job complete" \
	--body "The queued task finished successfully."
```

Available options:

| Option | Required | Description |
| --- | --- | --- |
| `-t`, `--to` | Yes | Recipient email address |
| `-s`, `--subject` | Yes | Email subject |
| `-b`, `--body` | No | Plain-text body; defaults to `Task completed successfully.` |
| `-a`, `--attach` | No | Optional file attachment |

From the repository root, the same helper can be queued as a job:

```bash
./queue.sh enqueue \
	uv run --project python-env python python-env/src/send_email.py \
	--to recipient@example.com \
	--subject "Queue job complete" \
	--body "The queued task finished successfully."
```

The email is sent as the authorized Gmail account. Access tokens are refreshed automatically.

## Repository layout

```text
.
├── queue.sh                # command dispatcher
├── enqueue.sh              # create a pending job
├── worker.sh               # watch and execute jobs
├── status.sh               # queue dashboard and job history
├── restart.sh              # retry a completed job
├── delivery_template.yaml  # delivery reference template
├── job_template.yaml       # job reference template
├── examples/
│   └── t2i.txt             # text-to-image command example
└── python-env/
		├── pyproject.toml
		├── README.md
		└── src/
				├── auth.py
				└── send_email.py
```

The Python-specific setup is also documented in [python-env/README.md](python-env/README.md).

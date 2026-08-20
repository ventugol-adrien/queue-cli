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
- `notify-send` for worker progress notifications
- Python 3.14 or newer and `uv` for the optional Gmail helpers

On Debian or Ubuntu, install the shell dependencies with:

```bash
sudo apt install jq inotify-tools libnotify-bin
```

The worker needs a desktop notification session for progress notifications. The queue itself can still be used without notifications if `worker.sh` is adapted for a headless environment.

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

Commands are shell-escaped when written to disk and evaluated by the worker. Treat enqueued commands as trusted local code: the worker executes them with the permissions and environment of the user running it.

## Check status

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

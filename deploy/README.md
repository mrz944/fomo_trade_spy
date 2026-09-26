# FOMO deployment

Target: `cyberdev@100.111.109.33` (UID/GID 1000). Repository:
`git@github.com:mrz944/fomo_trade_spy.git`, branch `master`.

| Item | Location / name |
| --- | --- |
| Source | `/home/cyberdev/.local/share/fomo-trade-spy-source` |
| Service / container | `fomo-spy.service` / `fomo-spy` |
| Image | `localhost/fomo-trade-spy:0.1.0` plus immutable commit tag |
| Config | `~/.fomo-trade-spy/config.toml` (0600) |
| Credentials | `~/.fomo-trade-spy/env` (0600) |
| Database | `~/.fomo-trade-spy/data/spy.sqlite` |
| Runtime socket | `/run/user/1000/fomo-spy/daemon.sock` (0600) |
| Deployment receipt | `~/.fomo-trade-spy/deployment.json` |
| Quadlet | `~/.config/containers/systemd/fomo-spy.container` |

State and runtime directories are 0700. The service runs in paper mode with
$1,000 total capital, $25 entries, $250 exposure, ten positions and a $50 daily
loss halt. No wallet signing secrets are provisioned. The existing Hermes,
OmniRoute and Solana Spy services are independent.

## Launch and manage

On Linux and this Mac:

```sh
~/.local/bin/fomo-spy-tui
```

Linux launcher source: `deploy/fomo-spy-tui`. Mac launcher source:
`deploy/fomo-spy-tui-mac`. Install the appropriate file with mode 0755 as
`~/.local/bin/fomo-spy-tui`. The Mac uses `ssh -t` and `podman exec -it`;
only the existing SSH connection is needed. Closing the terminal leaves FOMO
running. Use Ctrl-Q to detach from any widget, or `q` outside the command input.
Type `pause` or `resume` into the command input and press Enter; `p` and `r`
also work outside the input. Pause stops entries, not known-inventory exits.

Run these management commands on Linux (or prefix with `ssh cyberdev@100.111.109.33`):

```sh
systemctl --user status fomo-spy.service
journalctl --user -u fomo-spy.service -n 100 --no-pager
systemctl --user restart fomo-spy.service
systemctl --user stop fomo-spy.service
systemctl --user start fomo-spy.service
podman exec fomo-spy fomo-spy status --socket /run/fomo-spy/daemon.sock
podman exec fomo-spy fomo-spy control pause --socket /run/fomo-spy/daemon.sock
podman exec fomo-spy fomo-spy control resume --socket /run/fomo-spy/daemon.sock
cat ~/.fomo-trade-spy/deployment.json
```

The generated Quadlet starts under `default.target`; user lingering is enabled.
To prevent future automatic starts, use `systemctl --user mask fomo-spy.service`
after stopping it. Use `unmask`, `daemon-reload`, and `start` to restore it.

## Provision a FOMO key

No FOMO key was available at initial deployment. The real paper daemon and TUI
start with discovery and streaming explicitly unavailable and zero candidates;
health checks only establish daemon liveness, not provider readiness. No demo
data substitutes for the missing feed.

Edit the environment file privately on Linux:

```sh
nano ~/.fomo-trade-spy/env
chmod 600 ~/.fomo-trade-spy/env
systemctl --user restart fomo-spy.service
```

Add a single `FOMO_API_KEY=your-key` line without `export`. Do not put the key
in Git, shell commands/history, the TOML config, or a wallet secret field.
The process loads credentials at startup. Inspect the TUI health tab after
restart. Even with a key, raw FOMO histories cannot yet establish automatic
eligibility; see [integration limitations](../docs/integrations.md).

## Build and initial installation

Clone `master` into the source directory above. Build from a clean checkout of
the exact published commit; set an OCI revision label and a commit-specific tag:

```sh
cd ~/.local/share/fomo-trade-spy-source
revision=$(git rev-parse HEAD)
podman build --label "org.opencontainers.image.revision=$revision" \
  -t "localhost/fomo-trade-spy:$revision" \
  -t localhost/fomo-trade-spy:0.1.0 -f Containerfile .
install -d -m 700 ~/.fomo-trade-spy ~/.fomo-trade-spy/data
install -d ~/.config/containers/systemd ~/.local/bin
# Initial install only: preserve existing config and credentials on updates.
install -m 600 deploy/config.container.example.toml ~/.fomo-trade-spy/config.toml
install -m 600 /dev/null ~/.fomo-trade-spy/env
install -m 644 deploy/fomo-spy.container ~/.config/containers/systemd/fomo-spy.container
install -m 755 deploy/fomo-spy-tui ~/.local/bin/fomo-spy-tui
systemctl --user daemon-reload
systemd-analyze --user verify fomo-spy.service
systemctl --user start fomo-spy.service
```

The environment file can remain empty. No Podman secret dependency is required.
Only FOMO is restarted during updates. Keep an older commit-tagged image for
rollback; use its exact image tag in the Quadlet and reload/restart FOMO.
Check database migration compatibility before rolling back code.

## Deployment verification

Run `ruff check src tests` and `pytest -q` on macOS and Linux. For Linux,
an ephemeral container based on the built image can install the locked dev
dependencies with `uv sync --frozen` and mount the checkout's tests read-only.
This does not alter the service image or persistent service state.

Validate the generated systemd unit, healthy container, 0700 directories, 0600
socket/config/env/database, read-only root, empty port mappings, and dropped
capabilities. Attach both TUI launchers, exercise pause/resume and detach; check
that the daemon still responds. Pause, restart only FOMO, verify pause persists,
then resume. Check SQLite integrity through a read-only connection and compare
other services' PIDs and start times before/after.

Record the full Git revision and Podman image ID in `deployment.json` after
verification. Compare local `master`, GitHub `master`, Linux `HEAD`, and the
running image's OCI revision label. Runtime deployment evidence is separate
from the historical [development verification](../docs/verification.md).
Deployment is not validation of unfinished discovery/history normalization,
general Relay signing, live bridging, historical indexing or funded live trades.

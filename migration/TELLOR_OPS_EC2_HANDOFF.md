# tellor-ops EC2 handoff

Snapshot for the next agent. Original inventory captured **2026-07-21 ~04:48 UTC** (`2026-07-21 ~00:48 America/New_York`) via AWS CLI + SSM Run Command.

**Audit note (2026-07-21 04:52–04:59 UTC):** the AWS control plane and host were re-read without changing them, and the local `autoTasks`, `CPI`, and `layer-daemons` worktrees were checked against this document. Runtime facts below remain point-in-time observations, not desired state. Secret values were not read during the audit. Do not treat values in chat history or backups as current; use the designated secret store after the source-of-truth problem below is resolved.

## Continuation checkpoint — 2026-07-21T18:09:28Z / 2026-07-21 14:09:28 EDT

> **Evidence boundary:** this is an additive, local Major-provenance checkpoint.
> No AWS API, SSM, host, service, timer, secret, or destination was read or
> changed. The original point-in-time inventory below is preserved and remains
> the last live audit snapshot; this checkpoint does not revalidate it.

### Authoritative local source checkpoint

| Repository | Exact local state | Verified source-interface evidence | Production-artifact state |
|---|---|---|---|
| Monitor / `autoTasks` | Source closure `b9dad69db6fbce29446180660bdc3fa5e052a075` on local `monitor-migration`; the handoff commit follows it locally. `.cursor/`, `.pi/`, and the dangerous legacy `migration/scripts/tellor-ops-setup.sh` remain untracked. That setup script is non-idempotent and must not be reused as a reconciler. `migration/scripts/AWS.code-workspace` is the only file from `migration/scripts/` added to version control. | 144/144 discovery tests and an overlapping 52/52 focused OCI/release/delivery/host set passed. Two clean-clone config-release builds 1.1 seconds apart were byte-identical (`c6d4f15e224ea0339a741bd23bbedbd5ab7728569f2846f04a07e64564c307fd`), and the platform safe extractor accepted 22 regular files and zero links. | Source interfaces only. No real Monitor OCI image was built or scanned, no registry digest or immutable S3 version was produced, no account-owned AMI was built, and no target-host compatibility or canary was run. |
| CPI | `955954703d2eaaad0019a85f845b6c4873e88eef` on local `main`, three commits ahead of `origin/main`; user-owned `big_mac_prices.csv` modification and one deleted historical run artifact remain unstaged. The compatible sibling-consumer edits are verified machine-local state, not a commit. | 45/45 relevant source-interface tests passed. The 11/11 contract set and 4/4 platform-interface set also passed as focused, overlapping reruns—not 60 additional tests. Producer-generated bundles passed the platform validator: exact `CA`, `FL`, `NY` at 3/3 usable with zero duplicates, and the exact canonical 50-state sequence at 50/50 usable with zero duplicates. After the sibling consumer fix, its focused suite passed 12/12 and complete CPI discovery passed 51/51. | Cross-repository source compatibility is verified on this machine, but the consumer fix remains uncommitted and must be reviewed and committed, published, or otherwise archived before it is durable. No CPI release or AMI was built, attested, or published; no production Chrome sandbox probe, live three-state canary, four failure/cleanup scenarios, or controlled 50-state run occurred. The live production gate remains at least 45/50 useful states. |
| Refprice / `layer-daemons` | `84700c0ed4470959d70cec445cd80ebc2218e98c` on `ref-prices`; tracked and non-ignored state is clean | The targeted six-package source command, changed-package vet, module-tidy check, and release-definition validation passed. The isolated source lane also passed `go test -count=1 ./...`. One authoritative-checkout full-suite run exited 1 only because unrelated `pricefeed/client.TestStop` called `require.NoError` from a goroutine after the test completed at `client_test.go:306–310`; all subsequently reported packages passed, and the unchanged full suite was not retried. | The local interface is reproducible, but no Refprice OCI image was built, scanned, or published, no registry digest was pinned, no AWS task or seven-run promotion was executed, and reporter submission remains deliberately disconnected. |
| Desired-state platform | `/Users/df/Documents/aws` at `c25c10eb55aeaa3ec28daec0c564e522b9cdb821` on `codex/tellor-ops-production-platform`; clean | The provenance refresh followed the verified platform checkpoint at `d503b471f42ab8771e2a45b5c1f040383c9eec68`; `npm run verify` passed 18 test files / 197 tests plus strict synthesis. All enable flags and activation gates in `config/prod.json` remain false or unset. | `DEPLOYMENT_MANIFEST.json` remains `NOT_DEPLOYED`; release/version/hash, AMI, promotion, change-set, postdeploy, and sizing evidence coordinates remain unset. Both dependency audits remain blocked. |

All four named closure commits are local-only: none is contained by any locally
known remote ref. CPI `main` tracks `origin/main` and is ahead by three;
`monitor-migration` and `ref-prices` have no configured upstream branch, and
the platform checkout has no configured remote or upstream. These results
establish local source interfaces at the named commits. CPI producer/consumer
compatibility is also verified, but the consumer side has no commit and is not
durable source closure. They do **not** establish production artifacts,
platform compatibility, live data quality, current AWS state, or authorization
to activate a workload.

**Superseding tracked-runbook correction:**
`migration/AWS_DOCKER_OPERATIONS.md` is already tracked at Monitor closure
`b9dad69db6fbce29446180660bdc3fa5e052a075`, is unchanged from that commit, and
has SHA-256
`c3a60ea1f6bb460832f2cc80a79601cd1351852c09e3974e6b9c8856cc4f742d`.
Do not copy or overwrite it from the host. This additive correction supersedes
the stale original-inventory statement below that the runbook is absent while
leaving that point-in-time text intact.

### Source-interface detail and residual boundaries

- **Monitor:** the committed `migration/oci/Dockerfile` is intentionally a thin
  wrapper over the digest-pinned prebuilt
  `openzeppelin/openzeppelin-monitor:v1.5.0` image. It adds the source-revision
  label, delivery-mode default, and numeric `65532:65532` user, but contains no
  `ADD`, `COPY`, or `RUN`; it is not a full committed OpenZeppelin source fork
  or a from-source Monitor build. The build command statically requires
  `--pull=false`, `--network=none`, and `linux/amd64`, and the config-only
  release producer is deterministic, but no actual OCI build, vulnerability
  scan, non-root/read-only runtime proof, SIGINT proof, or production digest
  exists.
- **CPI:** source now emits separate canonical state and location fields,
  selects the platform's exact three-state canary or all 50 states, requires a
  pinned `CHROMEDRIVER_BIN`, preserves the browser sandbox, and fails closed on
  invalid tax treatment and all-`N/A` results. The 3/3 and 50/50 validator
  results were offline fixtures with test-only reference inputs; they are not
  live Uber Eats, Chrome-sandbox, canary, monthly, or controlled-run evidence.
  The sibling `big-mac-data-python` consumer is now compatible in machine-local
  source: `state` is required immediately after `currency_code`; USA rows
  require an uppercase two-letter state, while non-USA rows require an empty
  state. Its focused tests passed 12/12 and complete CPI discovery passed 51/51.
  No consumer commit was made because `big_mac_index/` and `tests/` were already
  broad untracked directories and staging either risked unrelated user work.
  This is verified but uncommitted residual state; review and commit, publish,
  or otherwise archive that consumer source before treating the compatibility
  fix as durable.
- **Refprice:** `capture` and `compare` consume fixed environment contracts;
  live DEX collection has no implicit public-RPC fallback. Controlled inputs
  bind bucket, key, exact `versionId`, and SHA-256. Production S3-arrival inputs
  bind bucket/key/version and hash the exact fetched version because the event
  carries no SHA-256. Immutable outputs and receipts use `If-None-Match: *` and
  require non-null S3 version IDs. The source implements the platform
  production-capture receipt plus controlled capture/comparison task receipts
  and versioned capture, approved-BRRNY, and comparison payloads.
- **Refprice release evidence:** the source lane produced two byte-identical,
  static Linux/amd64 builds with Go `1.23.7`, `CGO_ENABLED=0`, an exact source
  label/tag, and a hashed CA bundle. After integration, one offline release
  readback bound HEAD `84700c0ed4470959d70cec445cd80ebc2218e98c`, local CA
  SHA-256 `9dae8d76e55cb08991f2b672d58999ea15560d910759c16b544f843bdffbb994`,
  and binary SHA-256
  `07587b60092a796071b874a1b8cc56fff7bd2ce274a0025ee27a3685c48dfe25`.
  This proves the local non-container release interface, not a built/scanned
  production image or a published artifact.

### Stable-CDK deployment blocker

The exact vulnerable production path remains
`aws-cdk-lib@2.261.0` → bundled `minimatch@10.2.5` → bundled
`brace-expansion@5.0.6`. [GHSA-3jxr-9vmj-r5cp](https://github.com/advisories/GHSA-3jxr-9vmj-r5cp)
affects versions before `5.0.7`. The existing top-level override, a tested
CDK-scoped override, and `npm audit fix` cannot replace the copy bundled inside
the released CDK package. At the completed dependency review, official CDK
mainline resolved `5.0.7`, but the latest stable npm package was still
`2.261.0`; therefore no unreleased fork, audit suppression, or speculative
lockfile edit was retained.

The first machine-checkable unblock condition is a newer **stable**
`aws-cdk-lib` that supports Node 22, installs no affected nested copy, preserves
the exact lockfile, passes the complete verifier, and makes both audits exit
zero:

```bash
candidate=$(npm view aws-cdk-lib@latest version)
test "$candidate" != "2.261.0"
npm view "aws-cdk-lib@$candidate" engines --json
npm install --save-exact "aws-cdk-lib@$candidate"
npm ci
npm ls aws-cdk-lib brace-expansion --all
npm run verify
npm audit --omit=dev
npm audit
```

Do not prepare a production change set until that entire condition passes and
the resulting dependency change receives review.

### Identity, approval, and live-state prerequisites

- The committed approver file is still a comment-only placeholder and
  `config/deployment-approvers.sha256` is still 64 zeroes. A real reviewed
  Ed25519 public-key allowlist must be committed, its SHA-256 must be delivered
  to the root operator through an independent authenticated channel, and the
  private signing key must remain outside the repository.
- Local AWS CLI readback remains `2.36.2`. The only configured session was
  reported as expired account root; this checkpoint made no STS or other AWS
  call. The one-time root bootstrap requires explicit user confirmation, an
  MFA-protected temporary `aws login` session, the separately signed 30-minute
  bootstrap envelope, live proof of root MFA and zero root access/signing keys,
  and the independently delivered trust-anchor hash. Root is not a normal
  deployment caller.
- Foundation must then create the sole human login `tellor-codex`; enroll MFA,
  prove browser `aws login`, prove assumption of the reviewed Observer,
  Operator, and Deployer roles, and prove zero access keys, groups, other IAM
  users, or direct workload permissions. `tellor-operator` remains only a
  non-login Linux service identity.
- After identity bootstrap, capture a fresh Observer inventory before relying
  on any account, instance, IAM, SSM, alarm, backup, secret metadata, or service
  fact below. The last live inventory remains the original timestamped audit.

### Ordered next safe checkpoint

1. Consume the first compatible stable CDK release and make both audits zero;
   keep all workload enable flags false.
2. Configure, review, commit, and independently distribute the SSH approver
   trust anchor; prepare and separately sign the exact bootstrap envelope.
3. Obtain explicit confirmation for the one-time MFA-backed root `aws login`,
   execute only the signed bootstrap prerequisite, deploy Foundation through
   the documented initial exception, enroll `tellor-codex`, prove its three
   role assumptions, publish the immutable bootstrap receipt, and return root
   to recovery-only custody. The temporary bootstrap stack needs its own later
   signed retirement envelope.
4. Capture fresh Observer inventory. Separately approve and capture a completed
   encrypted rollback snapshot; harden the legacy instance profile and prove
   DHMC/Session Manager recovery before Runtime preparation or canonical secret
   creation. Rotate the exposed webhook, establish one authoritative record per
   secret, and prove both notification routes without exposing values.
5. Review and durably commit, publish, or otherwise archive the verified
   machine-local CPI consumer fix. Then build, scan, attest, immutably publish,
   and read back exact versions/hashes
   for the Monitor config release and real OCI image, Monitor account-owned AMI,
   CPI release and account-owned AMI, and Refprice Linux/amd64 image. Resolve
   both AMI build locks and every unset artifact coordinate. Decide and review
   whether the Monitor thin wrapper is sufficient or a full OpenZeppelin source
   fork is required before assigning a production source claim.
6. Deploy Foundation and Runtime with every workload disabled through separately
   reviewed, signed exact change sets; reproduce postdeploy effective controls,
   backup/rollback coordinates, alarms, audit, logging, patching, and recovery.
7. Pass production-boundary evidence: Monitor notification-disabled replay,
   alert, checkpoint, restore, SIGINT, and forced singleton replacement; CPI
   live sandbox, exact three-state canary, all failure/abort cleanup fixtures,
   and controlled 50-state run with at least 45 useful values; Refprice exact
   version-binding proof and seven controlled runs with at least 30% CPU/memory
   headroom. Reporter submission remains out of scope.
8. Collect and immutably publish 14 representative days of one-minute Monitor
   baseline telemetry with complete handler-delivery coverage, daily controlled
   canaries, and workload/process/memory/disk/inode evidence. Only after the
   baseline decision passes may an exact-next-launch-template `t3a.medium`
   trial run for seven days; select the final shape from the reproduced
   baseline/trial evidence. Capture a real or controlled CPI peak separately so
   a monthly browser spike is not hidden by Monitor-only telemetry.
9. Activate through separate diffs only after every applicable gate is true:
   cut over Monitor with rollback proof and retain the stopped legacy host for
   seven days; enable CPI and Refprice independently after their evidence; then
   close only when all immutable coordinates, approvals, live postdeploy
   readbacks, retention results, and rollback paths are recorded.

### Major provenance

- **Source map:** the exact four HEADs in the table; `migration/oci/Dockerfile`,
  `migration/AWS_DOCKER_OPERATIONS.md`,
  `migration/ops/build_monitor_image.py`,
  `migration/ops/monitor_platform_release.py`, and their Monitor delivery/release
  tests; the untracked `migration/scripts/tellor-ops-setup.sh` was read only to
  confirm its overwrite/enable behavior, while `AWS.code-workspace` is the sole
  tracked file from that directory; CPI `README.md`, `main.py`,
  `manual_scrape/countries/usa.py`,
  `scripts/run_big_mac_pipeline.py`, and `tests/test_{platform_interface,
  handoff_quality_gates,scheduled_pipeline,selenium_reliability,cohort}.py`;
  sibling `big-mac-data-python/big_mac_index/pipeline.py` and consumer tests,
  which remain in broad untracked directories rather than a named commit;
  Refprice `Makefile`, `cmd/refprice-prototype/{main,production,storage_s3}.go`,
  their tests, `reference_price/container/`, schemas, and `README.md`; platform
  `config/prod.json`, `config/deployment-approvers.*`, `WORKLOAD_CONTRACT.md`,
  `DEPLOYMENT_MANIFEST.json`, `docs/{BOOTSTRAP_DECISION,
  EXTERNAL_SOURCE_GATES,OPERATING_MODEL,SECURITY_EXCEPTIONS}.md`, rollout and
  deployment runbooks, Refprice/CPI validators and schemas, `package*.json`, and
  `verification/local-d503b471f42ab8771e2a45b5c1f040383c9eec68.json`; completed
  source-lane and authoritative-integration test receipts; and the advisory
  linked above.
- **Changed claims:** replaces the stale CPI 38-test/source-closure statement
  with exact 45-test and 3/50 validator evidence; advances all source commits;
  records the local-only publication boundary, CPI consumer compatibility
  verification and its uncommitted durability boundary,
  tracked Docker-operations runbook correction, Monitor thin-wrapper limit,
  dangerous untracked setup-script boundary, exact Refprice S3/version/receipt and
  Linux/amd64 interfaces, the unrelated integration-suite failure, the exact
  stable-CDK unblock condition, identity/approval preconditions, and the full
  remaining artifact, live-evidence, activation, and sizing sequence. No
  original live inventory claim is changed.
- **Assumptions:** the completed lane receipts correspond to the exact commit
  trees read back here; a future stable CDK package must be re-evaluated rather
  than inferred from mainline; current AWS and host state may differ from the
  04:52–04:59 UTC audit.
- **Validation:** local commit/status/source readback; exact source-test receipt
  readback; Markdown structure/readback, local-link, and source-map checks;
  full runbook SHA-256 and Git-tracking readback; local remote-ref containment
  and upstream checks; the sibling consumer's 12/12 focused pass and CPI's
  51/51 complete discovery pass after the compatibility fix (the first
  unprivileged discovery attempt passed 49 tests and was blocked only when two
  notebook cases attempted a local Jupyter kernel socket); relevant
  handoff/provenance tests; 144-test offline Monitor discovery; staged
  secret-pattern scan; and `git diff --check`. No AWS, network, browser, host,
  Discord, secret, build, scan, publish, or deployment validation was attempted
  by this checkpoint.
- **Residual risks:** the platform's external handoff-byte receipt at
  `c25c10e` necessarily describes the pre-finalization bytes and must be
  refreshed in a later platform-only provenance commit. The verified CPI
  consumer fix is machine-local and uncommitted; it must be reviewed and
  committed, published, or otherwise archived. All stable-dependency,
  trust-anchor, root-bootstrap, live-state, backup, legacy-profile/DHMC,
  secret-rotation, artifact/AMI, scan, canary, controlled-run, sizing,
  deployment, activation, retention, rollback, and durable-evidence gates
  remain open.

## TL;DR (revalidated point-in-time state)

| Item | State |
|------|--------|
| Instance | `i-00b865643d4023484` (`tellor-ops`), **running**, `t3a.large`, `us-east-2a` |
| OpenZeppelin Monitor | **OFF**: systemd `disabled` + `inactive`; container `migration-monitor-1` **Exited (137)** after SIGTERM did not finish within Docker's 10-second stop grace period and Docker sent SIGKILL; `OOMKilled=false` |
| Monitor watchdog | Timer **still enabled and active**; it fails and logs an alert every ~5 min while the monitor is intentionally off |
| Refprice daily | Timer **enabled** (14:50 America/New_York), but this is a prototype/manual comparison job and its last logged run failed with `context canceled` |
| CPI Big Mac | Timer **enabled** (16:00 America/New_York); it no-ops except on the 3rd Thursday and currently runs the USA scraper only |
| Access | Intended interactive admin path is SSM. There is no key pair and SG ingress is empty, but the public host runs `sshd`; the SG is the current inbound barrier, not an absent SSH service |
| Discord webhooks | Production monitor `.env` webhooks were rotated **2026-07-21 ~03:17 UTC** to a new Discord webhook (channel label `aws-defender`); full URL was pasted in operator chat — **rotate again if still that URL** |

**Intentional operator action (this session):** monitor was stopped and disabled on request. Restart is not automatic after reboot until someone re-enables the unit.

---

## AWS identity and connectivity

| Field | Value |
|-------|--------|
| Account | `075483720677` |
| Region | `us-east-2` |
| Instance ID | `i-00b865643d4023484` |
| Name / Project tags | `tellor-ops` |
| Type | `t3a.large` (x86_64) |
| AZ / subnet / VPC | `us-east-2a` / `subnet-03aa7de553c221216` / `vpc-0c8b7f62538e50596` |
| Private IP | `172.31.14.151` |
| Public IP | `3.16.207.22` (not an Elastic IP) |
| Launch time | `2026-07-20T21:20:19Z` |
| AMI | `ami-03499a87bbb39a09a` — Amazon Linux 2023 (`al2023-ami-2023.12.20260710.0-kernel-6.1-x86_64`) |
| Hostname | `ip-172-31-14-151.us-east-2.compute.internal` |
| Key pair | **none** (`KeyName: null`) |
| SSM | Online, agent `3.3.4624.0`; SSM reports `IsLatestVersion=false` |
| Instance profile | `TellorOpsEC2Role` |
| Metadata | IMDSv2 required (`HttpTokens=required`), endpoint enabled, hop limit 2 |
| EC2 monitoring | Basic/5-minute (`Monitoring=disabled`); no CloudWatch Agent metrics |
| CPU credits | `unlimited` |
| Root EBS | 40-GiB `gp3`, 3,000 IOPS / 125 MiB/s, encrypted with the AWS-managed EBS KMS key, `DeleteOnTermination=true` |

### Security group `tellor-ops-sg` (`sg-0cf58c65ac74501af`)

- **Inbound:** empty (no SSH/22, no public app ports).
- **Outbound:** TCP 80/443 and TCP+UDP 53 to `0.0.0.0/0`.

The instance is in the default public subnet, has an Internet Gateway route and a public non-EIP IPv4, and has no VPC endpoints. SSM is outbound-initiated. Host readback found `sshd` listening on IPv4 and IPv6 port 22; the empty SG ingress currently blocks it. A future SG mistake would expose SSH, so disable `sshd` if SSM-only administration is the desired control rather than relying on one network rule. Before doing that, test a break-glass host-recovery path such as an authorized EC2 Serial Console workflow or stop/detach/repair-volume procedure.

### IAM on the instance role

Attached:

- `AmazonSSMManagedInstanceCore`

Inline:

| Policy | Purpose |
|--------|---------|
| `TellorOpsSSMParametersRead` | `ssm:GetParameter(s)` on `arn:aws:ssm:us-east-2:075483720677:parameter/tellor-ops/*` |
| `TellorOpsSSMParams` | `ssm:GetParameter(s)` on `arn:aws:ssm:us-east-2:075483720677:parameter/tellor/ops/*` |
| `TellorOpsStagingS3Read` | `s3:GetObject` + `ListBucket` on `tellor-ops-staging-075483720677-use2` |

The instance role **cannot** `ssm:StartSession` (expected); the human principal starts a session and the managed node only opens the SSM channels. Operators recently used account root via `aws login`.

**Important IAM correction:** the prefix-scoped inline Parameter Store policies do not constrain effective access. The attached `AmazonSSMManagedInstanceCore` v2 policy also grants `ssm:GetParameter` and `ssm:GetParameters` on `Resource: "*"`; IAM allows are additive. The instance can therefore request named parameters outside the two Tellor prefixes, subject to KMS-key restrictions. Separate SSM host-management permissions from the application role (for example, Default Host Management Configuration or a reviewed custom managed-node policy), then keep only explicit workload prefixes on the application role.

A metadata-only check found three `SecureString` records: one watchdog value and two redundant migration-env records under the two prefixes. The checked-in code does not fetch them automatically, while the host also has materialized `.env` files. Choose one authoritative path and one secret per parameter, record owner/rotation date, and remove stale duplicates/backups only after rotation and rollback planning.

### How to connect

Local prerequisites:

1. Valid AWS creds (`aws login` if expired). CLI ≥ 2.32 for `aws login`.
2. Session Manager plugin on PATH. On this Mac it was installed user-local at `~/.local/bin/session-manager-plugin` (system pkg install needed sudo).

```bash
export PATH="$HOME/.local/bin:$PATH"
aws ssm start-session --target i-00b865643d4023484 --region us-east-2
```

Non-interactive work: `aws ssm send-command --document-name AWS-RunShellScript ...` (prefer JSON `file://` params; avoid embedding secrets in chat).

JSON `file://` parameters reduce quoting mistakes and local shell-history exposure; they are **not** a secret-transport boundary. Send parameter names and fetch values on-host through the instance role rather than putting values in Run Command parameters. The current CLI identity is account root. Replace it with a scoped, temporary-credential operator role before routine work; reserve root for root-only recovery. No regional `SSM-SessionManagerRunShell` preferences document exists, so session transcript delivery to CloudWatch Logs/S3, session KMS preferences, idle timeout, and Run As are not configured.

You are already on the host at `sh-5.2$` once the session starts. Do **not** re-run `aws ssm start-session` from inside the instance.

---

## Host layout

```
/opt/tellor/
  autoTasks/          # Defender legacy + OpenZeppelin Monitor migration (NOT a git checkout)
  CPI/                # Big Mac CPI scraper (NOT a git checkout); .venv + venv present
  layer-daemons/      # Go daemons + refprice prototype sources (NOT a git checkout)
  bin/
    cpi-bigmac-run.sh
    cpi-monthly.sh       # present on host, but not generated/referenced by current local source; treat as legacy/orphan
    monitor-watchdog.sh
    refprice-prototype   # compiled binary used by systemd

/etc/tellor/
  refprice.env              # mode 600
  monitor-watchdog.env      # mode 600

/var/lib/tellor/
  reference-price/          # brrny.json, live dumps, daily outputs

/var/log/tellor/
  monitor-watchdog.log
  refprice-daily.log
  cpi/
  userdata-complete
  refprice-live-*.log
```

Host bootstrap template used to create the units: local repo `autoTasks/migration/scripts/tellor-ops-setup.sh` (also conceptually applied on the host under `/opt/tellor/...`).

**Do not rerun it as a reconciler.** The untracked script is not idempotent: it unconditionally overwrites `/etc/tellor/refprice.env` and `/etc/tellor/monitor-watchdog.env` with empty placeholders, enables all three timers before validating dependencies, and may start the Monitor whenever its `.env` contains an RPC URL. Back up and diff installed files and units before reusing any part of it.

On-host ops doc present (may be ahead of local workspace): `/opt/tellor/autoTasks/migration/AWS_DOCKER_OPERATIONS.md`.

That runbook is absent from the current local worktree even though local README/MIGRATION links point to it. Copy the reviewed host version into version control and record its checksum before treating either copy as authoritative.

Local workspace docs:

- `migration/MIGRATION.md`
- `README.md`
- this handoff

**Important:** `/opt/tellor/{autoTasks,CPI,layer-daemons}` are **copied trees without `.git`**. Local developer clones under `~/projects/dev/` are the editable sources; deploying means copying/rsync/S3 staging, not `git pull` on the box unless someone re-gitifies the trees. At audit time all three worktrees were dirty, the setup/handoff files were untracked, and the entire refprice implementation was untracked. The deployed binary and copied trees therefore cannot be reproduced from Git as-is.

Staging bucket the role can read: `s3://tellor-ops-staging-075483720677-use2`. It has SSE-S3 and public-access blocks, but versioning is disabled and the role can read the whole bucket rather than a release prefix.

---

## Workloads

### 1) OpenZeppelin Monitor (Tellor Discord alerts) — **currently STOPPED**

| Piece | Detail |
|-------|--------|
| Compose project | `/opt/tellor/autoTasks/migration` |
| Compose file | `docker-compose.yaml` |
| Image | `openzeppelin/openzeppelin-monitor:v1.5.0@sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635` |
| Container name | `migration-monitor-1` |
| Systemd | `openzeppelin-monitor.service` (**disabled**, **inactive**) |
| Secrets | `/opt/tellor/autoTasks/migration/.env` (mode 600) |
| Config mount | `./config` → `/app/config` (ro) |
| Checkpoints | `./data` (e.g. `ethereum_mainnet_last_block.txt`) |
| Logs | `./logs` (+ Docker logs) |
| Metrics profile | Prometheus/Grafana defined but **profile `metrics` not in use** by default |

The final Monitor image does not declare a non-root user, and Compose has no CPU or memory limits. `no-new-privileges` and the content-pinned digest are useful controls, but they are not a complete container sandbox.

Systemd unit behavior:

- `Type=oneshot` + `RemainAfterExit=yes`
- `ExecStart` / `ExecReload`: `docker compose up -d --force-recreate monitor`
- `ExecStop`: `docker compose stop monitor`
- Requires `.env` to exist
- No `User=` is set, so the host-side systemd command runs as root

**Reload rule:** `.env` changes require container recreation. Both `systemctl reload openzeppelin-monitor` and `systemctl restart openzeppelin-monitor` suffice because `ExecReload` and `ExecStart` use `docker compose up -d --force-recreate`. A plain `docker compose restart` does **not** reread changed environment values. Mounted config/script changes require a process restart; force-recreate remains the clearest uniform procedure.

#### Discord / env keys (names only)

Present in migration `.env` at inventory time:

- `RPC_ETHEREUM_MAINNET` — the only RPC consumed by the active Monitor network config
- `RPC_SEPOLIA`, `RPC_POLYGON`, `RPC_OPTIMISM`, `RPC_GNOSIS`, `RPC_CHIADO` — EVMCall target RPCs; Sepolia also has a network file, but no active monitor watches it
- BlockPI, dRPC, and Alchemy entries, if present, are **not** operational fallbacks in the checked-in config
- `DISCORD_WEBHOOK_URL` — used by all checked-in production handlers
- `CUSTOM_DISCORD_WEBHOOK_URL` — used only by generated quickstart monitors, not the 12 listed below
- `UPDATE_VALIDATOR_SET_DISCORD_WEBHOOK_URL` — present but currently unused; remove it or wire it explicitly
- `CMC_PRO_API_KEY`, `EXCHANGERATE_API_KEY`; `COINCAP_API_KEY` is supported by code but was not present in the audited env key list

Backup before last webhook edit: `.env.bak.20260721T031742Z` (pre-rotation values).

**2026-07-21 operator change:** both `DISCORD_WEBHOOK_URL` and `CUSTOM_DISCORD_WEBHOOK_URL` were set to the same new Discord incoming webhook (Discord metadata name `aws-defender`). That URL appeared in operator chat; treat as compromised until rotated.

Changing `CUSTOM_DISCORD_WEBHOOK_URL` did not change delivery for the 12 checked-in production monitors. The `.env.bak.*` name is not covered by the repository's current `.gitignore`, so never copy that backup into the Git worktree; retire it after the relevant webhook values are rotated and recovery is no longer needed.

#### Monitors on disk (`config/monitors`; all declare `ethereum_mainnet` only)

| File | Name | paused |
|------|------|--------|
| `address_updates.json` | Tellor Address Updates | false |
| `deposit_to_layer.json` | Tellor Deposit To Layer | false |
| `dvm.json` | Tellor DVM Price Deviation | false |
| `evm_call.json` | Tellor EVMCall Validation | false |
| `guardian_reset_validator_set.json` | Guardian Reset Validator Set Calls | false |
| `smoke_usdc.json` | Smoke Test USDC Transfer | **true** |
| `staking.json` | Tellor Staking | false |
| `tellorflex_data_report.json` | TellorFlex Data Report | false |
| `token_bridge.json` | Tellor Token Bridge | false |
| `update_oracle_data.json` | Update Oracle Data Calls | false |
| `update_validator_set.json` | Update Validator Set Calls | false |
| `withdraw_from_layer.json` | Tellor Withdraw From Layer | false |

Policy from migration docs: 11 active + paused smoke; mainnet-only. Triggers under `config/triggers/` include script handlers in `config/triggers/scripts/` (`tellor_lib.py`, etc.).

#### Start / stop / reload

```bash
# On host
cd /opt/tellor/autoTasks/migration

# Start (and persist across reboot)
sudo systemctl enable --now openzeppelin-monitor.service

# Stop (and prevent reboot start) — current desired state as of this handoff
sudo systemctl stop openzeppelin-monitor.service
sudo systemctl disable openzeppelin-monitor.service

# After .env or config/script changes
sudo systemctl reload openzeppelin-monitor.service
# or:
sudo docker compose up -d --force-recreate monitor
```

Use `sudo` for `systemctl`/Docker commands unless the session is explicitly running as root or the operator has the required delegated group permissions. The last operator stop was not an OOM: Docker sent SIGTERM, waited 10 seconds, then SIGKILL. Investigate graceful shutdown and an appropriate `stop_grace_period` before treating future exit 137 events as clean stops.

Safe historical replay (no Discord): see `MIGRATION.md` — force empty webhook envs.

### 2) Monitor watchdog — **still ON**

| Piece | Detail |
|-------|--------|
| Timer | `monitor-watchdog.timer` enabled, every 5 minutes (+ 2 min after boot) |
| Service | `monitor-watchdog.service` |
| Script | `/opt/tellor/bin/monitor-watchdog.sh` |
| Env | `/etc/tellor/monitor-watchdog.env` (`MONITOR_WATCHDOG_WEBHOOK` / `DISCORD_WEBHOOK`) |
| Log | `/var/log/tellor/monitor-watchdog.log` |

Behavior: the current runtime defines no container healthcheck, so this is only a container-running probe. It cannot detect stalled block progress, bad RPC/config, handler errors, or failed Discord delivery. On every failed check it logs and, if configured, posts again without deduplication or a recovery notification. The generated payload also contains the literal text `$(hostname -s)` instead of expanding the hostname.

**While monitor is intentionally stopped, this timer will keep failing the health check.** Inventory showed:

- Early: `No webhook configured; alert suppressed`
- Later (`2026-07-21T00:48:37-04:00` and `00:53:42-04:00`): ALERT without a suppression line. This is consistent with a configured webhook but does not prove delivery because secret values and Discord were not queried.

If keeping monitor off, strongly consider:

```bash
sudo systemctl stop monitor-watchdog.timer
sudo systemctl disable monitor-watchdog.timer
```

### 3) Reference-price prototype capture — timer ON, not production reporting

| Piece | Detail |
|-------|--------|
| Timer | `refprice-daily.timer` — `*-*-* 14:50:00 America/New_York` |
| Service | `refprice-daily.service` |
| Binary | `/opt/tellor/bin/refprice-prototype` |
| Env | `/etc/tellor/refprice.env` → `REFERENCE_PRICE_ETH_RPC_URL` |
| BRRNY | `/var/lib/tellor/reference-price/brrny.json` |
| Output | `/var/lib/tellor/reference-price/reference-price-daily.json` |
| Log | `/var/log/tellor/refprice-daily.log` |

Last log snippet at inventory: scheduled window `2026-07-20T19:00:00Z`–`20:00:00Z`, then `collect live dataset: context canceled`. There are large live JSON dumps under `/var/lib/tellor/reference-price/` from Jul 20.

This is explicitly prototype/testnet-grade. It is not wired to the reporter daemon and submits no Tellor report. A successful canonical comparison requires an operator to populate `brrny.json` with matching BTC and ETH New York rates after the 3–4pm collection window. The program writes an unbenchmarked recovery capture before waiting up to 15 minutes for that file, so output existence or mtime does not prove service success.

The observed July 20 window predates this instance's stated launch (`21:20Z`) and could not be selected by the current scheduler after launch. Treat the log as copied/stale or evidence of deployed-binary/source drift until journal timestamps, file mtimes, invocation arguments, and the deployed binary SHA-256 are reconciled.

Source code for the prototype lives under `/opt/tellor/layer-daemons` (and local `~/projects/dev/layer-daemons`), but the local implementation is untracked and the deployed binary has no recorded build checksum. The service also has no `User=`, so it runs as root.

### 4) Big Mac CPI — timer ON (conditional run)

| Piece | Detail |
|-------|--------|
| Timer | `cpi-bigmac.timer` — daily `16:00:00 America/New_York` |
| Service | `cpi-bigmac.service` |
| Wrapper | `/opt/tellor/bin/cpi-bigmac-run.sh` (exits unless **3rd Thursday** NY) |
| App | `/opt/tellor/CPI` with `.venv` Python; Chrome at `/usr/bin/google-chrome` (v150) |
| Runtime config | The scheduled path does not read CPI `.env` or require API secrets. The wrapper sets `CHROME_BIN`; optional `CPI_US_STATES` / `CPI_HEADED` are not wired through an `EnvironmentFile` |
| Logs | `/var/log/tellor/cpi/cpi.log` (empty at inventory) plus detailed `/var/log/tellor/cpi/cpi-*.log` on an actual scrape |
| Data | `/opt/tellor/CPI/big_mac_prices.csv` (updated Jul 20) |

Current `main.py` runs the USA scraper only. Per-state failures are converted to `N/A` and do not fail the process, so systemd can report success even when no useful prices were collected. Validate coverage, non-`N/A` count, freshness, duplicates, and duration after each run. There are no discovered CPI tests.

**Security blocker:** this systemd service defaults to root, while the Selenium code browses public web content with Chrome's `--no-sandbox` flag. Move it to a dedicated unprivileged service account, restore browser sandboxing, and add systemd write-path/resource restrictions before unattended production use.

`Persistent=true` does not provide full backfill: a catch-up after the third Thursday has passed no-ops because the wrapper tests the current date. Refprice catch-up at or after its window schedules the next complete day, but the unit's two-hour timeout can kill that long wait. Define explicit missed-run/backfill behavior.

---

## Recent operator session (context for continuity)

Chronology relevant to this handoff (local laptop + SSM, account `075483720677`):

1. AWS CLI session expired → `aws login` as root.
2. Connected via SSM to `tellor-ops` (Session Manager plugin installed under `~/.local/bin` on the Mac).
3. Switched Discord webhooks in `/opt/tellor/autoTasks/migration/.env` for `DISCORD_WEBHOOK_URL` + `CUSTOM_DISCORD_WEBHOOK_URL`, backed up prior `.env`, `systemctl reload openzeppelin-monitor`.
4. Later **stopped and disabled** `openzeppelin-monitor.service` on request; the container did not exit within the 10-second grace period and was SIGKILLed (`ExitCode=137`, `OOMKilled=false`).
5. Watchdog timer was **not** disabled.

---

## Host resources (inventory)

- Root disk: 40G, ~15% used (~5.8G)
- RAM: 7.7 GiB, largely idle with monitor off
- Load ~0
- No swap
- Listening ports: Monitor metrics 8081 is not host-published by default, but host `sshd` listens on port 22 and is blocked only by SG ingress

---

## Common agent tasks (recipes)

### Change Discord webhook (monitor)

1. Current/manual path: edit `/opt/tellor/autoTasks/migration/.env` (mode 600). Target path: make one Parameter Store/Secrets Manager record per secret authoritative and materialize it through a reviewed deployment step.
2. Update `DISCORD_WEBHOOK_URL` for the listed production monitors; `CUSTOM_DISCORD_WEBHOOK_URL` affects generated quickstart monitors only.
3. Recreate: `sudo systemctl reload openzeppelin-monitor` (only if it is meant to be running).
4. Never commit `.env` or `.env.bak.*`. Rotate any webhook pasted into chat and retire stale host backups after rollback needs expire.

### Turn monitor back on

```bash
sudo systemctl enable --now openzeppelin-monitor.service
sudo docker compose -f /opt/tellor/autoTasks/migration/docker-compose.yaml ps
```

Also re-enable/confirm watchdog if you want downtime alerts.

### Turn monitor off cleanly (recommended pair)

```bash
sudo systemctl stop openzeppelin-monitor.service
sudo systemctl disable openzeppelin-monitor.service
sudo systemctl stop monitor-watchdog.timer
sudo systemctl disable monitor-watchdog.timer
```

### Deploy code updates

The current deployment is not reproducible from Git alone. Before the next deployment, create an immutable artifact and manifest recording each repository commit, any intentional dirty-patch checksum, source archive SHA-256, refprice binary SHA-256/build metadata, Monitor image digest, destination paths, deployment time, and rollback artifact. Enable staging-bucket versioning or use immutable release keys. The current code does not fetch Parameter Store values or S3 artifacts automatically.

### Validate Monitor config (when running or via compose run)

See `migration/MIGRATION.md` and, until it is restored to Git, the on-host `AWS_DOCKER_OPERATIONS.md`: `python3 quickstart.py check`, webhooks-off replay patterns, etc. A secret-free local audit passed 54/54 handler/config checks and 39/39 quickstart tests; this verifies the local working tree, not the unmanifested host copy.

---

## Audited risks and missing controls

1. **Intentional-off mismatch:** the Monitor is intentionally off while its watchdog remains enabled, failed, and noisy.
2. **Compromised webhook:** rotate the webhook pasted into chat; then eliminate stale materialized/backup copies.
3. **Root and browser sandboxing:** all generated systemd services run as root, and CPI launches Chrome with `--no-sandbox` against public sites. This is the highest-priority guest hardening defect.
4. **Container credential reachability:** IMDSv2 is correctly required, but hop limit 2 is specifically sufficient for a typical bridge-container hop and may make instance-role credentials reachable from the Monitor network; this path was not live-probed. The Monitor does not need AWS credentials, while the role currently has broad Parameter Store access. After compatibility testing, use hop limit 1 and/or block container access to `169.254.169.254`.
5. **No durable audit trail:** no CloudTrail trail was found. Event History may retain recent management events, but there is no durable multi-Region delivery. Session Manager transcript logging/preferences are also absent.
6. **Alarms have no direct actions:** three CloudWatch alarms exist (`tellor-ops-cpu-high`, `tellor-ops-cpu-credits-low`, `tellor-ops-status-check`) and were `OK`, but all have empty `AlarmActions`/`OKActions`. They perform no direct notification or recovery action; an exhaustive check for external EventBridge/composite consumers was not performed.
7. **No recovery proof:** the root volume deletes on termination. No self-owned snapshot, AWS Backup recovery point, launch template, stop protection, or termination protection was found, and no restore test is documented.
8. **No managed patch baseline:** no SSM association/patch state was found and the SSM Agent is not latest.
9. **Non-reproducible deployment:** host trees lack Git metadata, local sources are dirty/untracked, the staging bucket is unversioned, and there is no artifact manifest or rollback mapping.
10. **Refprice not trustworthy yet:** the last run failed, its log time conflicts with instance launch, its required benchmark is manual, and file existence can represent only an unbenchmarked recovery capture.
11. **CPI false success:** it is USA-only and can exit 0 with all states recorded as `N/A`; its empty `.env` is not the problem.
12. **Metrics profile exposure:** it references sibling bind mounts that may not exist and publishes Grafana 3000 / Prometheus 9090 on all host interfaces. If used, bind to loopback and reach it through SSM port forwarding.
13. **Unbounded logs:** append-only systemd/app logs and Docker logs have no documented rotation or retention; 40 GiB free-space inventory is not a retention policy.

---

## Required setup before choosing the final instance design

### 1) Agree on the workload contract

The first decision is what should run here; instance size and hardening follow from that contract.

| Workload | Known current behavior | Decision still required |
|---|---|---|
| OpenZeppelin Monitor | Intentionally off; if enabled it is a continuous Ethereum-mainnet alert service with local checkpoints | Should it be 24/7? Alert-delivery SLO, maximum block lag, RTO/RPO, owner, and maintenance window |
| Watchdog | Independent timer that treats intentional off as failure; shallow container-running check only | Derive enablement from the same desired-state value as Monitor; define deduplication, recovery alerts, and end-to-end health |
| Reference price | Daily prototype/testnet-grade capture; manual BRRNY dependency; no reporter submission | Keep as research, promote, or remove? Define data-quality gate, owner, backfill behavior, and whether it belongs on this host |
| CPI | Monthly third-Thursday USA Selenium scrape; starts at 16:00 while refprice can still be finishing | Required countries/coverage, useful-result threshold, retry/backfill, owner, and whether to isolate the browser job |

Also decide the availability boundary: one replaceable instance with accepted downtime, a self-healing single-instance design, or separate services/failure domains. Do not size a 24/7 host for a monthly browser spike until co-location is an explicit choice.

### 2) Establish the minimum security and recovery baseline

| Area | Required checkpoint before unattended production |
|---|---|
| Human access | Use IAM Identity Center or another federated identity source for short-lived credentials; map reviewed `Observer`, `Operator`, `Deploy`, and break-glass capability sets to least-privilege permissions using explicit resources and protected tag conditions where supported; require MFA; alert on root use; make mutation preflight reject root; document and test root-only recovery |
| Network | Keep zero ingress; after testing a break-glass recovery path, disable `sshd` if SSM-only is intended; decide explicitly between the present public-egress design and private subnets plus NAT/endpoints; bind any metrics UI to loopback |
| IAM/secrets | Remove effective account-wide Parameter Store reads; make one SecureString/secret per value authoritative; fetch by name on-host; rotate exposed webhook; prevent `.env.bak.*` from entering Git/artifacts |
| Guest/process isolation | Dedicated non-login users, Chrome sandbox enabled, systemd `NoNewPrivileges`/filesystem restrictions, explicit writable paths, and measured CPU/memory limits; for Monitor, test a non-root container UID, dropped capabilities, read-only paths, and resource limits or evaluate a rootless runtime—Docker-group membership remains root-equivalent host control |
| Audit | Multi-Region CloudTrail to encrypted/versioned S3 with validation; encrypted Session Manager transcripts, idle/max duration, and Run As where practical |
| Patching | SSM Quick Setup/Patch Manager: scan first, scheduled install with reboot/health/rollback plan; update the SSM Agent; install and configure the CloudWatch Agent |
| Recovery | Versioned immutable releases, encrypted scheduled backups/snapshots to agreed RPO, durable workload data separated from replaceable OS where useful, and a demonstrated restore |
| Alarms | Attach notification actions or document/test an external routing path and owner; add surplus-credit, memory, disk/inodes, service health, Monitor block lag, refprice freshness/success, and CPI useful-result alarms |
| Reproducibility | Versioned launch template/IaC, current AMI policy, bootstrap that is idempotent and secret-safe, deployment manifest, and tested rollback |

The encrypted current volume is good, but regional EBS encryption-by-default is disabled; enable the regional default for future volumes after choosing the account KMS policy.

### 3) Measure before resizing

Current `t3a.large` facts: 2 vCPU, 8 GiB RAM, Unlimited credits. Roughly the first eight bootstrap-heavy hours showed CPU average about 7.5%, a five-minute maximum about 60%, rising `CPUCreditBalance`, and zero `CPUSurplusCreditBalance`/`CPUSurplusCreditsCharged`. The Monitor is now off, and there are no RAM, filesystem, or process metrics. This evidence does **not** justify downsizing.

Measurement plan:

1. Enable one-minute EC2 monitoring and the CloudWatch Agent for memory, filesystem/inodes, swap/OOM, and per-workload process/container RSS/CPU; ship relevant systemd/Docker/app logs with encryption and retention.
2. Emit low-cardinality workload metrics: Monitor block lag/restarts/errors, refprice success/duration/result freshness, and CPI duration/non-`N/A` coverage/output freshness.
3. Run the intended workload mix until at least one real third-Thursday CPI run and its 16:00 overlap with refprice have been captured; the gap between such runs can exceed 32 days. Include a notification-disabled Monitor catch-up/replay test. If waiting is impractical, run a controlled CPI load test as supplemental evidence.
4. Opt in to Compute Optimizer (currently `Inactive`) after telemetry exists. Its free 32-day preference is better than the default 14 days but may still miss a monthly CPI run; supplement it with the captured/load-test evidence or consider the paid 93-day enhanced lookback. Review p95/p99 plus agreed headroom; do not accept a recommendation that violates the workload SLO.

Candidate comparison set (reported as offered in `us-east-2a` at audit time; recheck offering, quota, capacity, and price before launch):

| Candidate | When it earns a test |
|---|---|
| `t3a.medium` (x86, 2 vCPU / 4 GiB) | Peak memory plus headroom fits below 4 GiB and the lower burst baseline/credit balance remains safe |
| Current `t3a.large` (x86, 2 / 8) | Memory needs 4–8 GiB and CPU remains bursty without surplus charges |
| `c8a.large` (x86, 2 / 4) | CPU is sustained, memory fits 4 GiB, and fixed CPU outperforms Unlimited economics |
| `m8a.large` (x86, 2 / 8) | CPU is sustained and 8 GiB is required |
| `r8a.large` (x86, 2 / 16) | Only if measured memory pressure requires it |
| Arm64 `t4g` / `c8g` / `m8g` / `r8g` | Only after verifying every image, Go binary, Python dependency, browser, and agent; the current x86 AMI/Chrome stack cannot resize in place to Arm |

If CPI creates the only large monthly peak, compare moving it to an on-demand x86 task instead of paying for that headroom continuously.

### 4) Make future collaboration reproducible

Before the next infrastructure mutation, add and review these version-controlled artifacts:

- `WORKLOAD_CONTRACT.md`: the decisions/SLOs/owners above and one explicit desired-state declaration per independent workload; derive watchdog state from Monitor state.
- One IaC implementation (`CDK`, CloudFormation, or Terraform) containing the launch template, IAM, SG, logging, alarms, backups, and tags.
- A secret-free `inventory-readonly` script that prints caller/account/Region and captures EC2, SG, IAM, SSM, volume, alarm, backup, and workload status without parameter values.
- `DEPLOYMENT_MANIFEST.json`: Git commits, dirty-patch prohibition or hash, artifact/binary/image digests, deployed destinations, time, and rollback release.
- The missing `AWS_DOCKER_OPERATIONS.md`, the existing multi-root `scripts/AWS.code-workspace`, restore test, and incident/rollback runbooks.
- Version-controlled, narrowly scoped SSM documents for status, deploy, enable/disable, and rollback instead of arbitrary ad hoc shell strings.

That is the next clean checkpoint: agree on the workload contract and identity/IaC choice, then implement security/telemetry, capture every intended workload (including a real or controlled CPI peak), and select the instance shape from evidence.

---

## Quick status commands (on host)

```bash
sudo systemctl is-enabled openzeppelin-monitor.service; sudo systemctl is-active openzeppelin-monitor.service
sudo systemctl list-timers --all refprice-daily.timer cpi-bigmac.timer monitor-watchdog.timer
sudo docker ps -a --filter name=migration-monitor
sudo docker inspect migration-monitor-1 --format '{{json .State}}'
sudo ls -la /opt/tellor/autoTasks/migration/.env*
sudo tail -n 50 /var/log/tellor/monitor-watchdog.log
sudo tail -n 50 /var/log/tellor/refprice-daily.log
```

From laptop:

```bash
aws sts get-caller-identity  # stop before mutations if Arn ends in :root
aws ec2 describe-instances --region us-east-2 --instance-ids i-00b865643d4023484 \
  --query 'Reservations[0].Instances[0].State.Name'
aws ssm describe-instance-information --region us-east-2 \
  --filters Key=InstanceIds,Values=i-00b865643d4023484
```

---

## Related local workspaces

| Path | Role |
|------|------|
| `/Users/df/projects/dev/autoTasks` | Monitor migration + this handoff |
| `/Users/df/projects/dev/CPI` | Big Mac CPI scraper |
| `/Users/df/projects/dev/layer-daemons` | refprice prototype / daemons |

## Audit provenance

- **Live AWS/host readback:** account/instance/AMI/network/SG/metadata/credit/volume/IAM/Parameter Store metadata/SSM state, alarms/metrics, Compute Optimizer, CloudTrail, backup/snapshots, and installed service/container state at 2026-07-21 04:52–04:59 UTC. Secret values and external webhook delivery were not queried.
- **Local source map:** `migration/docker-compose.yaml`, `migration/scripts/tellor-ops-setup.sh`, all Monitor JSON/handlers/tests, CPI `main.py` + USA Selenium path, and `layer-daemons` refprice command/README/tests.
- **Validation:** Compose config and shell syntax passed; Monitor handler/config suite 54/54; quickstart suite 39/39; focused refprice packages passed. CPI test discovery found 0 tests. These validate local working trees, not the unmanifested deployed copies.
- **Changed claims:** clarified Exit 137, SSM-vs-SSH exposure, effective IAM parameter access, env reload behavior, unused env variables, watchdog depth, refprice maturity/time contradiction, CPI configuration/data-quality behavior, and the insufficiency of current sizing data.
- **Residual uncertainty:** exact secret values/rotation state, root MFA/access-key posture, Discord delivery, external alarm consumers, OS package patch level, external and container-to-IMDS reachability, deployed-file/binary equivalence, Arm compatibility, and representative full-load memory/process data remain unverified.

Current AWS primary references used for the security/sizing guidance:

- [Unlimited burstable-instance behavior](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode-concepts.html)
- [Configure IMDS options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html)
- [Session Manager preferences and logging](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-configure-preferences.html)
- [Compute Optimizer rightsizing preferences](https://docs.aws.amazon.com/compute-optimizer/latest/ug/rightsizing-preferences.html)
- [EBS encryption by default](https://docs.aws.amazon.com/ebs/latest/userguide/encryption-by-default.html)
- [AWS account root-user best practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/root-user-best-practices.html)

End of handoff.

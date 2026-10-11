# AzurJuus plugin kernel

`runtime.ts` hosts application services on the official `@deepseek-ai/cordis`
Context. Cordis owns plugin fibers, injected services, event listeners and
cleanup; the application adapter adds deterministic startup and its domain contracts.

`native/workspace.mjs` is loaded directly into the official Harness SDK runtime.
It registers the phase-scoped workspace tool, passes calls to the local Python
authorization gateway and projects admitted images as durable attachments.
There is no MCP relay. Python owns task persistence, operating-system permissions
and domain storage; it does not duplicate the Harness model/tool loop.

`runtime-events` reads /api/runtime/events incrementally without duplicating
SQLite data. `workflows` projects run.* and tool.* events into a stable service.
Native tool and turn lifecycle events are persisted by the SDK bridge as
`runtime.native` events. The prototype session/tool registries, separate JSONL
log and unused gateway client have been removed; the official Harness owns
model sessions and tool registrations, and RunStore owns application journals.

The Python bridge owns one startup and shutdown task per phase, serializes full
turns and joins blocking SDK calls after shutdown. It uses an initialized
Session directly so a queued turn cannot lazily restart a closed runtime.
Active guidance uses the official session/prompt inbox; runtime.steering records
delivery only after a root-session agent/inbox/spliced receipt. Unreceived
guidance remains pending for task resume. The SDK wire has no per-session cancel,
so phase cancellation shuts down its owned runtime. Cancellation during startup
waits for initialization to settle (currently bounded by its 90-second timeout).

KernelHost starts and stops runtime-events, workflows, messages and domains
as one unit. Each workspace owns a host; the frontend supplies its HTTP event
source and live WebSocket notifications through the same ingestion contract.
The browser creates the Cordis host during the real workspace lifecycle and
feeds its WebSocket events into the workflow projection. Domain signals for
mind, skill, social and workflow changes pass through the `domains` plugin.
An unused native event-source placeholder has been removed; HTTP/WebSocket is
the actual transport for the shared RunStore journal.

The messages plugin owns live chat streams, task interruption/removal and the
handoff to persisted messages. Vue subscribes to this projection and never
rebuilds stream content from runtime payloads. Completed streams are removed
only when a persisted snapshot contains the same message ID and body.

Run `npm run test:kernel` for Cordis lifecycle and native tool contracts.
`tests/tasks/test_native_harness.py` runs the installed official runtime against
a local synthetic model and tool gateway. `tools/helper_acceptance.py` performs
a short cloud helper acceptance using saved connected members and isolated data.
`tests/tasks/test_harness_lifecycle.py` covers concurrent startup/shutdown,
cancellation, callback failures, late notifications and guidance receipts.

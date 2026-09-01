# Telegram Command Surface

## Safety contract

Telegram commands are deterministic gateway controls. They are accepted only after the
Telegram webhook secret, numeric sender allowlist, chat restriction (when configured),
and durable `update_id` claim succeed. A command never becomes model input. A duplicate
completed update sends neither a second command action nor a second reply.

The command registry in `src/hive/gateway/telegram_commands.py` is the one source of
truth for dispatch, `/help`, `/commands`, and the Bot API native command menu.

## V1: implemented

| Command | Behavior |
| --- | --- |
| `/start` | Confirm that Hive is reachable and point to deterministic controls. |
| `/help [command]`, `/commands` | Generate help from the central registry. |
| `/new [title]`, `/reset [title]` | Select a new durable conversation; prior history is retained. |
| `/status` | Read-only session, memory, task, and approval summary. |
| `/sessions`, `/resume <number>` | List or restore this chat/user/topic's own conversation history. |
| `/memory` | Read-only memory-provider and record-count status, plus aggregate open/review projection counts; it never exposes claims, identifiers, or provider errors. |
| `/autonomy` | Pull-only summary of the deterministic autonomy policy and aggregate evidence. Past owner decisions remain evidence only and never expand Hive's permissions. |
| `/correct <stable-key> \| <claim> \| <reason>` | Owner-only append-only correction of a canonical claim. It is deduplicated with the Telegram update, never calls the model, and retains prior versions. |
| `/tasks` | Read-only recent durable task list, without payloads. |
| `/approvals` | Read-only pending approval IDs, kinds, and tool names; never renders approval arguments. |

Sessions are scoped to `bot_scope + chat_id + user_id + thread_id`. The first use keeps
the pilot's existing legacy Telegram session; subsequent `/new` entries receive a fresh
ID. No command in v1 deletes session rows or messages.

## Staged roadmap

1. **Foundation — delivered:** central registry, parser, durable session binding,
   native Telegram menu, local commands, and webhook-level deduplication tests.
2. **Approval controls — delivered:** `/approve <id>` and `/deny <id>` call the same
   durable decision path as `POST /approvals/decide`, including expiry, kill-switch,
   audit, and at-most-once execution guards. They require a user in
   `TELEGRAM_OWNER_USER_IDS`, which must be a subset of the delivery allowlist.
3. **Capability controls:** only after explicit design and tests, add guarded
   `/goal`, `/model`, `/skills`, `/review`, and background-task controls.
4. **Excluded from Telegram v1:** arbitrary shell, YOLO/approval bypass, deployment,
   plugin installation, config writes, and restart controls.

## Operations

On gateway startup, `TelegramChannel.set_commands()` reconciles Telegram's native menu
from the central registry. A Bot API failure is logged and does not disable authenticated
message ingress. Menu registration contains no secret values.

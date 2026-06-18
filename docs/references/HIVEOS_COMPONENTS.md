# HiveOS — Component reference (per-module map)

> Authoritative per-module map of the built `hive` package, in the format of the
> reference reports. Columns: **Module** · **Responsibility** · **Key public API** ·
> **Wired by** · **Source pattern** · **Tests**. See `docs/ARCHITECTURE.md` for flows
> and `docs/STATUS.md` for built/gap status. ⚠ = built but not wired (see STATUS §gaps).

## core/ (leaf — imports nothing higher)
| Module | Responsibility | Key public API | Wired by | Source | Tests |
|---|---|---|---|---|---|
| `core/registry.py` | generic per-subclass registry | `RegistryBase[T]` | tools/registry | OpenJarvis | test_core_primitives |
| `core/events.py` | thread-safe pub/sub spine | `EventBus`, `EventType`, `Event` | runtime (all subs) | OpenJarvis | test_core_primitives |
| `core/types.py` | canonical chat/tool types | `Message,Role,ToolCall,ToolResult,Conversation,ModelSpec` | everywhere | OpenJarvis | test_core_primitives |
| `core/config.py` | frozen typed config from env | `HiveConfig.from_env`, `get/set_config`, `validate`, `llm_summary`, `is_production`, `to_safe_dict` | runtime | OpenClaw | test_runtime |
| `core/doctor.py` | health checks + versioned migrations | `check`, `run`, `main`; `schema_migrations` table records applied versions | `hive doctor` | OpenClaw | test_core_health |
| `core/credentials.py` | 0o600 secret vault | `save`, `get`, `inject` | runtime (inject + pool seed) | OpenJarvis | test_m6_wiring |
| `core/soul.py` | lazy read of PROTECTED SOUL.md | `SOUL`, `SOUL_PATH`, `REPO_ROOT` | prompt_builder | HiveOS bridge | test_protected_bridges |
| `core/approval.py` | bridge to PROTECTED approval gate | `gate`, `PROTECTED_PATHS`, `DANGEROUS_TOOLS` | tools/executor, self_mod | HiveOS bridge | test_protected_bridges |
| `core/self_mod.py` | safe self-mod (worktree→test→PR) | `SelfModifier`, `github_pr_opener`, `SelfModifier.{success_rate,failed_proposals,proposals_by_stage,recent_branches,history,last_result,proposal_count}` | runtime, spec_search | HiveOS+OJ+Hermes | test_self_mod |
| `core/spec_search.py` | risk-tiered self-improvement | `Edit,EditOp,RiskTier,SelfImprovement`, `SelfImprovement.{tier_summary,pending_count,describe_pending,cancel_review,apply_approved}` | runtime | OpenJarvis spec_search | test_spec_search |
| `core/budgeter.py` | call cap + credit window + cost accrual | `Budgeter.{gate,record_call,record_usage,refresh,snapshot,remaining_calls,is_near_cap,forecast,calls_per_hour,cost_per_call,warning_status}` | runtime (events) | HiveOS+Hermes | test_resilience |
| `core/sandbox.py` | container test runner for self-mod | `make_sandbox_runner`, `docker_command` | runtime | OpenJarvis sandbox | test_hardening |
| `core/redact.py` | mask secrets before logs/audit | `redact_text`, `redact_args`, `mask_secret` | observability/audit | Hermes redact | test_m7_hardening2 |

## llm/
| Module | Responsibility | Key public API | Wired by | Source | Tests |
|---|---|---|---|---|---|
| `llm/router.py` | planner/executor split + resilience + stream | `ModelRouter.{complete,stream}`, `TaskKind`, `make_codex_planner` | runtime | HiveOS+Hermes | test_llm, test_resilience, test_surfaces |
| `llm/failover.py` | error taxonomy + retry policy | `classify`, `FailoverReason`, `RetryPolicy` | router | Hermes | test_llm |
| `llm/credential_pool.py` | multi-key pool + cooldowns | `CredentialPool.{acquire,report_failure,cooldown_all,reset_cooldowns,status,available_count,labels,failure_counts,total_failures}` | runtime/router | Hermes | test_llm, test_resilience |
| `llm/model_catalog.py` | per-model capabilities | `ModelCatalog.{get,register,unregister,list_models,__contains__,__len__}` | router/adapter | OpenClaw+OJ | test_llm |
| `llm/pricing.py` | per-token cost table | `cost_usd`, `rate_for` | router | Hermes usage_pricing | test_resilience |
| `llm/rate_limit.py` | parse x-ratelimit-* headers | `parse_rate_limit_headers`, `RateLimitState` | adapter→router | Hermes | test_resilience |
| `llm/sanitize.py` | surrogate strip + tool-arg repair | `sanitize_messages`, `repair_tool_arguments` | adapter | Hermes | test_new_components |
| `llm/adapters/base.py` | adapter contract + stream default | `LLMAdapter`, `CompletionRequest/Result`, `Usage` | router | OpenJarvis engine | test_surfaces |
| `llm/adapters/__init__.py` | provider-plugin registry | `make_adapter`, `PROVIDERS` | runtime (exec provider) | M8 | test_m8_providers |
| `llm/adapters/minimax.py` | MiniMax Anthropic endpoint + SSE | `MiniMaxAdapter.{complete,astream}` | runtime | HiveOS | test_minimax_serialization |
| `llm/adapters/anthropic.py` | native Anthropic (reuses minimax wire) | `AnthropicAdapter` | runtime (provider=anthropic) | Hermes | test_m8_providers |
| `llm/adapters/codex.py` | Codex (ChatGPT Plus) as an adapter | `CodexAdapter`, `run_codex`, `PlannerError` | router planner | Hermes codex | test_m8_providers |
| `llm/host_bridge.py` | sync host-LLM for Mnemosyne (own loop+client) | `HostLLMBridge` | runtime → `build_mnemosyne_provider(host_llm=)` | Mnemosyne llm_backends | test_a3_hostllm |

## agents/
| Module | Responsibility | Key public API | Wired by | Source | Tests |
|---|---|---|---|---|---|
| `agents/base.py` | agent ABCs + context/result | `BaseAgent,ToolUsingAgent,AgentContext,AgentResult` | orchestrator/delegate/executor | OpenJarvis | test_agents |
| `agents/orchestrator.py` | the turn loop | `ConversationOrchestrator.ask` | runtime | OJ+Hermes | test_agents, test_runtime |
| `agents/loop_guard.py` | degenerate-loop detection | `LoopGuard.{check,reset,stats,top_repeated_tools,call_count}` | orchestrator | OJ+Hermes | test_agents |
| `agents/delegate.py` | parallel leaf subagents + named-factory registry | `delegate`, `register_agent`, `get_agent_factory`, `delegate_named` | (callable); M10-d named registry | Hermes | test_hardening, test_m10_agents |
| `agents/planner.py` | goals+state → task list | `Planner.plan` | runtime/heartbeat | HiveOS | test_agents |
| `agents/executor.py` | agent tick + retry + terminal outcome | `AgentExecutor.execute_tick`, `TerminalOutcome` | agents/delegate | OJ+OpenClaw | test_agents, test_m6_wiring |
| `.claude/agents/*.md` | Claude Code specialist sub-agent definitions (researcher/coder/reviewer/memory-keeper/security-reviewer) | — (YAML frontmatter + system prompt) | Claude Code session; `HiveOS.agents_registry` | M10-d | test_m10_agents |

## memory/
| Module | Responsibility | Key public API | Wired by | Source | Tests |
|---|---|---|---|---|---|
| `memory/provider.py` | single-slot memory ABC | `MemoryProvider` | runtime | Hermes+OpenClaw | test_memory |
| `memory/mnemosyne_provider.py` | real Mnemosyne adapter + host-LLM bridge | `build_mnemosyne_provider`, `HiveMnemosyneProvider`, `set_host_llm_backend` | runtime | Mnemosyne §6 | test_new_components, test_m9_mnemosyne_bridge |
| `memory/local.py` | SQLite fallback provider | `LocalMemoryProvider`, `system_prompt_block()` (returns top-5 facts by importance), `most_important_facts(limit)`, `memory_stats()`, `list_topics(kind)`, `wipe_knowledge(kind)`, `count_episodic(session)`, `delete_session_memory(session)`, `export_backup()` | runtime (fallback) | HiveOS | test_memory |
| `memory/keeper.py` | sleep-time consolidation | `MemoryKeeper.consolidate` | runtime | Hermes curator | test_memory |
| `memory/vault.py` | Obsidian markdown export | `ObsidianVault.write` | local provider | HiveOS | test_hardening |
| `memory/curator.py` | skill lifecycle state machine + LLM umbrella consolidation | `Curator.{run,restore,consolidate_umbrellas}`, `CuratorConfig`; `consolidate_umbrellas` is async (aux-model summarizer injected), fail-open | runtime, heartbeat | Hermes curator | test_curator |
| `memory/skill_usage.py` | skill usage store | `SkillUsageStore.{record,get,by_state,recently_used,stats,pin,unpin,unused_skills,archived_count}` | runtime | Hermes skill_usage | test_curator |

## context/
| Module | Responsibility | Key public API | Wired by | Source | Tests |
|---|---|---|---|---|---|
| `context/session_store.py` | SQLite sessions + FTS5 | `SessionStore.{append,messages,search,list_sessions,count_messages,stats,get_title,set_title,get_summary,delete_session,total_message_count}` | runtime | Hermes SessionDB | test_context |
| `context/compaction.py` | head/tail-protected summary | `compact` | orchestrator | Hermes | test_context |
| `context/prompt_builder.py` | prefix-cached system prompt | `system_prompt`, `restore_or_build_system_prompt`, `build_messages` | orchestrator, ask_stream | Hermes+OJ | test_context |
| `context/title.py` | session auto-naming | `generate_title` | `HiveOS.title_session` | Hermes title_generator | test_m7_hardening2 |

## tools/
| Module | Responsibility | Key public API | Wired by | Source | Tests |
|---|---|---|---|---|---|
| `tools/base.py` | tool ABC + spec | `BaseTool`, `ToolSpec` | builtins/mcp | OpenClaw | test_tools |
| `tools/registry.py` | typed tool registry | `ToolRegistry` | runtime | HiveOS+OJ | test_tools |
| `tools/executor.py` | dispatch: file-safety→gate→exec→audit | `ToolExecutor.{execute,execute_approved,stats,dangerous_tools,tool_categories}`, `DispatchStatus` | runtime | OJ+Hermes | test_tools |
| `tools/file_safety.py` | sensitive-path denylist | `check_path`, `is_write_denied` | tools/executor | Hermes | test_new_components |
| `tools/discovery.py` | discovery-first engine + security annotation | `discover` (optional `security_delegate: Callable` — each candidate gets a `security_note`), `audit_repo`, `scan_red_flags` | builtins `discover` tool + `HiveOS.discover` | HiveOS DNA | test_m6_wiring |
| `tools/builtins/__init__.py` | read_file/write_file/shell/web_get + gated spend_money/deploy/external_message + delegate_to_specialist + discover (with security audit) | `register_builtins`; `DelegateToSpecialist` routes to named sub-agents via local import; `DiscoverTool(enable_security_audit=True)` wires security-reviewer | runtime | HiveOS | test_tools, test_m6_wiring |
| `tools/mcp/client.py` | MCP client (stdio + SSE) + tool adapter | `MCPClient`, `MCPTool`, `mcp_tool_to_spec` | `HiveOS.load_mcp_servers` (gateway startup) | OpenJarvis | test_hardening, test_m6_wiring, test_m9_transport |
| `tools/mcp/server.py` | serve Hive tools over MCP | `MCPServer`, `build_tool_listing` | `HiveOS.mcp_server()` / `HiveOS.serve_mcp` / `hive mcp-serve` | Mnemosyne mcp_server | test_hardening, test_m9_mcp_server |
| `tools/shell_provider.py` | terminal-environment abstraction | `ShellProvider` (ABC), `LocalShellProvider`, `ShellResult` | `tools/builtins` Shell tool | Hermes #11 | test_m9_shell_provider |

## gateway/ · autonomy/ · surfaces/ · observability/ · runtime
| Module | Responsibility | Key public API | Wired by | Source | Tests |
|---|---|---|---|---|---|
| `gateway/app.py` | FastAPI surface — 100+ endpoints across 19 groups (health/chat/budget/config/tools/memory/telemetry/audit/tasks/sessions/cron/commitments/approvals/skills/llm/self-improve/events/loop-guard/telegram) | `create_app` | `hive serve` | HiveOS+OJ | test_gateway, test_surfaces, test_m10_observability |
| `gateway/protocol.py` | typed boundary models | `ChatRequest/Response`, `ApprovalDecision` | gateway | OpenClaw | test_gateway |
| `gateway/auth.py` | constant-time bearer auth | `make_auth_dependency`, `token_ok` | gateway | OpenClaw/Hermes | test_gateway |
| `gateway/channels/base.py` | transport-only channel ABC | `ChannelAdapter,MessageEvent,OutgoingMessage,SendResult` | telegram | OpenClaw | test_surfaces |
| `gateway/channels/telegram.py` | Telegram Bot API transport | `TelegramChannel` | gateway webhook | Hermes/OpenClaw | test_surfaces |
| `autonomy/heartbeat.py` | never-idle loop | `Heartbeat.{tick,run,enqueue}` | `hive heartbeat` | HiveOS | test_p8_subsystems, test_autonomy |
| `autonomy/tasks.py` | durable task board | `TaskBoard.{enqueue,due,claim,complete,fail,recent_failures,statistics,search,retry_all_failed,purge_done,running_count,last_failed,bulk_cancel_pending,bulk_purge_failed,count_by_kind,failed_count,oldest_pending,pending_by_kind,average_age_pending,oldest_pending_age,total_count,failure_rate_by_kind}` | runtime/heartbeat | Hermes/OpenClaw | test_autonomy, test_m10_self_improve |
| `autonomy/cron.py` | scheduled jobs | `CronScheduler.{add,remove,due_and_enqueue,jobs,get,set_enabled,enabled_count,due_count,overdue_jobs,next_due_time,job_health}` | runtime/heartbeat | Hermes cron | test_autonomy |
| `autonomy/commitments.py` | recurring promises | `CommitmentBook.{add,remove,fulfill,all,overdue,active_names,next_due_at,upcoming}` | runtime/heartbeat | Hermes | test_autonomy |
| `surfaces/cli.py` | `hive` CLI | `main` (chat/ask/serve/heartbeat/consolidate/doctor/mcp-serve) | console script | HiveOS | test_m9_mcp_server |
| `surfaces/voice.py` | wake-word→STT→gateway→TTS | `loop`, `STT`, `TTS` | `python -m hive.surfaces.voice` | HiveOS | (manual; needs audio) |
| `observability/telemetry.py` | call/token/cost counters | `Telemetry.{attach,snapshot,selfmod_success_rate,top_model,total_tokens}` | runtime | OpenJarvis | test_hardening |
| `observability/traces.py` | per-session event traces + export | `TraceCollector.{attach,trace,export,export_all,sessions,session_count,total_event_count,event_count,event_type_counts,clear}` | runtime | OpenJarvis | test_hardening |
| `observability/audit.py` | tool-call audit log (SQLite) | `AuditLog.{record,recent,search,stats,error_rate,recent_errors,recent_by_tool,purge_old,export}` | tools/executor | OpenClaw | test_tools |
| `runtime.py` | composition root | `HiveOS`, `HiveOS.build`, `mcp_server`, `self_improve_from_symptom`, `agents_registry`, `run_tests`, `self_diagnose`, `health`, `system_status`, `resume_after_restart`, `event_history`, `loop_guard_stats`, `reset_loop_guard`, `self_mod_history`, `recent_self_mod_branches`, `pending_review_edits`, `abort_all_self_mods` | gateway/cli/heartbeat | OpenJarvis builder | test_runtime |

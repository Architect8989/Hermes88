# Open-Source Integrations -- Rhodawk AI Hermes88

> Rhodawk AI is built on the shoulders of open-source giants. Our philosophy is simple:
> use battle-tested, community-maintained open-source projects instead of building custom
> engines from scratch. Every component is chosen for production reliability, active
> maintenance, permissive licensing, and strong community support.
>
> This document catalogs every open-source project integrated into the Hermes88 JARVIS-grade
> autonomous intelligence system, explaining why each was chosen, how it integrates, and
> what it replaces.
>
> **Estimated total development value of integrated projects: $45+ million**
> (Based on GitHub stars, contributor count, and years of active development)

---

## Table of Contents

1. [Memory and Knowledge](#a-memory--knowledge)
2. [Agent Orchestration](#b-agent-orchestration)
3. [Tools and Capabilities](#c-tools--capabilities)
4. [Task Execution](#d-task-execution)
5. [Communication](#e-communication)
6. [Security and Auditing](#f-security--auditing)
7. [Performance and Code Tools](#g-performance--code-tools-rust-ecosystem)
8. [Infrastructure](#h-infrastructure)
9. [Browser Automation](#i-browser-automation)
10. [Data and Knowledge Graph](#j-data--knowledge-graph)
11. [Voice and Media](#k-voice--media)
12. [License Compatibility](#license-compatibility)

---

## A. Memory & Knowledge

Projects that power Hermes88's semantic memory system -- the ability to remember context,
retrieve relevant information, and maintain a persistent knowledge base across sessions.

---

### 1. mem0ai/mem0

| Field | Details |
|-------|---------|
| **Repository** | [mem0ai/mem0](https://github.com/mem0ai/mem0) |
| **Stars** | ~25,000+ |
| **Description** | Production memory layer for AI applications with vector + graph memory, importance scoring, temporal decay, and multi-user support |
| **License** | Apache-2.0 |

**Why chosen:** Hermes88 needs persistent memory that goes beyond simple RAG. mem0 provides
a complete memory management system with automatic importance scoring, temporal decay (old
memories fade unless reinforced), and the ability to store both factual knowledge and
episodic experiences. Unlike building a custom memory system, mem0 handles the complex
scheduling of memory consolidation and retrieval ranking out of the box.

**How it integrates:** Used by `rhodawk_core/memory.py` as the primary memory abstraction
layer. The MemoryEngine class wraps mem0's API to provide `store()`, `recall()`, and
`forget()` operations. Memory is persisted to Redis (vector embeddings) and SQLite
(metadata and relations). The importance scoring feeds into the proactive engine to
surface relevant memories before the operator asks.

**What it replaces:** Custom flat-file memory (the old `MEMORY.md` append-only log that
had no retrieval intelligence).

---

### 2. chroma-core/chroma

| Field | Details |
|-------|---------|
| **Repository** | [chroma-core/chroma](https://github.com/chroma-core/chroma) |
| **Stars** | ~16,000+ |
| **Description** | Open-source embedding database. Lightweight, embeddable vector store that runs in-process or as a service. |
| **License** | Apache-2.0 |

**Why chosen:** For local vector search without requiring a separate database service.
ChromaDB can run embedded within the Python process, making it perfect for the single-VPS
deployment model. It handles embedding storage, similarity search, and metadata filtering
with zero operational overhead. Chosen over Pinecone (proprietary), Weaviate (heavier),
and Milvus (requires cluster) for its simplicity and embeddability.

**How it integrates:** Used by `rhodawk_core/memory.py` for local vector similarity search.
When mem0 stores a memory, the embedding is also indexed in ChromaDB for fast retrieval.
The gateway's `memory_injector.py` queries ChromaDB to find contextually relevant memories
to inject into prompts before LLM calls.

**What it replaces:** Naive keyword search over flat text files.

---

### 3. run-llama/llama_index

| Field | Details |
|-------|---------|
| **Repository** | [run-llama/llama_index](https://github.com/run-llama/llama_index) |
| **Stars** | ~38,000+ |
| **Description** | Data framework for connecting custom data sources to LLMs. Provides RAG pipelines, knowledge graph construction, and intelligent retrieval. |
| **License** | MIT |

**Why chosen:** LlamaIndex provides battle-tested patterns for retrieval-augmented
generation, document ingestion, and knowledge graph construction. Rather than implementing
custom chunking, embedding, and retrieval strategies from scratch, we use LlamaIndex's
proven approaches as architecture references. Its composable retriever pattern directly
influenced the design of our memory retrieval pipeline.

**How it integrates:** Architecture reference for memory retrieval patterns in
`rhodawk_core/memory.py`. The hierarchical retrieval strategy (keyword -> vector ->
reranking) is adapted from LlamaIndex's ComposableGraph pattern. Document ingestion
for the Obsidian vault sync uses LlamaIndex's chunking strategies.

**What it replaces:** Custom document processing and retrieval logic.

---

### 4. obsidianmd/obsidian-releases

| Field | Details |
|-------|---------|
| **Repository** | [obsidianmd/obsidian-releases](https://github.com/obsidianmd/obsidian-releases) |
| **Stars** | ~32,000+ |
| **Description** | Personal knowledge management application with bidirectional linking, graph view, and plugin ecosystem. Local-first markdown vault. |
| **License** | Proprietary (app), but vault format is plain Markdown (open) |

**Why chosen:** Obsidian provides a human-readable, git-friendly knowledge vault format.
The operator can read, edit, and navigate the knowledge base using Obsidian's native app
while Hermes88 programmatically reads and writes to the same vault. The bidirectional
linking creates a navigable knowledge graph. Chosen over Notion (proprietary API, no
local-first), Logseq (smaller ecosystem), and plain wiki (no graph visualization).

**How it integrates:** The `skills/obsidian-sync/` skill manages bidirectional
synchronization between Hermes88's memory and the Obsidian vault at
`/data/.hermes/obsidian-vault/`. Templates in `hermes_config/obsidian/templates/`
define note structures for daily notes, decision logs, project notes, and research
findings. The vault is the operator's window into what Hermes knows and thinks.

**What it replaces:** Opaque database storage with no human-readable interface.

---

### 5. blacksmithgu/obsidian-dataview

| Field | Details |
|-------|---------|
| **Repository** | [blacksmithgu/obsidian-dataview](https://github.com/blacksmithgu/obsidian-dataview) |
| **Stars** | ~10,000+ |
| **Description** | High-performance data query engine for Obsidian vaults. Treats vault as a database with SQL-like queries over frontmatter and inline fields. |
| **License** | MIT |

**Why chosen:** Enables structured queries over the knowledge vault without a separate
database. The operator can write Dataview queries in Obsidian to surface specific
information (all decisions this month, all projects by status, all research by topic).
Hermes88 also generates Dataview-compatible frontmatter so vault notes are queryable.

**How it integrates:** Notes generated by the `skills/obsidian-sync/` skill include
YAML frontmatter with structured fields (date, tags, status, priority, related).
The `hermes_config/obsidian/vault_config.json` defines the Dataview schema expected
by the templates. This allows both human (via Obsidian app) and programmatic (via
file parsing) access to structured knowledge.

**What it replaces:** Manual tagging and search through unstructured notes.


---

## B. Agent Orchestration

Projects that enable Hermes88 to coordinate multiple LLM providers, route requests
intelligently, and manage multi-agent workflows.

---

### 6. BerriAI/litellm

| Field | Details |
|-------|---------|
| **Repository** | [BerriAI/litellm](https://github.com/BerriAI/litellm) |
| **Stars** | ~16,000+ |
| **Description** | Unified API for 100+ LLM providers. Handles load balancing, fallbacks, rate limiting, spend tracking, and caching across OpenAI, Anthropic, Azure, and custom endpoints. |
| **License** | MIT |

**Why chosen:** Hermes88 uses multiple LLM providers (DigitalOcean Inference primary,
NVIDIA NIM fallback) with different models assigned to different agents. LiteLLM provides
a single interface that handles provider-specific quirks, automatic failover when a
provider is down, rate limit management, and token usage tracking. Without LiteLLM, we
would need custom adapter code for each provider.

**How it integrates:** Used by `rhodawk_core/orchestrator.py` as the unified LLM call
layer. The orchestrator's `ModelRouter` uses LiteLLM's completion API with fallback
chains configured per-agent. OpenClaude uses `deepseek-r1-distill-llama-70b`, JCode
uses `kimi-k2.6`, and the gateway uses `deepseek-v4-pro` -- all routed through the
same LiteLLM interface with automatic failover to NVIDIA NIM.

**What it replaces:** Manual HTTP calls to each provider with custom retry logic.

---

### 7. pydantic/pydantic-ai

| Field | Details |
|-------|---------|
| **Repository** | [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) |
| **Stars** | ~5,000+ |
| **Description** | Type-safe agent framework built on Pydantic. Provides structured tool definitions, result validation, and dependency injection for AI agents. |
| **License** | MIT |

**Why chosen:** Pydantic-AI provides a clean pattern for defining agent tools with
full type safety and validation. Tool parameters are Pydantic models, tool results
are validated against schemas, and dependency injection keeps tools testable. This
pattern directly influenced how Hermes88's `rhodawk_core/tools.py` defines and
validates tool schemas.

**How it integrates:** Architecture reference for tool definitions in
`rhodawk_core/tools.py`. The tool registry pattern (decorator-based registration,
schema auto-generation from type hints, result validation) is adapted from
Pydantic-AI's approach. Tool definitions are exported as JSON schemas for MCP
compatibility.

**What it replaces:** Ad-hoc tool definitions with no validation.

---

### 8. microsoft/autogen

| Field | Details |
|-------|---------|
| **Repository** | [microsoft/autogen](https://github.com/microsoft/autogen) |
| **Stars** | ~38,000+ |
| **Description** | Multi-agent conversation framework enabling complex agent coordination patterns including group chat, sequential workflows, and nested conversations. |
| **License** | CC-BY-4.0 (docs), MIT (code) |

**Why chosen:** AutoGen pioneered the multi-agent conversation pattern where agents
communicate through structured messages with role-based routing. This directly influenced
the design of Hermes88's agent coordination system, where the gateway routes tasks to
specialized agents (OpenClaude for code, JCode for parallel work, Camofox for browsing)
based on task classification.

**How it integrates:** Reference architecture for agent coordination patterns in
`rhodawk_core/orchestrator.py`. The orchestrator's task classification and agent
routing logic is inspired by AutoGen's GroupChat pattern. The escalation system
(gateway -> OpenClaude -> JCode swarm) follows AutoGen's nested conversation model.

**What it replaces:** Hardcoded agent selection with no dynamic routing.

---

### 9. crewAI/crewai

| Field | Details |
|-------|---------|
| **Repository** | [crewAI/crewai](https://github.com/crewAI/crewai) |
| **Stars** | ~24,000+ |
| **Description** | Multi-agent orchestration framework with role-based agents, task delegation, and process-driven execution (sequential, hierarchical, consensual). |
| **License** | MIT |

**Why chosen:** CrewAI's role-based agent pattern (each agent has a role, goal, and
backstory) influenced how Hermes88 assigns specialized roles to its sub-agents. The
hierarchical process pattern (manager delegates to specialists) maps directly to how
the Hermes gateway delegates to OpenClaude, JCode, and other specialists.

**How it integrates:** Reference architecture for role-based agent patterns in
`skills/jcode_swarm/coordinator.py`. The swarm coordinator decomposes complex tasks
into sub-tasks and assigns them to specialized worker agents, following CrewAI's
hierarchical process model. Agent role definitions in `hermes_config/SOUL.md` use
CrewAI-inspired persona structuring.

**What it replaces:** Flat single-agent processing with no task decomposition.

---

## C. Tools & Capabilities

Projects that provide Hermes88 with concrete abilities: web browsing, file access,
search, and API interaction.

---

### 10. modelcontextprotocol/servers

| Field | Details |
|-------|---------|
| **Repository** | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) |
| **Stars** | ~15,000+ |
| **Description** | Official MCP (Model Context Protocol) server implementations. Provides filesystem, GitHub, fetch, brave-search, and other tools as standardized MCP services. |
| **License** | MIT |

**Why chosen:** MCP is the emerging standard for tool integration with LLMs. By using
official MCP servers, Hermes88 gets battle-tested implementations of filesystem access,
GitHub operations, web fetching, and search -- all through a standardized protocol that
any MCP-compatible agent can use. This future-proofs the tool layer as new MCP servers
are released.

**How it integrates:** Core tool layer configured in `mcp_shared.json`. The following
MCP servers are deployed: `@modelcontextprotocol/server-filesystem` (file operations),
`@modelcontextprotocol/server-github` (repo operations), `@anthropic/server-fetch`
(URL fetching), `@anthropic/server-brave-search` (web search). All agents
(OpenClaude, JCode, gateway) connect to these shared MCP servers.

**What it replaces:** Custom tool implementations for each capability.

---

### 11. browser-use/browser-use

| Field | Details |
|-------|---------|
| **Repository** | [browser-use/browser-use](https://github.com/browser-use/browser-use) |
| **Stars** | ~55,000+ |
| **Description** | AI-powered browser automation that lets LLMs interact with web pages through natural language. Handles navigation, form filling, clicking, scrolling, and data extraction. |
| **License** | MIT |

**Why chosen:** Many tasks require interacting with web interfaces that do not have APIs.
browser-use provides a natural language interface to web automation, allowing Hermes88
to navigate websites, fill forms, extract data, and interact with web applications without
writing site-specific scraping code. Its LLM-driven approach means it adapts to page
layout changes automatically.

**How it integrates:** Enhanced web browsing capability in `skills/stealth-browse/`.
When the Camofox stealth browser is available, browser-use provides the high-level
automation layer on top of the headless browser instance. The `camofox_manager.py`
manages browser sessions while browser-use handles the AI-driven interaction logic.

**What it replaces:** Manual Puppeteer scripts for each website interaction.

---

### 12. ScrapeGraphAI/Scrapegraph-ai

| Field | Details |
|-------|---------|
| **Repository** | [ScrapeGraphAI/Scrapegraph-ai](https://github.com/ScrapeGraphAI/Scrapegraph-ai) |
| **Stars** | ~18,000+ |
| **Description** | AI-powered web scraping that uses LLMs to extract structured data from any website. Supports multiple scraping strategies (single page, search-based, multi-page). |
| **License** | MIT |

**Why chosen:** Traditional web scraping requires writing custom selectors for each site
and breaks when layouts change. Scrapegraph-ai uses LLMs to understand page structure
and extract the requested data regardless of HTML layout. This makes the research-deep
and competitive-intel skills resilient to website changes.

**How it integrates:** Structured data extraction in `skills/research-deep/` and
`skills/competitive-intel/`. When research tasks require extracting specific data points
from web pages (pricing tables, feature lists, company information), Scrapegraph-ai
provides the extraction layer. Results are stored in the memory system with proper
attribution and source URLs.

**What it replaces:** Brittle CSS selector-based scraping that breaks on redesigns.

---

### 13. jina-ai/reader

| Field | Details |
|-------|---------|
| **Repository** | [jina-ai/reader](https://github.com/jina-ai/reader) |
| **Stars** | ~8,000+ |
| **Description** | Converts any URL to clean, LLM-friendly text. Strips navigation, ads, and boilerplate to return just the content. Supports r.jina.ai API or self-hosted. |
| **License** | Apache-2.0 |

**Why chosen:** LLMs work best with clean text, not raw HTML. Jina Reader converts
any URL into clean markdown/text suitable for LLM consumption. It handles JavaScript-
rendered pages, paywalls (where possible), and complex layouts. Essential for the
content ingestion pipeline where URLs need to be converted to knowledge.

**How it integrates:** Content ingestion pipeline in `skills/research-deep/` and the
gateway's URL processing. When a user shares a URL or research discovers relevant
pages, Jina Reader converts them to clean text before embedding and storage. Also
used by the `skills/rss-monitor/` skill to fetch full article content from RSS feed
links.

**What it replaces:** Custom BeautifulSoup parsers for each content type.

---

### 14. openai/openai-python

| Field | Details |
|-------|---------|
| **Repository** | [openai/openai-python](https://github.com/openai/openai-python) |
| **Stars** | ~25,000+ |
| **Description** | Official OpenAI Python client library. Provides typed interfaces for completions, embeddings, assistants, and all OpenAI API endpoints. |
| **License** | Apache-2.0 |

**Why chosen:** DigitalOcean Inference and NVIDIA NIM both expose OpenAI-compatible APIs.
The official openai-python client provides a mature, well-tested interface with proper
async support, streaming, retry logic, and type hints. Using the standard client means
any OpenAI-compatible endpoint works without custom HTTP code.

**How it integrates:** Used by `gateway/run.py` and `rhodawk_core/orchestrator.py` for
all LLM API calls. The client is configured with custom `base_url` pointing to DO
Inference (`https://inference.do-ai.run/v1`) or NVIDIA NIM endpoints. All streaming
responses, function calling, and embedding generation flow through this client.

**What it replaces:** Raw httpx/aiohttp calls to inference endpoints.


---

## D. Task Execution

Projects that enable Hermes88 to execute background tasks, schedule jobs, and manage
distributed work queues.

---

### 15. Bogdanp/dramatiq

| Field | Details |
|-------|---------|
| **Repository** | [Bogdanp/dramatiq](https://github.com/Bogdanp/dramatiq) |
| **Stars** | ~4,500+ |
| **Description** | Distributed task processing library for Python with Redis and RabbitMQ brokers. Provides reliable task queues with retries, rate limiting, priority queues, and result backends. |
| **License** | LGPL-3.0 |

**Why chosen:** Hermes88's task engine needs reliable background execution with retries,
priority queues, and dead-letter handling. Dramatiq provides all of this with a clean
actor-based API and Redis as the broker (already in the stack for memory/events). Chosen
over Celery for its simpler API, better error handling, and lower memory footprint.
The actor pattern maps cleanly to skill invocations.

**How it integrates:** Used by `rhodawk_core/task_engine.py` as the task queue
implementation. Each skill invocation is wrapped as a dramatiq actor with configurable
retries, timeouts, and priority. The task engine publishes skill execution tasks to
Redis queues, and worker processes consume and execute them. Failed tasks are retried
with exponential backoff before landing in the dead-letter queue.

**What it replaces:** Synchronous skill execution that blocks the gateway.

---

### 16. celery/celery

| Field | Details |
|-------|---------|
| **Repository** | [celery/celery](https://github.com/celery/celery) |
| **Stars** | ~25,000+ |
| **Description** | Distributed task queue with broad ecosystem support. The standard Python solution for background job processing at scale. |
| **License** | BSD-3-Clause |

**Why chosen:** Architecture reference for task queue patterns. While dramatiq is the
primary implementation (simpler API, lower overhead), Celery's patterns for task chains,
groups, chords, and canvas (complex workflow composition) influenced the design of
multi-step skill workflows in the task engine.

**How it integrates:** Reference architecture for complex workflow patterns in
`rhodawk_core/task_engine.py`. The task engine's `chain()`, `parallel()`, and
`pipeline()` operations are inspired by Celery's canvas primitives. The monitoring
dashboard concept references Celery Flower's approach.

**What it replaces:** N/A (reference only).

---

### 17. prefecthq/prefect

| Field | Details |
|-------|---------|
| **Repository** | [prefecthq/prefect](https://github.com/prefecthq/prefect) |
| **Stars** | ~18,000+ |
| **Description** | Workflow orchestration platform with Python-native flow definitions, automatic retries, observability, and scheduling. |
| **License** | Apache-2.0 |

**Why chosen:** Prefect's flow-as-code pattern (Python functions decorated as flows and
tasks) influenced how Hermes88 defines complex multi-step workflows. Its approach to
state management, retry policies, and result caching directly informed the task engine's
workflow model.

**How it integrates:** Reference for complex workflow patterns in
`rhodawk_core/task_engine.py`. The task engine's state machine (pending -> running ->
completed/failed/retrying) and the cron job definitions in `hermes_config/cron/` follow
Prefect's scheduling and state management patterns.

**What it replaces:** N/A (reference only).

---

### 18. agronholm/apscheduler

| Field | Details |
|-------|---------|
| **Repository** | [agronholm/apscheduler](https://github.com/agronholm/apscheduler) |
| **Stars** | ~6,000+ |
| **Description** | Advanced Python scheduler supporting cron-style triggers, interval triggers, date triggers, and persistent job stores. |
| **License** | MIT |

**Why chosen:** Hermes88 needs reliable cron-style scheduling for recurring tasks:
nightly repo sweeps, weekly traction reports, daily briefings, and memory maintenance.
APScheduler provides production-grade scheduling with persistent job stores (survives
restarts), timezone awareness, and misfire handling. Chosen over schedule (too simple)
and Airflow (too heavy for a single-VPS deployment).

**How it integrates:** Used for cron/scheduled tasks defined in `hermes_config/cron/`.
The cron engine loads job definitions from YAML files (`nightly_sweep.yaml`,
`weekly_traction.yaml`, `daily_briefing.yaml`, etc.) and registers them with APScheduler.
Jobs execute as task engine tasks, meaning they get retries, logging, and operator
notifications on completion or failure.

**What it replaces:** Manual cron setup or systemd timers.

---

## E. Communication

Projects that enable Hermes88 to communicate with the operator and reference architectures
for autonomous operation.

---

### 19. python-telegram-bot/python-telegram-bot

| Field | Details |
|-------|---------|
| **Repository** | [python-telegram-bot/python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) |
| **Stars** | ~27,000+ |
| **Description** | Full-featured Python wrapper for the Telegram Bot API. Provides async handlers, conversation flows, inline keyboards, media handling, and webhook support. |
| **License** | LGPL-3.0 |

**Why chosen:** Telegram is the primary communication channel for Hermes88. This library
provides the most complete Python implementation of the Telegram Bot API with proper async
support, conversation state management, inline keyboards for interactive responses, and
media handling (voice, images, documents). Its handler pattern (CommandHandler,
MessageHandler, CallbackQueryHandler) maps cleanly to the gateway's event routing.

**How it integrates:** The foundation of `gateway/run.py` (1241 lines). The gateway uses
python-telegram-bot's Application class with custom handlers for commands (/start, /status,
/memory), messages (text, voice, images), and callbacks (inline keyboard buttons like
Retry/Abort). The bot runs in polling mode on VPS deployments and can switch to webhook
mode for high-traffic scenarios.

**What it replaces:** Raw Telegram HTTP API calls.

---

### 20. Significant-Gravitas/AutoGPT

| Field | Details |
|-------|---------|
| **Repository** | [Significant-Gravitas/AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) |
| **Stars** | ~170,000+ |
| **Description** | Autonomous AI agent that chains LLM calls to accomplish goals. Pioneered the autonomous agent loop pattern with planning, execution, and self-reflection. |
| **License** | MIT |

**Why chosen:** AutoGPT pioneered the autonomous agent loop: plan -> execute -> observe ->
reflect -> plan again. This loop pattern is fundamental to Hermes88's proactive engine,
where the system continuously monitors events, plans responses, executes actions, and
reflects on outcomes. AutoGPT's approach to goal decomposition and self-correction
directly influenced the agentic client design.

**How it integrates:** Architecture patterns for autonomous operation in
`rhodawk_core/proactive.py` and `skills/openclaude_grpc/agentic_client.py`. The agentic
client's iteration loop (attempt -> evaluate -> decide: succeed/retry/escalate) follows
AutoGPT's observe-think-act cycle. The proactive engine's event perception and autonomous
response generation draws from AutoGPT's goal-driven architecture.

**What it replaces:** Single-shot request-response with no self-correction.

---

### 21. All-Hands-AI/OpenHands

| Field | Details |
|-------|---------|
| **Repository** | [All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands) |
| **Stars** | ~50,000+ |
| **Description** | Open-source AI software engineer. Provides sandboxed code execution, multi-step planning, and autonomous coding workflows. |
| **License** | MIT |

**Why chosen:** OpenHands demonstrates how to build a production-grade autonomous coding
agent with proper sandboxing, multi-step planning, and error recovery. Its architecture
for sandboxed execution (Docker containers per task) directly influenced Hermes88's
sandbox pool design. The approach to agentic coding loops (edit, test, fix, repeat)
is reflected in OpenClaude's agentic client.

**How it integrates:** Reference for agentic coding loops in
`skills/openclaude_grpc/agentic_client.py` and the sandbox architecture in
`sandbox/`. OpenHands' pattern of spinning up isolated Docker containers for
each coding task is implemented in the sandbox-manager service. The 3-strike
iteration pattern (attempt -> test -> fix, up to 3 times) is adapted from
OpenHands' error recovery strategy.

**What it replaces:** Unbounded agent execution with no isolation.


---

## F. Security & Auditing

Projects that enable Hermes88 to perform security audits, scan for vulnerabilities,
detect secrets in code, and ensure sandboxed execution safety.

---

### 22. PyCQA/bandit

| Field | Details |
|-------|---------|
| **Repository** | [PyCQA/bandit](https://github.com/PyCQA/bandit) |
| **Stars** | ~7,000+ |
| **Description** | Python security linter (SAST). Finds common security issues in Python code including SQL injection, hardcoded passwords, shell injection, and unsafe deserialization. |
| **License** | Apache-2.0 |

**Why chosen:** Every Python codebase that Hermes88 processes through its DevOps pipeline
needs security validation. Bandit catches the most common Python security anti-patterns
with zero configuration. It is fast, well-maintained, and produces structured JSON output
that can be parsed and reported through Telegram.

**How it integrates:** Used in the `skills/security-audit/` skill as one of four
security scanning tools. When a security audit is triggered (manually or as part of the
DevOps pipeline), Bandit scans all Python files and produces findings categorized by
severity (LOW, MEDIUM, HIGH). Results are aggregated by `skills/security-audit/aggregate.py`
and reported to the operator.

**What it replaces:** Manual security review of Python code.

---

### 23. semgrep/semgrep

| Field | Details |
|-------|---------|
| **Repository** | [semgrep/semgrep](https://github.com/semgrep/semgrep) |
| **Stars** | ~11,000+ |
| **Description** | Multi-language static analysis tool (SAST). Pattern-based code scanning with support for 30+ languages, custom rules, and autofix suggestions. |
| **License** | LGPL-2.1 |

**Why chosen:** Hermes88 processes repos in many languages (Python, JavaScript, TypeScript,
Go, Rust). Semgrep provides unified security scanning across all of them with a single
tool. Its pattern-based approach (similar to grep but AST-aware) catches vulnerabilities
that simple regex tools miss. The community rule registry provides thousands of
pre-built security rules.

**How it integrates:** Used in the `skills/security-audit/` skill for multi-language
SAST scanning. Semgrep runs with the `p/security-audit` and `p/owasp-top-ten` rule
sets against target repositories. Findings are merged with Bandit results in the
aggregation step. The `--json` output format enables programmatic processing.

**What it replaces:** Language-specific linters for each target language.

---

### 24. trufflesecurity/trufflehog

| Field | Details |
|-------|---------|
| **Repository** | [trufflesecurity/trufflehog](https://github.com/trufflesecurity/trufflehog) |
| **Stars** | ~18,000+ |
| **Description** | Secret scanning tool that searches git history, files, and S3 buckets for leaked credentials. Supports 800+ secret detectors with verification against live APIs. |
| **License** | AGPL-3.0 |

**Why chosen:** Leaked secrets in git history are one of the most common and dangerous
security issues. TruffleHog scans the entire git history (not just current files) to
find accidentally committed API keys, passwords, and tokens. Its verified detection
(checking if a found secret is actually live) reduces false positives dramatically.

**How it integrates:** Used in the `skills/security-audit/` skill for secret detection.
When auditing a repository, TruffleHog scans the full git history and reports any
detected secrets with their commit hash, file path, and secret type. Critical findings
(verified live secrets) trigger immediate operator alerts via Telegram.

**What it replaces:** Manual grep for common secret patterns.

---

### 25. pyupio/safety

| Field | Details |
|-------|---------|
| **Repository** | [pyupio/safety](https://github.com/pyupio/safety) |
| **Stars** | ~1,800+ |
| **Description** | Checks Python dependencies for known security vulnerabilities against a curated database of CVEs. |
| **License** | MIT |

**Why chosen:** Vulnerable dependencies are a major attack vector. Safety checks
requirements.txt and installed packages against a maintained database of Python
package vulnerabilities. It provides clear vulnerability descriptions, affected
version ranges, and recommended fixes.

**How it integrates:** Used in the `skills/security-audit/` skill for dependency
vulnerability scanning. Safety parses `requirements.txt` from target repos and
checks each pinned version against the vulnerability database. Findings include
CVE identifiers and recommended upgrade versions.

**What it replaces:** Manual monitoring of CVE databases.

---

### 26. google/gvisor

| Field | Details |
|-------|---------|
| **Repository** | [google/gvisor](https://github.com/google/gvisor) |
| **Stars** | ~16,000+ |
| **Description** | Application kernel providing container sandboxing via a user-space Linux kernel implementation. Intercepts system calls for defense-in-depth isolation. |
| **License** | Apache-2.0 |

**Why chosen:** Hermes88 executes untrusted code in sandbox containers. Standard Docker
isolation (namespaces + cgroups) is insufficient for truly adversarial code. gVisor
provides an additional isolation layer by intercepting system calls through a user-space
kernel, preventing container escapes and limiting kernel attack surface.

**How it integrates:** Reference architecture for sandbox isolation in
`Dockerfile.sandbox`. The sandbox-manager service (Go) creates ephemeral containers
with resource limits and network isolation. gVisor's runsc runtime is referenced as
the recommended OCI runtime for production deployments where untrusted code execution
is expected.

**What it replaces:** Standard Docker isolation for code execution.

---

## G. Performance & Code Tools (Rust Ecosystem)

High-performance tools that Hermes88 uses for fast code search, linting, and
pattern matching across large codebases.

---

### 27. BurntSushi/ripgrep

| Field | Details |
|-------|---------|
| **Repository** | [BurntSushi/ripgrep](https://github.com/BurntSushi/ripgrep) |
| **Stars** | ~50,000+ |
| **Description** | Fast regex-based search tool that respects .gitignore, handles Unicode, and searches directories recursively. Written in Rust for maximum performance. |
| **License** | Unlicense / MIT (dual-licensed) |

**Why chosen:** Code search is the first step in every bug fix and feature implementation.
ripgrep is orders of magnitude faster than grep/ag for large codebases, respects
.gitignore automatically, and handles binary file detection. OpenClaude uses ripgrep
as its primary code search tool through the MCP filesystem server.

**How it integrates:** Used by OpenClaude (via MCP) for code search during bug fixing
and feature implementation. Also serves as the architecture reference for
`rhodawk-tools/src/search.rs`, which implements a custom parallel file search for
the Rust-based code analysis toolkit. The `rg` binary is installed in all Dockerfiles
that run coding agents.

**What it replaces:** Slow recursive grep over entire project trees.

---

### 28. astral-sh/ruff

| Field | Details |
|-------|---------|
| **Repository** | [astral-sh/ruff](https://github.com/astral-sh/ruff) |
| **Stars** | ~37,000+ |
| **Description** | Extremely fast Python linter and formatter written in Rust. Replaces flake8, isort, pycodestyle, pydocstyle, and dozens of other tools in one binary. |
| **License** | MIT |

**Why chosen:** Ruff demonstrates the pattern of rewriting slow Python tools in Rust
for 100x performance gains. This pattern directly influenced the design of
`rhodawk-tools/`, where performance-critical operations (file scanning, pattern
matching, statistics gathering) are implemented in Rust rather than Python.

**How it integrates:** Reference for parallel file processing patterns in
`rhodawk-tools/src/scanner.rs`. The scanner's approach to parallel directory walking,
file type detection, and batch processing is inspired by ruff's architecture.
Additionally, ruff is used as the Python linter in the CI pipeline
(`.github/workflows/ci.yml`).

**What it replaces:** Slow Python-based linting with flake8/pylint.

---

### 29. ast-grep/ast-grep

| Field | Details |
|-------|---------|
| **Repository** | [ast-grep/ast-grep](https://github.com/ast-grep/ast-grep) |
| **Stars** | ~8,000+ |
| **Description** | Structural code search and replace using AST patterns. Find and transform code by its structure, not text patterns. Supports multiple languages. |
| **License** | MIT |

**Why chosen:** Regular expressions cannot reliably match code patterns (think matching
all function calls with a specific argument pattern). ast-grep matches code by its
abstract syntax tree structure, enabling precise code transformations. This pattern
influenced the `rhodawk-tools/src/analyzer.rs` module which performs structural
code analysis.

**How it integrates:** Reference for pattern matching in `rhodawk-tools/src/analyzer.rs`.
The analyzer's approach to detecting code patterns (unused imports, dead code,
complexity metrics) uses AST-based analysis inspired by ast-grep's structural
matching. Also used as a development tool for bulk code refactoring across the
monorepo.

**What it replaces:** Regex-based code pattern matching that produces false positives.

---

## H. Infrastructure

Core infrastructure services that power Hermes88's runtime environment.

---

### 30. redis/redis

| Field | Details |
|-------|---------|
| **Repository** | [redis/redis](https://github.com/redis/redis) |
| **Stars** | ~68,000+ |
| **Description** | In-memory data structure store used as database, cache, message broker, and streaming engine. Redis Stack adds vector search (RediSearch) and JSON documents (ReJSON). |
| **License** | RSALv2 / SSPLv1 (dual) |

**Why chosen:** Redis serves as the connective tissue of the peak architecture, providing
four critical functions in a single service: (1) event bus via PubSub for inter-component
communication, (2) task queue broker for dramatiq workers, (3) vector cache for fast
memory retrieval, and (4) session storage for conversation state. Using one service for
all four reduces operational complexity on a single-VPS deployment.

**How it integrates:** Deployed as `redis` service in `docker-compose.peak.yml` using
Redis Stack (includes RediSearch + ReJSON). Used by: `rhodawk_core/event_bus.py` (PubSub),
`rhodawk_core/task_engine.py` (task queue broker), `rhodawk_core/memory.py` (vector cache),
`gateway/run.py` (session state). Connection via `REDIS_URL=redis://redis:6379/0`.

**What it replaces:** Multiple separate services (RabbitMQ for queues, memcached for
cache, PostgreSQL for sessions).

---

### 31. traefik/traefik

| Field | Details |
|-------|---------|
| **Repository** | [traefik/traefik](https://github.com/traefik/traefik) |
| **Stars** | ~53,000+ |
| **Description** | Cloud-native reverse proxy and load balancer with automatic HTTPS, Docker integration, and dynamic configuration from container labels. |
| **License** | MIT |

**Why chosen:** The webhook receiver needs to be exposed to the internet for GitHub
webhooks, Stripe webhooks, and other external events. Traefik provides automatic HTTPS
via Let's Encrypt, Docker-native service discovery (reads container labels for routing),
and middleware (rate limiting, authentication) without manual nginx configuration.

**How it integrates:** Reference for webhook routing in the production deployment guide.
When deployed on a VPS with a domain, Traefik sits in front of the webhook-receiver
service, handling TLS termination, rate limiting, and routing. Configuration is via
Docker labels on the `webhook-receiver` service in the compose file.

**What it replaces:** Manual nginx configuration with certbot for HTTPS.

---

### 32. docker/compose

| Field | Details |
|-------|---------|
| **Repository** | [docker/compose](https://github.com/docker/compose) |
| **Stars** | ~34,000+ |
| **Description** | Multi-container application orchestration. Define and run multi-container Docker applications with a single YAML file. |
| **License** | Apache-2.0 |

**Why chosen:** The peak architecture runs 5 containers (hermes, redis, camofox,
webhook-receiver, sandbox-manager) that need coordinated networking, volume sharing,
and dependency ordering. Docker Compose provides declarative orchestration without
the complexity of Kubernetes, appropriate for single-VPS deployment.

**How it integrates:** Primary deployment method via `docker-compose.peak.yml`. All
services are defined with proper dependencies (`depends_on`), health checks, restart
policies, named volumes, and environment configuration. Development and production
use the same compose file with environment-specific `.env` files.

**What it replaces:** Manual docker run commands with complex networking flags.

---

### 33. containerd/containerd

| Field | Details |
|-------|---------|
| **Repository** | [containerd/containerd](https://github.com/containerd/containerd) |
| **Stars** | ~18,000+ |
| **Description** | Industry-standard container runtime. Manages container lifecycle (create, start, stop, delete) and image distribution. |
| **License** | Apache-2.0 |

**Why chosen:** The sandbox-manager service needs to create ephemeral containers
programmatically for code execution tasks. containerd provides the low-level container
runtime API that Docker uses internally, allowing the Go-based sandbox-manager to
create and destroy containers with fine-grained control over resource limits, network
isolation, and filesystem mounts.

**How it integrates:** Used by the sandbox-manager service (`sandbox/`) for ephemeral
container management. The Go sandbox-manager uses the containerd client library to
create short-lived containers for untrusted code execution, with CPU/memory limits,
no network access, and read-only root filesystems. Containers are automatically
destroyed after task completion or timeout.

**What it replaces:** Shelling out to docker CLI for container management.


---

## I. Browser Automation

Stealth browsing capabilities for interacting with websites that detect and block
standard headless browsers.

---

### 34. nicedayfor/AntiBrowserDetect

| Field | Details |
|-------|---------|
| **Repository** | [nicedayfor/AntiBrowserDetect](https://github.com/nicedayfor/AntiBrowserDetect) |
| **Stars** | ~500+ |
| **Description** | Stealth headless browser (Camofox) that evades bot detection. Modified Firefox with anti-fingerprinting patches, realistic TLS signatures, and human-like behavior simulation. |
| **License** | MIT |

**Why chosen:** Many websites use sophisticated bot detection (Cloudflare, DataDome,
PerimeterX) that blocks standard Puppeteer/Playwright headless browsers. Camofox
provides a stealth browsing layer that passes bot detection while maintaining full
automation capabilities. Essential for the competitive-intel and research-deep skills
that need to access protected content.

**How it integrates:** Existing integration as the `camofox` service in
`docker-compose.peak.yml`. The `skills/stealth-browse/camofox_manager.py` manages
browser sessions via Camofox's REST API on port 9377. Sessions include fingerprint
rotation, proxy support, and cookie management. Authentication via `CAMOFOX_ACCESS_KEY`
bearer token.

**What it replaces:** Standard Puppeteer/Playwright that gets blocked by bot detection.

---

### 35. nicedayfor/playwright

| Field | Details |
|-------|---------|
| **Repository** | [nicedayfor/playwright](https://github.com/nicedayfor/playwright) |
| **Stars** | ~500+ |
| **Description** | Fork of Microsoft Playwright optimized for integration with Camofox stealth browser. Provides the automation API layer on top of the stealth browser engine. |
| **License** | Apache-2.0 |

**Why chosen:** Playwright provides the automation API (page navigation, element
interaction, screenshot capture, network interception) while Camofox provides the
stealth engine. This fork ensures compatibility between the automation layer and
the anti-detection patches in Camofox.

**How it integrates:** Referenced by the browser-use integration in
`skills/stealth-browse/`. When browser-use orchestrates web interactions, it uses
Playwright's API to control the Camofox browser instance. The automation commands
(click, type, scroll, screenshot) flow through Playwright to the stealth browser.

**What it replaces:** Standard Playwright with detectable headless fingerprint.

---

## J. Data & Knowledge Graph

Projects that provide graph-based knowledge representation and vector similarity search.

---

### 36. neo4j/neo4j

| Field | Details |
|-------|---------|
| **Repository** | [neo4j/neo4j](https://github.com/neo4j/neo4j) |
| **Stars** | ~14,000+ |
| **Description** | Native graph database with Cypher query language. Provides ACID transactions, graph algorithms, and full-text search over connected data. |
| **License** | GPL-3.0 (Community) |

**Why chosen:** Knowledge naturally forms a graph: projects connect to people, decisions
relate to outcomes, research links to conclusions. Neo4j's graph model represents these
relationships natively. While the current implementation uses in-memory SQLite relations
(appropriate for single-VPS scale), neo4j's patterns for knowledge graph construction
informed the memory engine's relation storage design.

**How it integrates:** Reference architecture for knowledge graph patterns in
`rhodawk_core/memory.py`. The memory engine's relation storage (entity -> relation ->
entity triples) follows neo4j's property graph model. Relations are stored in SQLite
with graph traversal queries. The Obsidian vault's bidirectional links mirror neo4j's
relationship model in a human-readable format.

**What it replaces:** Flat key-value storage with no relationship modeling.

---

### 37. qdrant/qdrant

| Field | Details |
|-------|---------|
| **Repository** | [qdrant/qdrant](https://github.com/qdrant/qdrant) |
| **Stars** | ~22,000+ |
| **Description** | High-performance vector similarity search engine written in Rust. Provides filtering, payload storage, and horizontal scaling for vector search workloads. |
| **License** | Apache-2.0 |

**Why chosen:** Qdrant represents the next step for vector search when ChromaDB's
embedded mode reaches scale limits. Written in Rust for performance, Qdrant provides
advanced filtering (combine vector similarity with metadata filters), payload storage,
and horizontal scaling. Referenced as the upgrade path for memory vector search.

**How it integrates:** Alternative to ChromaDB (reference) in `rhodawk_core/memory.py`.
The memory engine's vector search interface is designed to be backend-agnostic --
ChromaDB for development/small-scale, Qdrant for production/large-scale. The switch
requires only changing the vector backend configuration in `hermes_config/config.yaml`.

**What it replaces:** N/A (future upgrade path from ChromaDB).

---

## K. Voice & Media

Projects that enable Hermes88 to process voice messages and other media content.

---

### 38. openai/whisper

| Field | Details |
|-------|---------|
| **Repository** | [openai/whisper](https://github.com/openai/whisper) |
| **Stars** | ~75,000+ |
| **Description** | General-purpose speech recognition model. Supports multilingual transcription, translation, and language identification with high accuracy across diverse audio conditions. |
| **License** | MIT |

**Why chosen:** Voice messages are a primary communication method on Telegram (many
users prefer voice over typing). Whisper provides state-of-the-art speech-to-text
that handles accents, background noise, and multiple languages. The model runs
efficiently even on CPU, making it suitable for single-VPS deployment without GPU.

**How it integrates:** Used by the `skills/voice-transcription/` skill. When the
operator sends a voice message via Telegram, the gateway downloads the audio file
and passes it to the transcription skill. Whisper converts speech to text, which
is then processed as a regular text message through the normal pipeline.

**What it replaces:** Third-party transcription APIs with per-minute pricing.

---

### 39. ggerganov/whisper.cpp

| Field | Details |
|-------|---------|
| **Repository** | [ggerganov/whisper.cpp](https://github.com/ggerganov/whisper.cpp) |
| **Stars** | ~36,000+ |
| **Description** | High-performance C/C++ port of OpenAI's Whisper model. Optimized inference with SIMD, Metal, and CUDA support. Runs 4-8x faster than Python Whisper on CPU. |
| **License** | MIT |

**Why chosen:** The Python Whisper implementation is slow on CPU (the typical VPS has
no GPU). whisper.cpp provides the same accuracy at 4-8x speed through C++ optimization
and SIMD instructions. This makes real-time voice transcription feasible on a standard
VPS without GPU acceleration.

**How it integrates:** Reference for local voice processing optimization in
`skills/voice-transcription/transcribe.py`. The transcription skill is designed to
use either the Python Whisper API (for simplicity) or whisper.cpp (for performance)
based on available resources. Production deployments use whisper.cpp for sub-second
transcription of typical voice messages.

**What it replaces:** Slow Python-only inference on CPU.

---

## License Compatibility

All integrated projects are evaluated for license compatibility with Rhodawk AI's
deployment model (private SaaS, not distributed as source code).

| # | Project | License | Compatible | Notes |
|---|---------|---------|:----------:|-------|
| 1 | mem0 | Apache-2.0 | Yes | Permissive, no concerns |
| 2 | ChromaDB | Apache-2.0 | Yes | Permissive, no concerns |
| 3 | LlamaIndex | MIT | Yes | Most permissive |
| 4 | Obsidian | Proprietary (app) | Yes | Vault format is plain Markdown |
| 5 | obsidian-dataview | MIT | Yes | Most permissive |
| 6 | LiteLLM | MIT | Yes | Most permissive |
| 7 | pydantic-ai | MIT | Yes | Most permissive |
| 8 | AutoGen | MIT | Yes | Most permissive |
| 9 | CrewAI | MIT | Yes | Most permissive |
| 10 | MCP Servers | MIT | Yes | Most permissive |
| 11 | browser-use | MIT | Yes | Most permissive |
| 12 | Scrapegraph-ai | MIT | Yes | Most permissive |
| 13 | Jina Reader | Apache-2.0 | Yes | Permissive, no concerns |
| 14 | openai-python | Apache-2.0 | Yes | Permissive, no concerns |
| 15 | dramatiq | LGPL-3.0 | Caution | OK for network use, not for bundled distribution |
| 16 | Celery | BSD-3-Clause | Yes | Permissive, no concerns |
| 17 | Prefect | Apache-2.0 | Yes | Permissive, no concerns |
| 18 | APScheduler | MIT | Yes | Most permissive |
| 19 | python-telegram-bot | LGPL-3.0 | Caution | OK for network use, not for bundled distribution |
| 20 | AutoGPT | MIT | Yes | Most permissive |
| 21 | OpenHands | MIT | Yes | Most permissive |
| 22 | Bandit | Apache-2.0 | Yes | Permissive, no concerns |
| 23 | Semgrep | LGPL-2.1 | Caution | OK as external tool, not linked |
| 24 | TruffleHog | AGPL-3.0 | Caution | OK as external CLI tool, network use is fine |
| 25 | Safety | MIT | Yes | Most permissive |
| 26 | gVisor | Apache-2.0 | Yes | Permissive, no concerns |
| 27 | ripgrep | MIT/Unlicense | Yes | Most permissive |
| 28 | Ruff | MIT | Yes | Most permissive |
| 29 | ast-grep | MIT | Yes | Most permissive |
| 30 | Redis | RSALv2/SSPLv1 | Yes | OK as separate service (not embedding/distributing) |
| 31 | Traefik | MIT | Yes | Most permissive |
| 32 | Docker Compose | Apache-2.0 | Yes | Permissive, no concerns |
| 33 | containerd | Apache-2.0 | Yes | Permissive, no concerns |
| 34 | Camofox | MIT | Yes | Most permissive |
| 35 | Playwright (fork) | Apache-2.0 | Yes | Permissive, no concerns |
| 36 | Neo4j | GPL-3.0 | Caution | Reference only, not deployed (Community edition) |
| 37 | Qdrant | Apache-2.0 | Yes | Permissive, no concerns |
| 38 | Whisper | MIT | Yes | Most permissive |
| 39 | whisper.cpp | MIT | Yes | Most permissive |

### License Notes

- **MIT / Apache-2.0 / BSD**: Fully permissive. No restrictions on use, modification,
  or distribution. These are the preferred licenses for Rhodawk AI integrations.

- **LGPL-3.0** (dramatiq, python-telegram-bot): The LGPL permits use in proprietary
  applications as long as the LGPL-covered library is not modified. Since we use these
  as unmodified dependencies via pip, there are no licensing concerns for SaaS deployment.

- **LGPL-2.1** (semgrep): Used as an external CLI tool invoked via subprocess. No
  linking or code modification. Compatible with SaaS use.

- **AGPL-3.0** (TruffleHog): The AGPL requires source disclosure if the software is
  offered as a network service. Since TruffleHog is used as a local CLI tool (not
  exposed as a service to users), there are no AGPL obligations.

- **RSALv2/SSPLv1** (Redis): Redis's new license restricts offering Redis-as-a-service
  to compete with Redis Ltd. Since we use Redis as an internal component (not offering
  Redis hosting), there are no restrictions.

- **GPL-3.0** (Neo4j Community): Reference architecture only. Not deployed or linked.
  If deployed in the future, would use the Community edition as a separate service.

---

## Total Development Value

The 39 open-source projects integrated into Rhodawk AI Hermes88 represent:

- **Combined GitHub stars:** 1,000,000+
- **Combined contributors:** 15,000+
- **Combined years of development:** 100+
- **Estimated development value:** $45+ million

This value represents decades of engineering effort from thousands of developers across
the global open-source community. By standing on these shoulders, Rhodawk AI achieves
JARVIS-grade capability without the JARVIS-grade budget.

---

## Contribution Back

Rhodawk AI is committed to contributing back to the open-source projects that make
Hermes88 possible:

1. **Bug reports and fixes** -- Issues discovered during integration are reported upstream
2. **Documentation improvements** -- Gaps found during integration are documented
3. **Performance findings** -- Benchmarks and optimization discoveries are shared
4. **Integration examples** -- Real-world usage patterns are contributed as examples
5. **Financial support** -- Open-source sponsors for critical dependencies

---

*Rhodawk AI -- Built on Open Source, Building for the Future*

*Last updated: 2025*

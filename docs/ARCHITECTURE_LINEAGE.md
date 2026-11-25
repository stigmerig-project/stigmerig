# StigmeRig Architecture Lineage

This document records the research, talks, and repositories behind StigmeRig design decisions.
Each entry traces an architectural constraint or CLI flag to its public source.

## 1. Theoretical and Macroeconomic Foundations

StigmeRig applies six foundational principles of coordination and software engineering.

- **Stigmergy**: Pierre-Paul Grasse (1959, *Insectes Sociaux* `6(1)`) showed indirect coordination through shared environment state.
- **Parallel Limits**: Gene M. Amdahl (1967, *AFIPS* `30`) proved serial bottlenecks cap parallel speedup.
- **Communication Overhead**: Fred Brooks (1975, *The Mythical Man-Month*, `ISBN 978-0201835953`) showed quadratic coordination cost with team size.
- **Essential Complexity**: Fred Brooks (1987, *No Silver Bullet*, `DOI: 10.1109/MC.1987.1663532`) separated essential logic from accidental tooling.
- **Theory of Constraints**: Eliyahu Goldratt (*The Goal*) established that local speedups fail unless system throughput improves.
- **Code as Design**: Jack W. Reeves (1992, *What Is Software Design?*) defined source code as the engineering design artifact.

Macroeconomic and field studies ground StigmeRig verification gates against unmeasured automation claims.

- **J-Curve Lag**: Erik Brynjolfsson, Daniel Rock, and Chad Syverson (2021, *The Productivity J-Curve*, `DOI: 10.1257/mac.20180386`) modeled productivity dips during restructuring.
- **Brownfield Slowdown**: METR (2025/2026 brownfield RCT) measured slower delivery when AI agents skip codebase verification.
- **Documentation Drag**: Rotenstein et al. (*JAMA*, 2025) and Murray (*JAMIA Open*, 2025) documented review burden from verbose machine output.
- **Safety Reset**: Amazon 90-Day Code Safety Reset (2026) reinforced mandatory human review gates and rollback discipline.

Four engineering papers and essays shaped the execution architecture directly.

- **Interpretable Context**: Jake Van Clief and David McDermott ([arXiv:2603.16021](https://arxiv.org/abs/2603.16021 "paper")) formalized structured context layers over raw prompt stuffing.
- **Workflow Rails**: Anthropic Engineering ([Building Effective Agents](https://www.anthropic.com/research/building-effective-agents "essay")) favored deterministic workflows over unconstrained autonomous loops.
- **Factory Limits**: Rob West ([The Agentic Software Factory Is the Wrong Metaphor](https://robwest.info/articles/the-agentic-software-factory-is-the-wrong-metaphor/ "essay")) critiqued blind assembly-line analogies for software.
- **Value Streams**: CMU SEI ([DevSecOps Value Stream Mapping](https://www.sei.cmu.edu/blog/taking-devsecops-to-the-next-level-with-value-stream-mapping/ "blog")) mapped lead time and verification waste across delivery stages.

## 2. Practitioner Talks and Ecosystem Lineage

The table below maps practitioner talks and reference repositories to StigmeRig modules and invariants.

| Source | References | StigmeRig Mapping |
| --- | --- | --- |
| OpenRig (`@FeralMachine` / Marcus Schwarz) | [Repo](https://github.com/mvschwarz/openrig "repo"), [Talk 1](https://youtu.be/qccsfUnrQ9w "talk"), [Talk 2](https://youtu.be/CYRJDTcTKhQ "talk"), [Talk 3](https://www.youtube.com/watch?v=vndsXRBPGio "talk"), [Talk 4](https://www.youtube.com/watch?v=yCFSRnPDFqY "talk") | `--rig-dir`, Anti-DMV ratios (`verify_only_ratio <= 0.30`), `refocus`, workflow rails |
| Jake Van Clief (ICM) | [Talk 1](https://www.youtube.com/watch?v=uYRXYsPS66g "talk"), [Talk 2](https://www.youtube.com/watch?v=MkN-ss2Nl10 "talk"), [Short](https://youtube.com/shorts/nnQcgv2rRhU "short") | `stigmerig init`, SHA-256 `--sync-icm`, wave concurrency caps |
| Nate B Jones | [Talk 1](https://www.youtube.com/watch?v=NIRFVjttNvA "talk"), [Talk 2](https://www.youtube.com/watch?v=QSK4vf_ZTRA "talk"), [Talk 3](https://www.youtube.com/watch?v=dxq7WtWxi44 "talk") | Local-first rails, execution-verification separation |
| Lauren Tan (SpaceXSI) & `pstack` | [Talk 1](https://youtu.be/O_zcc6Xl12g "talk"), [SKILL.md](https://github.com/cursor/plugins/blob/main/pstack/skills/create-formal-verification-skill/SKILL.md "skill"), [Talk 2](https://www.youtube.com/watch?v=bQCBfcZZj0Y "talk") | `stigmerig traps`, `--baseline`, `vacuous_test_warn`, Axis 4 rubric |
| Rody Davis (Google Cloud Tech) | [Talk](https://youtu.be/EFejzwBLAVo "talk") | `@remove-when` `TRAP-EVG-001`, `platform_targets`, `net_loc_delta` |
| Brandon Thomas (ArtCraft) | [Talk](https://youtu.be/UCL4K36SHp0 "talk") | `TRAP-RS-003` and `TRAP-RS-004`, boolean readiness predicates, Tier 1 focus |
| Girish Konda (Microsoft) | [Talk](https://www.youtube.com/watch?v=DxP5aW4VknQ "talk") | CAS fencing leases, `unknown` and timeout recovery |
| Melvin Johnson (Google DeepMind) | [Talk](https://www.youtube.com/watch?v=fTHByDwTxGM "talk") | 4-axis `HIGH_FRICTION_PR_RUBRIC`, `scope_hygiene_warn` |
| Zach Lloyd & Ben Holmes (Warp) | [Talk 1](https://youtu.be/nKjPBAw_N_0 "talk"), [Talk 2](https://www.youtube.com/watch?v=0LeT996Hz3E "talk"), [Talk 3](https://www.youtube.com/watch?v=pz74r-4dGfQ "talk") | Offline telemetry, blocker trap deduplication |
| Nouha Dziri (Cohere) | [Talk](https://www.youtube.com/watch?v=maitMe9HCvw "talk") | Early planning bursts, `archetype_models`, `tier_overprovisioned_alert` |
| Garry Tan (GStack) | [Talk](https://www.youtube.com/watch?v=wkv2ifxPpF8 "talk") | Thin execution runner, specialized archetypes |
| Design Engineering | [Talk](https://www.youtube.com/watch?v=4Oq_gdhjYUw "talk") | Separating discovery spikes from `WaveRunner` |
| Reference Repos | [ecc](https://github.com/affaan-m/ecc "repo"), [lacuna-research-mcp](https://github.com/tiptreesystems/lacuna-research-mcp "repo") | Isolated git worktrees (`ecc`), automated trap harvesting (`lacuna-research-mcp`) |

## 3. Adjudicated Design Trade-Offs (Adopt / Adapt / Defer / Reject)

StigmeRig classifies external ideas into four explicit architectural dispositions.
Each decision balances single-operator simplicity against deterministic verification cost.

### Adopt

StigmeRig adopts five core primitives directly into the local execution loop.

- **Shared Board Coordination**: Workers read and write deterministic local task state instead of conversational relay.
- **Thin Execution Runner**: Specialized worker archetypes run under strict concurrency caps and scoped context.
- **Deterministic Hygiene Gates**: Automated checks enforce `verify_only_ratio <= 0.30`, `net_loc_delta`, and `scope_hygiene_warn`.
- **Lease Fencing**: Compare-and-swap fencing tokens protect task claims and recover from `unknown` timeout states.
- **Worktree Isolation**: Parallel workers use isolated git worktrees before merging verified diffs.

### Adapt

StigmeRig adapts external verification and model-routing patterns for local CLI workflows.

- **Verification Rubrics**: StigmeRig adapts `pstack` checks into lightweight `stigmerig traps --baseline` and `vacuous_test_warn` rules.
- **Model Tiering**: Early planning bursts use stronger models while routine leaf checks flag `tier_overprovisioned_alert`.
- **Trap Lifecycle**: Blocker deduplication and `@remove-when` (`TRAP-EVG-001`) keep rule sets compact across releases.
- **Discovery Isolation**: Exploratory spikes run outside `WaveRunner` before formal task decomposition begins.
- **Friction Scoring**: StigmeRig applies the 4-axis `HIGH_FRICTION_PR_RUBRIC` locally without remote PR bots.

### Defer

StigmeRig defers large-scale distributed features that exceed single-operator local rig needs.

- **Large-Team Cloud Replay**: Deterministic replay for `>10` developers stays out of scope for local single-operator rigs.
- **High-Volume Issue Daemons**: Autonomous triage daemons for `1,000+` external issues remain deferred.
- **External Formal Provers**: Standalone SMT and TLA+ theorem provers stay deferred beyond `--baseline` trap assertions.
- **Multi-Region Task Brokers**: Distributed consensus clusters remain unnecessary for single-machine repositories.

### Reject

StigmeRig explicitly rejects high-drift patterns that weaken deterministic human review.

- **Peer-Chat Swarms and Video-Only Approvals**: Unstructured agent chatter hides defects and inflates review friction.
- **Peer-Chat Rejection Sources**: See [Reject 1](https://www.youtube.com/watch?v=U92J5mCDLMc "talk"), [Reject 2](https://www.youtube.com/watch?v=BTtyguoPvBU "talk"), and [Reject 3](https://www.youtube.com/watch?v=fTUinMqA1XQ "talk") for rejected patterns.
- **Autonomous `AGENTS.md` Prose Mutation**: Self-modifying instruction files cause policy drift and unreviewed regressions.
- **Mutation Rejection Sources**: See [Reject 4](https://www.youtube.com/watch?v=oeknLUsN_GE "talk"), [Reject 5](https://www.youtube.com/watch?v=Xw9utONfhIY "talk"), [Reject 6](https://www.youtube.com/watch?v=vlfCjWuv4es "talk"), and [Reject 7](https://www.youtube.com/watch?v=yzx4IuEh_tU "talk") for rejected patterns.

## 4. Attribution and Security Scope Note

Cited papers, talks, and repositories represent independent intellectual lineage rather than endorsement by any author.
Reference links document design inspiration and rejected patterns only.

StigmeRig operates as a cooperative local CLI tool over repository files and local task state.

- **Local Trust Boundary**: All task leases, trap baselines, and telemetry remain inside the user workspace.
- **No Remote Orchestrator**: StigmeRig runs without remote control planes or background cloud daemons.
- **Cooperative Execution**: Safety relies on explicit CLI commands, deterministic file locks, and human review gates.
- **Execution Separation**: Verification checks run separately from worker edits to preserve independent review evidence.
- **Scope Boundary**: This architecture makes no external compliance or multi-tenant isolation claims.

//! Record a source-linked claim and hand it to a reviewer for a source recheck.
mod common;

use clickhouse_loom::{
    CallOptions, Client, CodeIndexStatusInput, CodeSymbolInput, Integer, MemorySetInput,
    RunBeginInput, RunCompleteInput, RunCompleteInputOutcome, RunCompleteInputStatus,
    TaskClaimInput, TaskCompleteInput, TaskCreateInput, TaskGetInput, TaskHeartbeatInput,
    TaskPayloadStatus,
};
use common::{received, require, Args, Result};
use serde::Serialize;
use serde_json::json;
use std::{collections::HashMap, env};

const IMPLEMENTER: &str = "factory-implementation";
const REVIEWER: &str = "factory-reviewer";

fn commit_valid(commit: &str) -> bool {
    commit.len() == 40
        && commit
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn validate(args: &Args) -> Result<()> {
    for field in [
        "org",
        "memory-namespace",
        "code-namespace",
        "repo",
        "commit",
        "issue-url",
        "symbol",
        "repo-tag",
        "review-inbox",
        "lesson",
    ] {
        args.required(field)?;
    }
    let repo = args.required("repo")?;
    let parts: Vec<_> = repo.split('/').collect();
    require(
        parts.len() == 2
            && parts.iter().all(|part| {
                !part.is_empty()
                    && part
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
            }),
        "Use owner/repo",
    )?;
    require(
        commit_valid(args.required("commit")?),
        "Use a full commit SHA",
    )?;
    let issue = args.required("issue-url")?;
    let prefix = format!("https://github.com/{repo}/issues/");
    let number = issue
        .strip_prefix(&prefix)
        .ok_or("Use an issue URL for this repository")?;
    require(
        number.starts_with(|c: char| ('1'..='9').contains(&c))
            && number.bytes().all(|c| c.is_ascii_digit()),
        "Use a public issue URL",
    )?;
    let tag = args
        .required("repo-tag")?
        .strip_prefix("repo:")
        .ok_or("Use the repository's recorded tag")?;
    require(
        !tag.is_empty()
            && tag
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c)),
        "Invalid repository tag",
    )?;
    require(
        args.required("lesson")?.chars().count() <= 1200,
        "Keep the claim within 1200 characters",
    )
}

#[derive(Serialize)]
struct SourceEvidence {
    repo: String,
    commit: String,
    symbol: String,
    tier: String,
    path: String,
    returned_body_sha256: String,
}

async fn source(code: &Client, args: &Args) -> Result<SourceEvidence> {
    let before = received(
        code.code_index_status_with_options(
            CodeIndexStatusInput::new().brief(true),
            CallOptions::default(),
        )
        .await?,
    );
    let commit = &before.git_head;
    require(
        before.repo == args.required("repo")? && commit_valid(commit) && !before.fetch_overdue,
        "The index repository, commit or fetch freshness differs",
    )?;
    for tier in &before.compiler_tiers {
        require(
            tier.commit == *commit,
            "A compiler tier is at another commit",
        )?;
    }
    let result = received(
        code.code_symbol_with_options(
            CodeSymbolInput::new()
                .name(args.required("symbol")?)
                .include_body(true)
                .include_history(false)
                .include_examples(false)
                .include_lessons(false),
            CallOptions::default(),
        )
        .await?,
    );
    let definitions = &result.definitions;
    require(
        !result.ambiguous && definitions.len() == 1,
        "Resolve one definition",
    )?;
    let unit = &definitions[0];
    require(
        unit.symbol == args.required("symbol")? && !unit.elided && unit.granularity == "unit",
        "The symbol body is incomplete or differs",
    )?;
    require(
        !unit.body.is_empty() && !unit.path.is_empty(),
        "Missing symbol body or path",
    )?;
    let after = received(
        code.code_index_status_with_options(
            CodeIndexStatusInput::new().brief(true),
            CallOptions::default(),
        )
        .await?,
    );
    require(
        after.repo == before.repo && after.git_head == *commit,
        "The index advanced during the read",
    )?;
    Ok(SourceEvidence {
        repo: args.required("repo")?.into(),
        commit: commit.clone(),
        symbol: args.required("symbol")?.into(),
        tier: if unit.tier.is_empty() {
            "unknown".into()
        } else {
            unit.tier.clone()
        },
        path: unit.path.clone(),
        returned_body_sha256: common::sha256(&unit.body),
    })
}

async fn run() -> Result<()> {
    let Some(args) = Args::parse("software_factory_memory --org ORG --memory-namespace NS --code-namespace NS --repo owner/repo --commit SHA --issue-url URL --symbol SYMBOL --repo-tag repo:TAG --review-inbox INBOX --lesson CLAIM; requires distinct LOOM_IMPLEMENTATION_API_KEY and LOOM_REVIEW_API_KEY", &["org", "memory-namespace", "code-namespace", "repo", "commit", "issue-url", "symbol", "repo-tag", "review-inbox", "lesson"])? else { return Ok(()); };
    validate(&args)?;
    let implementation_key = env::var("LOOM_IMPLEMENTATION_API_KEY")?;
    let review_key = env::var("LOOM_REVIEW_API_KEY")?;
    require(
        !implementation_key.is_empty()
            && !review_key.is_empty()
            && implementation_key != review_key,
        "Use distinct credentials",
    )?;
    let org = args.required("org")?;
    let namespace = args.required("memory-namespace")?;
    let code_ns = args.required("code-namespace")?;
    let implementer = common::client(
        org,
        namespace,
        IMPLEMENTER,
        "-",
        "-",
        Some(&implementation_key),
    )?;
    let reviewer = common::client(org, namespace, REVIEWER, "-", "-", Some(&review_key))?;
    let implementation_code = common::client(
        org,
        code_ns,
        IMPLEMENTER,
        "-",
        "-",
        Some(&implementation_key),
    )?;
    let review_code = common::client(org, code_ns, REVIEWER, "-", "-", Some(&review_key))?;
    let baseline = source(&implementation_code, &args).await?;
    require(
        baseline.commit == args.required("commit")?,
        "The index is not at the requested commit",
    )?;
    let key = format!("example/software-factory/{}", uuid::Uuid::new_v4());
    let source_url = format!(
        "https://github.com/{}/blob/{}/{}",
        args.required("repo")?,
        args.required("commit")?,
        baseline.path
    );
    let note = json!(format!(
        "Operator-supplied review claim about `{}`: {}\nIssue: {}\nSource: {}\nReturned body SHA256: {}\nThis example records the claim and source, without verifying the claim.",
        args.required("symbol")?, args.required("lesson")?, args.required("issue-url")?, source_url, baseline.returned_body_sha256
    ));
    let opened = received(
        implementer
            .run_begin_with_options(
                RunBeginInput::new("Collect source evidence and hand it to review"),
                CallOptions::default(),
            )
            .await?,
    );
    require(!opened.run_id.is_empty(), "Missing run ID")?;
    let run_id = &opened.run_id;
    let mut task_id = String::new();
    let mut fence = Integer::default();
    let mut owned = Vec::new();
    let mut complete_attempted = false;
    let result: Result<()> = async {
        let written = common::write_owned(
            &implementer,
            MemorySetInput::new(&key, note.clone()).run_id(run_id).tags([args.required("repo-tag")?])
                .source_class("derived").source_resource_id(&source_url).confidence(0.5),
            &mut owned,
        ).await?;
        let id = &written.memory_id;
        require(!written.content_sha256.is_empty(), "Missing write receipt hash")?;
        common::read_owned(&implementer, id, &key, IMPLEMENTER, "process:factory-implementation", "derived", &note).await?;
        let brief = HashMap::from([
            ("issue_url".into(), json!(args.required("issue-url")?)),
            ("memory_key".into(), json!(key)),
            ("memory_id".into(), json!(id)),
            ("source".into(), serde_json::to_value(&baseline)?),
        ]);
        let created = received(
            implementer.task_create_with_options(
                TaskCreateInput::new(args.required("review-inbox")?, "review", "Recheck a source-linked implementation note")
                    .brief(brief.clone()).idempotency_key(&key),
                CallOptions::default(),
            ).await?,
        );
        let task = &created.task;
        require(!task.task_id.is_empty(), "Missing review task ID")?;
        task_id = task.task_id.clone();
        fence = task.fence.clone();
        require(task.from_subject == IMPLEMENTER && task.status == TaskPayloadStatus::Todo, "The review task needs the expected posting subject and admission")?;
        let claimed = received(reviewer.task_claim_with_options(
            TaskClaimInput::new(&task_id), CallOptions::default(),
        ).await?);
        fence = claimed.task.fence.clone();
        require(claimed.task.owner == REVIEWER && claimed.task.last_actor == "process:factory-reviewer" && claimed.task.brief == brief, "The review lease or handoff evidence differs")?;
        received(reviewer.task_heartbeat_with_options(
            TaskHeartbeatInput::new(&task_id, fence.clone()), CallOptions::default(),
        ).await?);
        common::read_owned(&reviewer, id, &key, IMPLEMENTER, "process:factory-implementation", "derived", &note).await?;
        let current = source(&review_code, &args).await?;
        let unchanged = current.path == baseline.path && current.symbol == baseline.symbol
            && current.returned_body_sha256 == baseline.returned_body_sha256;
        let state = if unchanged { "unchanged_body" } else { "changed_body_needs_review" };
        let outcome = HashMap::from([
            ("source_recheck".into(), json!(state)),
            ("source".into(), serde_json::to_value(&current)?),
            ("memory_key".into(), json!(key)),
            ("claim_truth".into(), json!("not_verified")),
            ("code_review".into(), json!("not_performed")),
            ("ci".into(), json!("not_run")),
        ]);
        received(reviewer.task_complete_with_options(
            TaskCompleteInput::new(&task_id, fence.clone()).outcome(outcome.clone()),
            CallOptions::default(),
        ).await?);
        let finished = received(implementer.task_get_with_options(
            TaskGetInput::new(&task_id), CallOptions::default(),
        ).await?);
        require(finished.found, "The completed task is missing")?;
        let finished = finished.task.as_ref().ok_or("Missing completed task payload")?;
        require(finished.status == TaskPayloadStatus::Done
            && finished.last_actor == "process:factory-reviewer"
            && finished.outcome == outcome, "The completed task lost its reported outcome")?;
        complete_attempted = true;
        received(implementer.run_complete_with_options(
            RunCompleteInput::new(run_id, RunCompleteInputStatus::Completed)
                .outcome(RunCompleteInputOutcome::None).memories_used(Vec::<String>::new())
                .metadata(HashMap::from([
                    ("source_recheck".into(), json!(state)),
                    ("code_review".into(), json!("not_performed")),
                    ("ci".into(), json!("not_run")),
                ])),
            CallOptions::default(),
        ).await?);
        println!("{}", json!({"source_recheck": state, "claim_truth": "not_verified", "merge_ready": false, "source_tier": current.tier}));
        Ok(())
    }.await;
    let run_cleanup = if complete_attempted {
        Ok(())
    } else {
        implementer
            .run_complete_with_options(
                RunCompleteInput::new(run_id, RunCompleteInputStatus::Failed)
                    .outcome(RunCompleteInputOutcome::None),
                CallOptions::default(),
            )
            .await
            .map(|result| {
                received(result);
            })
    };
    let task_cleanup = if task_id.is_empty() {
        Ok(())
    } else {
        common::cancel_owned(
            &implementer,
            &reviewer,
            &task_id,
            &fence,
            IMPLEMENTER,
            REVIEWER,
        )
        .await
    };
    let memory_cleanup = common::cleanup_memories(&implementer, &owned).await;
    result?;
    run_cleanup?;
    task_cleanup?;
    memory_cleanup?;
    require(
        !implementer.get(&key).await?.found,
        "The generated note remains readable",
    )?;
    println!(
        "{}",
        json!({"cleanup": "generated_memory_missing", "history": "retained"})
    );
    Ok(())
}

#[tokio::main]
async fn main() {
    common::finish(run().await);
}

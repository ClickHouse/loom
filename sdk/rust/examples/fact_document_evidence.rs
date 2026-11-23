//! Read a fact and a caller-selected section without evaluating their relationship.
mod common;

use clickhouse_loom::{KnowledgeOutlineInput, MemoryGetInput};
use common::{require, Args, Result};
use serde_json::json;

async fn run() -> Result<()> {
    let Some(args) = Args::parse("fact_document_evidence --org ORG --namespace NS --agent AGENT --memory-key KEY --source-uri URI --section HEADING [CHILD ...] [--user-id USER] [--session-id SESSION]", &["org", "namespace", "agent", "memory-key", "source-uri", "section", "user-id", "session-id"])? else { return Ok(()); };
    let section = args.many("section");
    require(
        !section.is_empty() && section.iter().all(|part| !part.trim().is_empty()),
        "Select a nonempty section path",
    )?;
    let agent = args.required("agent")?;
    let user = args.optional("user-id").unwrap_or("-");
    let session = args.optional("session-id").unwrap_or("-");
    let bot = common::client(
        args.required("org")?,
        args.required("namespace")?,
        agent,
        user,
        session,
        None,
    )?;
    let fields = [
        "agent",
        "user_id",
        "session_id",
        "value",
        "version",
        "status",
        "actor",
        "source_class",
        "source_trace",
        "source_resource_id",
    ];
    let fact = bot
        .memory_get(MemoryGetInput::new(args.required("memory-key")?).fields(fields))
        .await?;
    if fact.found {
        require(
            fact.agent == agent && fact.user_id == user && fact.session_id == session,
            "The fact differs from the selected identity",
        )?;
    }
    let passage = bot
        .knowledge_outline(
            KnowledgeOutlineInput::new(args.required("source-uri")?)
                .section_path(section)
                .include_children(false),
        )
        .await?;
    println!(
        "{}",
        json!({
            "fact_found": fact.found,
            "section_found": passage.found && passage.section_content.is_some(),
            "fact_fields_selected": fields,
            "section_fields_selected": ["source_uri", "section_content", "section_chunk_idxs"],
            "relationship": "caller_selected", "support": "not_evaluated"
        })
    );
    Ok(())
}

#[tokio::main]
async fn main() {
    common::finish(run().await);
}

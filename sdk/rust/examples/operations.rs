use std::{env, error::Error};

use clickhouse_loom::{
    CallOptions, Client, ContextPackInput, KnowledgeAddInput, KnowledgeDeleteInput,
    KnowledgeGetInput, KnowledgeSearchInput, KnowledgeSearchInputNamespaceScope,
    KnowledgeSearchInputSearchMode, OpsCapabilitiesInput, OpsHealthInput, SearchRequest,
    SetRequest,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let org = env::var("LOOM_ORG").unwrap_or_else(|_| "dev".into());
    let namespace = env::var("LOOM_NAMESPACE").unwrap_or_else(|_| "local".into());
    let client = Client::builder(org, namespace, "rust-example")
        .user_id("example-user")
        .session_id("example-session")
        .build()?;
    let capabilities = client.capabilities(OpsCapabilitiesInput::new()).await?;
    for required in [
        "memory.set",
        "memory.get",
        "memory.search",
        "memory.delete",
        "knowledge.add",
        "knowledge.get",
        "knowledge.search",
        "knowledge.delete",
        "context.pack",
        "ops.health",
    ] {
        if !capabilities.ops.iter().any(|op| op.name == required) {
            return Err(format!("server does not advertise {required}").into());
        }
    }
    println!("schema: {}", capabilities.schema_version);
    if env::var("LOOM_CHECK_HEALTH").as_deref() == Ok("1") {
        println!(
            "health: {:?}",
            client.health(OpsHealthInput::new()).await?.overall
        );
    }

    let key = format!("rust-example-{}", uuid::Uuid::new_v4());
    let uri = format!("urn:loom:example:{key}");
    let value = format!("The example task {key} uses concise answers.");
    let mut memory_created = false;
    let mut knowledge_attempted = false;
    let outcome: Result<(), Box<dyn Error>> = async {
        let saved = client
            .set_with_options(
                SetRequest::new(&key, value.as_str())
                    .memory_type("semantic")
                    .ttl_seconds(300),
                CallOptions::default().request_id("example-save"),
            )
            .await?;
        if saved.data.noop {
            return Err("memory merged with an existing row; use a fresh namespace".into());
        }
        memory_created = true;
        println!(
            "set: version={} request={} attempts={}",
            saved.data.version, saved.metadata.request_id, saved.metadata.attempts
        );
        let memory = client.get(&key).await?;
        if !memory.found || memory.value != value {
            return Err("memory read did not match the write".into());
        }
        let recall = client
            .search(
                SearchRequest::new(&key)
                    .search_mode("lexical")
                    .namespace_scope("this")
                    .agent_id("rust-example"),
            )
            .await?;
        println!(
            "memory search: hits={} abstain={} degraded={:?}",
            recall.results.len(),
            recall.abstain,
            recall.degraded
        );

        knowledge_attempted = true;
        let doc = client
            .knowledge_add(
                KnowledgeAddInput::new(&uri, &value)
                    .title("Example task guide")
                    .tags(["example"]),
            )
            .await?;
        println!(
            "knowledge add: doc={} chunks={}",
            doc.doc_id, doc.chunks_created
        );
        let fetched = client.knowledge_get(KnowledgeGetInput::new(&uri)).await?;
        if !fetched.found || fetched.content.as_deref() != Some(value.as_str()) {
            return Err("knowledge read did not match the write".into());
        }
        let documents = client
            .knowledge_search(
                KnowledgeSearchInput::new(&key)
                    .namespace_scope(KnowledgeSearchInputNamespaceScope::This)
                    .search_mode(KnowledgeSearchInputSearchMode::Lexical),
            )
            .await?;
        println!(
            "knowledge search: hits={} skipped={:?}",
            documents.results.len(),
            documents.namespaces_skipped
        );
        let context = ContextPackInput::new()
            .query(&key)
            .include_knowledge(true)
            .token_budget(2000)
            .send(&client, CallOptions::default())
            .await?;
        println!(
            "context: truncated={} request={}\n{}",
            context.data.truncated, context.metadata.request_id, context.data.rendered
        );
        Ok(())
    }
    .await;

    // Each cleanup addresses only a resource this example attempted to create.
    let memory_cleanup = if memory_created {
        Some(client.delete(&key).await)
    } else {
        None
    };
    let knowledge_cleanup = if knowledge_attempted {
        Some(
            client
                .knowledge_delete(KnowledgeDeleteInput::new(&uri))
                .await,
        )
    } else {
        None
    };
    if let Some(Err(error)) = &memory_cleanup {
        eprintln!("memory cleanup: {error}");
    }
    if let Some(Err(error)) = &knowledge_cleanup {
        eprintln!("knowledge cleanup: {error}");
    }
    outcome?;
    if let Some(result) = memory_cleanup {
        result?;
    }
    if let Some(result) = knowledge_cleanup {
        result?;
    }
    Ok(())
}

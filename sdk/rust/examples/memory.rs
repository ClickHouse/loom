use std::{
    env,
    error::Error,
    time::{SystemTime, UNIX_EPOCH},
};

use clickhouse_loom::{Client, SearchRequest, SetRequest};

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let org = env::var("LOOM_ORG").unwrap_or_else(|_| "dev".into());
    let namespace = env::var("LOOM_NAMESPACE").unwrap_or_else(|_| "local".into());
    let client = Client::builder(org, namespace, "rust-example").build()?;
    let suffix = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let key = format!("rust-example-{}-{suffix}", std::process::id());
    let value = format!("Rust SDK memory demo: {key}");
    let outcome: Result<(), Box<dyn Error>> = async {
        let saved = client.set(SetRequest::new(&key, value.clone())).await?;
        if saved.noop {
            return Err("set merged with an existing memory; use a fresh namespace".into());
        }
        println!(
            "set: memory_id={} version={}",
            saved.memory_id, saved.version
        );
        let memory = client.get(&key).await?;
        if !memory.found || memory.value != value {
            return Err("get did not return the saved memory".into());
        }
        println!("get: found={} value={}", memory.found, memory.value);
        let mut query = SearchRequest::new(&key);
        query.search_mode = Some("lexical".into());
        query.namespace_scope = Some("this".into());
        query.agent_id = Some("rust-example".into());
        let hits = client.search(query).await?;
        if !hits.results.iter().any(|hit| hit.memory_key == key) {
            return Err("search did not return the saved memory".into());
        }
        println!(
            "search: {} hits abstain={} risk={} pending_extraction={} degraded={:?}",
            hits.results.len(),
            hits.abstain,
            hits.retrieval_risk,
            hits.pending_extraction,
            hits.degraded
        );
        Ok(())
    }
    .await;
    // Attempt exact-key cleanup even when a later read or search fails.
    let deleted = client.delete(&key).await;
    outcome?;
    let deleted = deleted?;
    let remaining = client.get(&key).await?;
    if !deleted.tombstoned || remaining.found {
        return Err("delete did not hide the saved memory".into());
    }
    println!("delete: tombstoned={}", deleted.tombstoned);
    println!("get after delete: found={}", remaining.found);
    Ok(())
}

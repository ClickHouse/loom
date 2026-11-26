//! Remember travel preferences, recall, and ask for a grounded answer.
mod common;

use clickhouse_loom::{
    CallOptions, Error, MemoryAskInput, MemorySetInput, ProbeRequest, SearchRequest,
};
use common::{require, Args, Result};
use serde_json::{json, Value};

async fn run() -> Result<()> {
    if Args::parse(
        "agent_memory: configure LOOM_BASE_URL, LOOM_API_KEY, LOOM_ORG and optional LOOM_NAMESPACE",
        &[],
    )?
    .is_none()
    {
        return Ok(());
    }
    let bot = common::demo_client("travel-assistant", "u-42").await?;
    let probe = bot
        .probe(ProbeRequest::new().ops(["memory.set", "memory.search", "memory.ask"]))
        .await?;
    println!(
        "probe: build={} ok={} unknown_to_sdk={}",
        probe.build_commit,
        probe.ok,
        probe.unknown_to_sdk.len(),
    );
    if !probe.ok {
        println!(
            "missing operations: {:?}; missing fields: {:?}",
            probe.missing_ops, probe.missing_fields,
        );
    }
    let prefix = format!("example/travel/{}", uuid::Uuid::new_v4());
    let facts = [
        (
            "seat_pref",
            "The user prefers an aisle seat on flights over three hours.",
        ),
        (
            "meal_pref",
            "The user asked for a vegetarian meal on their last two bookings.",
        ),
        (
            "airline_status",
            "The user holds gold status with Lufthansa.",
        ),
    ];
    let mut owned = Vec::new();
    let result: Result<()> = async {
        for (name, value) in facts {
            let key = format!("{prefix}/{name}");
            common::write_owned(
                &bot,
                MemorySetInput::new(&key, value).tags(["travel"]),
                &mut owned,
            ).await?;
        }
        let recall = bot.search(
            SearchRequest::new("what seat should I book for a long flight?")
                .user_id("u-42")
                .agent_id("travel-assistant")
                .namespace_scope("this")
                .top_k(3),
        ).await?;
        for hit in &recall.results {
            println!("recall: {} {}", hit.similarity, hit.content_excerpt);
        }
        let answer = bot.memory_ask(
            MemoryAskInput::new("Which seat and meal should I request for the user's flight to Tokyo?")
                .user_id("u-42"),
        ).await?;
        println!("ask: abstain={} answer={}", answer.abstain, answer.answer);
        let miss = bot.get(&format!("{prefix}/never_written")).await?;
        require(!miss.found, "The missing key was found")?;
        let unknown = bot.call::<Value>("memory.no_such_op", &json!({}), CallOptions::default()).await;
        require(matches!(unknown, Err(ref error) if matches!(&error.error, Error::Api(api) if api.status == 404)), "An unknown operation did not return HTTP 404")?;
        println!("unknown operation: HTTP 404");
        Ok(())
    }.await;
    let cleanup = common::cleanup_memories(&bot, &owned).await;
    result?;
    cleanup
}

#[tokio::main]
async fn main() {
    common::finish(run().await);
}

//! Cite two documents and check that unrelated text is below the knowledge floor.
mod common;

use clickhouse_loom::{
    KnowledgeAddInput, KnowledgeSearchInput, KnowledgeSearchInputNamespaceScope, SearchInput,
    SearchInputNamespaceScope,
};
use common::{require, Args, Result};
use serde_json::Value;

async fn run() -> Result<()> {
    if Args::parse("document_search: requires a real embedder and a configured knowledge similarity floor; use LOOM_* configuration", &[])?.is_none() {
        return Ok(());
    }
    let bot = common::demo_client("docs-assistant", "-").await?;
    let suffix = uuid::Uuid::new_v4();
    let documents = [
        (format!("docs://example/{suffix}/refunds.md"), "Refund policy",
         "# Refund policy\n\n## Eligibility\n\nCustomers can return an unopened item within 30 days of delivery for a full refund.\nOpened items are refunded at 80% of the price.\n\n## How refunds are paid\n\nRefunds go back to the original payment method within 5 business days.",
         "How many days do customers have to return an item for a refund?"),
        (format!("docs://example/{suffix}/shipping.md"), "Shipping times",
         "# Shipping times\n\n## Domestic\n\nStandard domestic orders arrive in 3 to 5 business days.\n\n## International\n\nInternational orders ship by air and arrive in 7 to 14 business days.",
         "How long does international shipping take?"),
    ];
    let mut attempted = Vec::new();
    let result: Result<()> = async {
        for (uri, title, content, _) in &documents {
            attempted.push(uri.clone());
            bot.knowledge_add(KnowledgeAddInput::new(uri, *content).title(*title))
                .await?;
        }
        for (uri, _, _, query) in &documents {
            let hits = bot
                .knowledge_search(
                    KnowledgeSearchInput::new(*query)
                        .top_k(3)
                        .namespace_scope(KnowledgeSearchInputNamespaceScope::This),
                )
                .await?;
            let rows = &hits.results;
            for hit in rows {
                println!(
                    "[{}#{}] {} ({})",
                    hit.source_uri, hit.chunk_idx, hit.section, hit.similarity
                );
            }
            require(
                rows.first().is_some_and(|hit| hit.source_uri == *uri),
                "The top document hit differs from the expected subject",
            )?;
        }
        let off_topic = bot
            .search_query(
                SearchInput::new("What is the boiling point of water at the top of Mount Everest?")
                    .namespace_scope(SearchInputNamespaceScope::This),
            )
            .await?;
        require(
            off_topic
                .knowledge
                .get("results")
                .and_then(Value::as_array)
                .ok_or("Missing knowledge results")?
                .is_empty(),
            "The off-topic question returned knowledge passages",
        )?;
        println!("all checks passed");
        Ok(())
    }
    .await;
    let cleanup = common::cleanup_documents(&bot, &attempted).await;
    result?;
    cleanup
}

#[tokio::main]
async fn main() {
    common::finish(run().await);
}

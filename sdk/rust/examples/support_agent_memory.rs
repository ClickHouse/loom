//! Keep customer memories separate, revise a fact, and erase a fresh customer ID.
mod common;

use clickhouse_loom::{
    Client, ErasureRequestInput, ErasureRequestInputScopeType, ErasureStatusInput,
    ErasureStatusOutputStatus, Integer, MemorySetInput, MemoryUpdateByIdInput, SearchHit,
    SearchRequest,
};
use common::{require, Args, Result};
use std::time::{Duration, Instant};

const QUERY: &str = "Which plan is the customer on, and which device do they use?";

async fn search(bot: &Client, user: &str) -> Result<Vec<SearchHit>> {
    let result = bot
        .search(
            SearchRequest::new(QUERY)
                .user_id(user)
                .agent_id("support-agent")
                .namespace_scope("this")
                .top_k(5),
        )
        .await?;
    Ok(result.results)
}

async fn run() -> Result<()> {
    if Args::parse("support_agent_memory: requires an org-admin LOOM_API_KEY and the erasure worker; only a newly generated customer ID is erased", &[])?.is_none() {
        return Ok(());
    }
    let (org, namespace) = common::demo_scope().await?;
    let suffix = uuid::Uuid::new_v4();
    let users = [
        format!("customer-keep-{suffix}"),
        format!("customer-erase-{suffix}"),
    ];
    let keep = common::client(&org, &namespace, "support-agent", &users[0], "-", None)?;
    let erase = common::client(&org, &namespace, "support-agent", &users[1], "-", None)?;
    let clients = [&keep, &erase];
    let facts = [
        [
            ("plan", "The customer is on the Pro plan."),
            ("device", "The customer uses the Android app on a Pixel 8."),
        ],
        [
            ("plan", "The customer is on the Free plan."),
            (
                "device",
                "The customer uses Safari on macOS and sees a login loop.",
            ),
        ],
    ];
    let mut owned = [Vec::new(), Vec::new()];
    let result: Result<()> = async {
        let mut plan_id = String::new();
        for who in 0..2 {
            for (key, value) in facts[who] {
                let receipt = common::write_owned(
                    clients[who], MemorySetInput::new(key, value).tags(["support"]), &mut owned[who],
                ).await?;
                if who == 0 && key == "plan" {
                    plan_id = receipt.memory_id;
                }
            }
        }
        for who in 0..2 {
            let hits = search(clients[who], &users[who]).await?;
            let mentions = |values: &[(&str, &str)]| hits.iter().any(|hit| {
                let excerpt = &hit.content_excerpt;
                values.iter().any(|(_, value)| excerpt.contains(value))
            });
            require(mentions(&facts[who]) && !mentions(&facts[1 - who]), "Customer search did not preserve isolation")?;
        }
        let revised = "The customer upgraded to the Business plan.";
        keep.memory_update_by_id(MemoryUpdateByIdInput::new(&plan_id).value(revised)).await?;
        require(keep.get("plan").await?.value == revised, "The revised plan differs")?;
        let receipt = erase.erasure_request(
            ErasureRequestInput::new(ErasureRequestInputScopeType::UserId, &users[1], "example@loom.test"),
        ).await?;
        require(!receipt.erasure_id.is_empty(), "Missing erasure ID")?;
        let id = &receipt.erasure_id;
        let deadline = Instant::now() + Duration::from_secs(120);
        loop {
            let status = erase.erasure_status(ErasureStatusInput::new(id)).await?;
            require(status.found, "The erasure request is missing")?;
            if status.status != Some(ErasureStatusOutputStatus::InProgress) {
                require(status.status == Some(ErasureStatusOutputStatus::Completed) && status.residue_count == Integer::from(0), "Erasure did not complete with zero residue")?;
                break;
            }
            require(Instant::now() < deadline, "Erasure exceeded the 120 second wait")?;
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
        require(search(&erase, &users[1]).await?.is_empty(), "Erased customer memories remain searchable")?;
        require(!search(&keep, &users[0]).await?.is_empty(), "The other customer's recall disappeared")?;
        require(keep.get("plan").await?.value == revised && keep.get("device").await?.value == facts[0][1].1, "Erasure changed the other customer's facts")?;
        for key in ["plan", "device"] {
            require(!erase.get(key).await?.found, "Erased customer memory remains readable")?;
        }
        println!("all checks passed: isolated recall, revision, zero erasure residue, other customer intact");
        Ok(())
    }.await;
    let first = common::cleanup_memories(&keep, &owned[0]).await;
    let second = common::cleanup_memories(&erase, &owned[1]).await;
    result?;
    first?;
    second
}

#[tokio::main]
async fn main() {
    common::finish(run().await);
}

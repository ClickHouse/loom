//! Hand a deterministic task to a separately authenticated worker.
mod common;

use clickhouse_loom::{
    InboxRegisterInput, Integer, MemorySetInput, TaskClaimInput, TaskCompleteInput,
    TaskCreateInput, TaskGetInput, TaskHeartbeatInput, TaskPayloadStatus,
};
use common::{require, Args, Result};
use serde_json::json;
use std::{collections::HashMap, env};

async fn run() -> Result<()> {
    if Args::parse("multi_agent_handoff: set LOOM_ORG, LOOM_NAMESPACE, LOOM_PLANNER_API_KEY (subject handoff-planner, admin) and LOOM_WORKER_API_KEY (subject handoff-worker, writer)", &[])?.is_none() {
        return Ok(());
    }
    let (org, namespace) = common::org_namespace()?;
    let planner_key = env::var("LOOM_PLANNER_API_KEY")?;
    let worker_key = env::var("LOOM_WORKER_API_KEY")?;
    require(
        !planner_key.is_empty() && !worker_key.is_empty() && planner_key != worker_key,
        "Use distinct planner and worker credentials",
    )?;
    let planner = common::client(
        &org,
        &namespace,
        "handoff-planner",
        "-",
        "-",
        Some(&planner_key),
    )?;
    let worker = common::client(
        &org,
        &namespace,
        "handoff-worker",
        "-",
        "-",
        Some(&worker_key),
    )?;
    let suffix = uuid::Uuid::new_v4();
    let inbox = format!("handoff-{suffix}");
    let key = format!("example/handoff/{suffix}/square");
    planner
        .inbox_register(InboxRegisterInput::new(&inbox, "handoff-worker"))
        .await?;
    let mut task_id = String::new();
    let mut fence = Integer::default();
    let mut owned = Vec::new();
    let result: Result<()> = async {
        let created = planner
            .task_create(
                TaskCreateInput::new(&inbox, "calculate", "Square a number")
                    .brief(HashMap::from([("number".into(), json!(7))]))
                    .idempotency_key(&inbox),
            )
            .await?;
        let task = &created.task;
        require(!task.task_id.is_empty(), "Missing task ID")?;
        task_id = task.task_id.clone();
        fence = task.fence.clone();
        require(
            task.from_subject == "handoff-planner" && task.status == TaskPayloadStatus::Todo,
            "Use the expected planner subject and an admitted task",
        )?;
        let claimed = worker.task_claim(TaskClaimInput::new(&task_id)).await?;
        fence = claimed.task.fence.clone();
        require(
            claimed.task.owner == "handoff-worker"
                && claimed.task.last_actor == "process:handoff-worker",
            "The worker principal does not own the task",
        )?;
        require(claimed.task.brief == task.brief, "The task brief changed")?;
        worker
            .task_heartbeat(TaskHeartbeatInput::new(&task_id, fence.clone()))
            .await?;
        let number = claimed
            .task
            .brief
            .get("number")
            .and_then(serde_json::Value::as_i64)
            .ok_or("Missing task number")?;
        let square = number.checked_mul(number).ok_or("Calculation overflow")?;
        let value = json!({"task_id": task_id, "result": format!("{number} squared is {square}")});
        let written = common::write_owned(
            &worker,
            MemorySetInput::new(&key, value.clone()),
            &mut owned,
        )
        .await?;
        let id = &written.memory_id;
        let outcome = HashMap::from([
            ("memory_key".into(), json!(key)),
            ("memory_id".into(), json!(id)),
        ]);
        worker
            .task_complete(TaskCompleteInput::new(&task_id, fence.clone()).outcome(outcome.clone()))
            .await?;
        let finished = planner.task_get(TaskGetInput::new(&task_id)).await?;
        require(finished.found, "The completed task is missing")?;
        let finished = finished
            .task
            .as_ref()
            .ok_or("Missing completed task payload")?;
        require(
            finished.status == TaskPayloadStatus::Done && finished.outcome == outcome,
            "The task did not retain its result",
        )?;
        let memory = common::read_owned(
            &planner,
            id,
            &key,
            "handoff-worker",
            "process:handoff-worker",
            "asserted",
            &value,
        )
        .await?;
        println!(
            "task: done; result: {}; writer: {}; source class: {}",
            memory.value["result"], memory.actor, memory.source_class
        );
        Ok(())
    }
    .await;
    let cancel = if task_id.is_empty() {
        Ok(())
    } else {
        common::cancel_owned(
            &planner,
            &worker,
            &task_id,
            &fence,
            "handoff-planner",
            "handoff-worker",
        )
        .await
    };
    // Memory cleanup still runs if fetching or cancelling the task failed.
    let cleanup = common::cleanup_memories(&worker, &owned).await;
    result?;
    cancel?;
    cleanup?;
    require(
        !worker.get(&key).await?.found,
        "The result memory remains readable",
    )?;
    println!("cleanup: result memory missing; inbox and task history retained");
    Ok(())
}

#[tokio::main]
async fn main() {
    common::finish(run().await);
}

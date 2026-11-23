// Each example uses only the helpers its flow needs.
#![allow(dead_code)]

use std::{collections::HashMap, env};

use clickhouse_loom::{
    CallOptions, CallResult, Client, Integer, KnowledgeDeleteInput, MemoryGetByIdInput,
    MemoryGetByIdOutput, MemorySetInput, MemorySetOutput, NamespaceRegisterInput,
    NamespaceRegisterInputKind, RetryPolicy, TaskCancelInput, TaskGetInput, TaskPayload,
    TaskPayloadStatus,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

pub fn require(condition: bool, message: &str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(message.into())
    }
}

pub fn client(
    org: &str,
    namespace: &str,
    agent: &str,
    user: &str,
    session: &str,
    api_key: Option<&str>,
) -> Result<Client> {
    let mut builder = Client::builder(org, namespace, agent)
        .user_id(user)
        .session_id(session)
        .retry(RetryPolicy::disabled())
        .pacing(false);
    if let Some(key) = api_key {
        builder = builder.api_key(key);
    }
    Ok(builder.build()?)
}

pub fn org_namespace() -> Result<(String, String)> {
    let org = env::var("LOOM_ORG")?;
    let namespace = env::var("LOOM_NAMESPACE")?;
    require(
        !org.trim().is_empty() && !namespace.trim().is_empty(),
        "Set LOOM_ORG and LOOM_NAMESPACE to an authorized, registered namespace",
    )?;
    Ok((org, namespace))
}

pub async fn demo_scope() -> Result<(String, String)> {
    let org = env::var("LOOM_ORG").unwrap_or_else(|_| "dev".into());
    if let Ok(namespace) = env::var("LOOM_NAMESPACE") {
        require(
            !namespace.trim().is_empty(),
            "LOOM_NAMESPACE must not be empty",
        )?;
        return Ok((org, namespace));
    }
    let namespace = format!("rust-example-{}", uuid::Uuid::new_v4());
    let bootstrap = client(&org, &namespace, "example-bootstrap", "-", "-", None)?;
    let receipt = bootstrap
        .namespace_register(
            NamespaceRegisterInput::new("example@loom.test")
                .kind(NamespaceRegisterInputKind::Bench),
        )
        .await?;
    require(
        receipt.registered && receipt.namespace == namespace,
        "The example namespace was not registered",
    )?;
    println!("namespace: {namespace} (retained)");
    Ok((org, namespace))
}

pub async fn demo_client(agent: &str, user: &str) -> Result<Client> {
    let (org, namespace) = demo_scope().await?;
    client(&org, &namespace, agent, user, "-", None)
}

pub async fn cleanup_memories(client: &Client, keys: &[String]) -> Result<()> {
    let mut failed = false;
    for key in keys {
        if client.delete(key).await.is_err() {
            failed = true;
        }
    }
    require(
        !failed,
        "Memory cleanup failed; inspect the deployment privately",
    )
}

pub async fn cleanup_documents(client: &Client, uris: &[String]) -> Result<()> {
    let mut failed = false;
    for uri in uris {
        if client
            .knowledge_delete(KnowledgeDeleteInput::new(uri))
            .await
            .is_err()
        {
            failed = true;
        }
    }
    require(
        !failed,
        "Document cleanup failed; inspect the deployment privately",
    )
}

pub fn sha256(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

// Log an already received typed response without logging its body.
pub fn received<T>(result: CallResult<T>) -> T {
    let id = &result.metadata.request_id;
    let safe = !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(&c));
    println!(
        "{}",
        json!({"request_id": if safe { id.as_str() } else { "" },
               "response_received": true})
    );
    result.data
}

pub async fn write_owned(
    client: &Client,
    input: MemorySetInput,
    keys: &mut Vec<String>,
) -> Result<MemorySetOutput> {
    // A timeout can happen after commit, so remember the attempted exact key.
    keys.push(input.memory_key.clone());
    let written = received(
        client
            .memory_set_with_options(input, CallOptions::default())
            .await?,
    );
    if written.noop {
        keys.pop();
        return Err("The write merged; use a fresh namespace".into());
    }
    require(!written.memory_id.is_empty(), "Missing write receipt ID")?;
    Ok(written)
}

pub async fn read_owned(
    client: &Client,
    id: &str,
    key: &str,
    agent: &str,
    actor: &str,
    class: &str,
    expected: &Value,
) -> Result<MemoryGetByIdOutput> {
    let memory = received(
        client
            .memory_get_by_id_with_options(MemoryGetByIdInput::new(id), CallOptions::default())
            .await?,
    );
    require(
        memory.found
            && memory.memory_key == key
            && memory.agent == agent
            && memory.actor == actor
            && memory.source_class == class
            && memory.value == *expected,
        "The result value or writing provenance differs",
    )?;
    Ok(memory)
}

pub fn finish(result: Result<()>) {
    if result.is_err() {
        eprintln!("Example failed; inspect the deployment privately. No error body is logged.");
        std::process::exit(1);
    }
}

pub fn terminal(task: &TaskPayload) -> bool {
    matches!(
        task.status,
        TaskPayloadStatus::Done
            | TaskPayloadStatus::Failed
            | TaskPayloadStatus::WontDo
            | TaskPayloadStatus::Cancelled
    )
}

pub async fn cancel_owned(
    poster: &Client,
    worker: &Client,
    task_id: &str,
    fence: &Integer,
    poster_subject: &str,
    worker_agent: &str,
) -> Result<()> {
    let got = received(
        poster
            .task_get_with_options(TaskGetInput::new(task_id), CallOptions::default())
            .await?,
    );
    require(got.found, "The created task is missing")?;
    let task = got.task.as_ref().ok_or("Missing task payload")?;
    require(
        task.from_subject == poster_subject,
        "Another principal posted the task",
    )?;
    if terminal(task) {
        return Ok(());
    }
    require(
        task.fence == *fence,
        "The task lease changed; leave it for its operator",
    )?;
    let caller = if task.owner == worker_agent {
        worker
    } else {
        require(
            task.owner.is_empty(),
            "Another agent owns the task; leave it for its operator",
        )?;
        poster
    };
    received(
        caller
            .task_cancel_with_options(
                TaskCancelInput::new(task_id, "Example stopped").fence(fence.clone()),
                CallOptions::default(),
            )
            .await?,
    );
    Ok(())
}

pub struct Args {
    fields: HashMap<String, Vec<String>>,
}

impl Args {
    pub fn parse(usage: &str, allowed: &[&str]) -> Result<Option<Self>> {
        let words: Vec<String> = env::args().skip(1).collect();
        if words.iter().any(|word| word == "--help" || word == "-h") {
            println!("{usage}");
            return Ok(None);
        }
        let mut fields = HashMap::new();
        let mut index = 0;
        while index < words.len() {
            let word = &words[index];
            if let Some(name) = word.strip_prefix("--") {
                require(allowed.contains(&name), "Unknown command-line option")?;
                require(!fields.contains_key(name), "Repeated command-line option")?;
                index += 1;
                let mut values = Vec::new();
                while index < words.len() && !words[index].starts_with("--") {
                    values.push(words[index].clone());
                    index += 1;
                    if name != "section" {
                        break;
                    }
                }
                require(
                    !values.is_empty(),
                    "A command-line option is missing its value",
                )?;
                fields.insert(name.to_owned(), values);
            } else {
                require(
                    allowed.contains(&"target") && !fields.contains_key("target"),
                    "Unexpected positional argument",
                )?;
                fields.insert("target".into(), vec![word.clone()]);
                index += 1;
            }
        }
        Ok(Some(Self { fields }))
    }

    pub fn required(&self, name: &str) -> Result<&str> {
        self.optional(name)
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| format!("Supply --{name}").into())
    }

    pub fn optional(&self, name: &str) -> Option<&str> {
        self.fields.get(name)?.first().map(String::as_str)
    }

    pub fn many(&self, name: &str) -> Vec<String> {
        self.fields.get(name).cloned().unwrap_or_default()
    }
}

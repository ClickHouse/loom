//! Synchronous calls using the async transport on a dedicated runtime thread.
//!
//! Clones share the worker, connection pool and pacing state. Construction and
//! destruction are allowed inside Tokio; calls require a thread outside Tokio.

use std::{fmt, future::Future, pin::Pin, sync::Arc, thread, time::Duration};

use serde::{de::DeserializeOwned, Serialize};
use tokio::sync::mpsc;

use crate::{
    CallFailure, CallOptions, CallResult, Error, RetryPolicy, RouteRequest, RouteResponse,
};

type Job = Box<dyn FnOnce(crate::Client) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send>;

struct Worker {
    sender: Option<mpsc::UnboundedSender<Job>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Drop for Worker {
    fn drop(&mut self) {
        // Closing the channel wakes the runtime; only its owner drops it.
        self.sender.take();
        if let Some(worker) = self.thread.take() {
            let _ = worker.join();
        }
    }
}

/// A synchronous client. Clones can call concurrently from separate threads.
#[derive(Clone)]
pub struct Client {
    worker: Arc<Worker>,
    retry: Option<RetryPolicy>,
}

/// Endpoint, identity and retry settings for a synchronous client.
pub struct ClientBuilder {
    inner: crate::ClientBuilder,
}

macro_rules! typed_call {
    ($method:ident, $with_options:ident, $request:ty, $response:ty, $op:literal) => {
        pub fn $method(&self, request: $request) -> Result<$response, Error> {
            self.$with_options(request, CallOptions::default())
                .map(|result| result.data)
                .map_err(|failure| failure.error)
        }

        /// Call with per-request controls and final-attempt metadata.
        pub fn $with_options(
            &self,
            request: $request,
            options: CallOptions,
        ) -> Result<CallResult<$response>, CallFailure> {
            self.call($op, &request, options)
        }
    };
}

impl Client {
    pub fn builder(
        org: impl Into<String>,
        namespace: impl Into<String>,
        agent: impl Into<String>,
    ) -> ClientBuilder {
        ClientBuilder {
            inner: crate::Client::builder(org, namespace, agent),
        }
    }

    typed_call!(
        set,
        set_with_options,
        crate::SetRequest,
        crate::SetResponse,
        "memory.set"
    );
    typed_call!(
        search,
        search_with_options,
        crate::SearchRequest,
        crate::SearchResponse,
        "memory.search"
    );
    typed_call!(
        health,
        health_with_options,
        crate::OpsHealthInput,
        crate::OpsHealthOutput,
        "ops.health"
    );
    typed_call!(
        capabilities,
        capabilities_with_options,
        crate::OpsCapabilitiesInput,
        crate::OpsCapabilitiesOutput,
        "ops.capabilities"
    );

    pub fn get(&self, memory_key: &str) -> Result<crate::GetResponse, Error> {
        self.get_with_options(memory_key, CallOptions::default())
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Fetch fresh capabilities; missing operations and fields are report data.
    pub fn probe(&self, request: crate::ProbeRequest) -> Result<crate::ProbeReport, Error> {
        self.probe_with_options(request, CallOptions::default())
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Probe with per-request controls and final-attempt metadata.
    pub fn probe_with_options(
        &self,
        request: crate::ProbeRequest,
        options: CallOptions,
    ) -> Result<CallResult<crate::ProbeReport>, CallFailure> {
        let result = self.capabilities_with_options(crate::OpsCapabilitiesInput::new(), options)?;
        Ok(CallResult {
            data: crate::capabilities::compare(request, result.data),
            metadata: result.metadata,
        })
    }

    pub fn get_with_options(
        &self,
        memory_key: &str,
        options: CallOptions,
    ) -> Result<CallResult<crate::GetResponse>, CallFailure> {
        self.call(
            "memory.get",
            &crate::MemoryGetInput::new(memory_key),
            options,
        )
    }

    pub fn delete(&self, memory_key: &str) -> Result<crate::DeleteResponse, Error> {
        self.delete_with_options(memory_key, CallOptions::default())
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    pub fn delete_with_options(
        &self,
        memory_key: &str,
        options: CallOptions,
    ) -> Result<CallResult<crate::DeleteResponse>, CallFailure> {
        self.call(
            "memory.delete",
            &crate::MemoryDeleteInput::new(memory_key),
            options,
        )
    }

    /// Override the retry policy while sharing the worker and connection pool.
    pub fn with_retry(&self, retry: RetryPolicy) -> Result<Self, Error> {
        retry.validate()?;
        Ok(Self {
            retry: Some(retry),
            ..self.clone()
        })
    }

    /// Call a dotted operation with the same identity and retry rules as async.
    ///
    /// Returns a configuration error on threads entered into a Tokio runtime.
    pub fn call<T: DeserializeOwned + Send + 'static>(
        &self,
        op: &str,
        request: &impl Serialize,
        mut options: CallOptions,
    ) -> Result<CallResult<T>, CallFailure> {
        outside_runtime()?;
        let body = serde_json::to_value(request)
            .map_err(|e| Error::Input(format!("cannot encode request: {e}")))?;
        let op = op.to_owned();
        if options.retry.is_none() {
            options.retry.clone_from(&self.retry);
        }
        self.submit(move |client| async move { client.call(&op, &body, options).await })
    }

    /// Send a request to a route outside the op registry, as the async client's
    /// [`crate::Client::request`] does.
    pub fn request(&self, request: RouteRequest) -> Result<RouteResponse, Error> {
        self.request_with_options(request, CallOptions::default())
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// Request with per-request controls and final-attempt metadata.
    pub fn request_with_options(
        &self,
        request: RouteRequest,
        options: CallOptions,
    ) -> Result<CallResult<RouteResponse>, CallFailure> {
        self.submit(
            move |client| async move { client.request_with_options(request, options).await },
        )
    }

    /// [`Client::request`], decoding the route's JSON body as `T`.
    pub fn request_json<T: DeserializeOwned + Send + 'static>(
        &self,
        request: RouteRequest,
    ) -> Result<T, Error> {
        self.request_json_with_options(request, CallOptions::default())
            .map(|result| result.data)
            .map_err(|failure| failure.error)
    }

    /// `request_json` with per-request controls and final-attempt metadata.
    pub fn request_json_with_options<T: DeserializeOwned + Send + 'static>(
        &self,
        request: RouteRequest,
        options: CallOptions,
    ) -> Result<CallResult<T>, CallFailure> {
        self.submit(move |client| async move {
            client.request_json_with_options(request, options).await
        })
    }

    /// Run one job on the worker and wait for its result.
    fn submit<T: Send + 'static, F, Fut>(&self, job: F) -> Result<T, CallFailure>
    where
        F: FnOnce(crate::Client) -> Fut + Send + 'static,
        Fut: Future<Output = Result<T, CallFailure>> + Send + 'static,
    {
        outside_runtime()?;
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        let job: Job = Box::new(move |client| {
            Box::pin(async move {
                let _ = sender.send(job(client).await);
            })
        });
        self.worker
            .sender
            .as_ref()
            .expect("live worker sender")
            .send(job)
            .map_err(|_| worker_error())?;
        receiver.recv().map_err(|_| worker_error())?
    }
}

fn worker_error() -> CallFailure {
    Error::Configuration("blocking client worker stopped".into()).into()
}

fn outside_runtime() -> Result<(), Error> {
    if tokio::runtime::Handle::try_current().is_ok() {
        return Err(Error::Configuration(
            "blocking calls require a thread outside a Tokio runtime; use the async client".into(),
        ));
    }
    Ok(())
}

macro_rules! setting {
    ($name:ident, $ty:ty) => {
        pub fn $name(mut self, value: $ty) -> Self {
            self.inner = self.inner.$name(value);
            self
        }
    };
}

impl ClientBuilder {
    setting!(base_url, impl Into<String>);
    setting!(api_key, impl Into<String>);
    setting!(user_id, impl Into<String>);
    setting!(session_id, impl Into<String>);
    setting!(run_id, impl Into<String>);
    setting!(timeout, Duration);
    setting!(retry, RetryPolicy);
    setting!(pacing, bool);
    setting!(ratelimit_max_wait, Duration);

    pub fn build(self) -> Result<Client, Error> {
        let (sender, mut receiver) = mpsc::unbounded_channel::<Job>();
        let (ready, startup) = std::sync::mpsc::sync_channel(1);
        let worker = thread::Builder::new()
            .name("loom-blocking".into())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(error) => {
                        let _ = ready.send(Err(Error::Configuration(format!(
                            "cannot create blocking runtime: {error}"
                        ))));
                        return;
                    }
                };
                runtime.block_on(async move {
                    let client = match self.inner.build() {
                        Ok(client) => client,
                        Err(error) => {
                            let _ = ready.send(Err(error));
                            return;
                        }
                    };
                    if ready.send(Ok(())).is_err() {
                        return;
                    }
                    while let Some(job) = receiver.recv().await {
                        tokio::spawn(job(client.clone()));
                    }
                });
            })
            .map_err(|error| {
                Error::Configuration(format!("cannot start blocking worker: {error}"))
            })?;
        match startup.recv() {
            Ok(Ok(())) => Ok(Client {
                worker: Arc::new(Worker {
                    sender: Some(sender),
                    thread: Some(worker),
                }),
                retry: None,
            }),
            result => {
                drop(sender);
                let _ = worker.join();
                match result {
                    Ok(Err(error)) => Err(error),
                    Err(_) => Err(Error::Configuration(
                        "blocking client worker stopped during startup".into(),
                    )),
                    Ok(Ok(())) => unreachable!("success handled above"),
                }
            }
        }
    }
}

impl fmt::Debug for Client {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Client")
            .field("retry", &self.retry)
            .finish_non_exhaustive()
    }
}

impl fmt::Debug for ClientBuilder {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ClientBuilder")
            .field("inner", &self.inner)
            .finish()
    }
}

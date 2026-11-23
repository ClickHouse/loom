//! Durable, content-free conversation protection and retained automatic captures.

use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{atomic::{AtomicBool, Ordering}, Arc},
};

use serde_json::Value;
use crate::Error;

pub(crate) const MAX_BODY: usize = 4 * 1024 * 1024;

fn unavailable() -> Error {
    Error::Configuration("session protection state could not be confirmed".into())
}

fn io<T>(result: std::io::Result<T>) -> Result<T, Error> {
    result.map_err(|_| unavailable())
}

/// Keep both fields in the application's trusted conversation record. Never recreate
/// a family when restoring this pointer fails.
#[derive(Clone)]
pub struct SessionProtectionPointer {
    pub path: PathBuf,
    pub id: String,
}

struct Authority {
    pointer: SessionProtectionPointer,
    latched: AtomicBool,
}

/// Clones share an authority independently of client identity, namespace and token.
#[derive(Clone)]
pub struct SessionProtection(Arc<Authority>);

impl SessionProtection {
    pub fn create(path: impl AsRef<Path>) -> Result<Self, Error> {
        let path = path.as_ref();
        let mut directory = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            directory.mode(0o700);
        }
        io(directory.create(path))?;
        let id = uuid::Uuid::new_v4().to_string();
        for (name, data) in [("identity", id.as_bytes()), ("state", b"0".as_slice())] {
            let mut file = create_private(&path.join(name))?;
            io(file.write_all(data))?;
            io(file.sync_all())?;
        }
        sync_directory(path)?;
        Self::open(SessionProtectionPointer { path: io(fs::canonicalize(path))?, id })
    }

    pub fn open(pointer: SessionProtectionPointer) -> Result<Self, Error> {
        if uuid::Uuid::parse_str(&pointer.id).is_err() || !pointer.path.is_absolute() {
            return Err(unavailable());
        }
        let family = Self(Arc::new(Authority { pointer, latched: AtomicBool::new(false) }));
        family.require_ready()?;
        // An interrupted request cannot acknowledge that its response was public.
        if family.pending_except(None)? {
            family.mark()?;
        }
        Ok(family)
    }

    pub fn pointer(&self) -> SessionProtectionPointer {
        self.0.pointer.clone()
    }

    pub fn require_ready(&self) -> Result<(), Error> {
        let directory = io(fs::symlink_metadata(&self.0.pointer.path))?;
        if !directory.is_dir() || directory.file_type().is_symlink() || !private(&directory) {
            return Err(unavailable());
        }
        if self.read("identity", 36)? != self.0.pointer.id.as_bytes() {
            return Err(unavailable());
        }
        match self.read("state", 1)?.as_slice() {
            b"1" => { self.0.latched.store(true, Ordering::SeqCst); }
            b"0" if !self.0.latched.load(Ordering::SeqCst) => {}
            _ => return Err(unavailable()),
        }
        Ok(())
    }

    pub fn protected(&self) -> Result<bool, Error> {
        self.require_ready()?;
        Ok(self.0.latched.load(Ordering::SeqCst))
    }

    pub fn mark(&self) -> Result<(), Error> {
        self.require_ready()?;
        self.0.latched.store(true, Ordering::SeqCst);
        let path = self.0.pointer.path.join("state");
        let mut file = io(OpenOptions::new().write(true).open(path))?;
        io(file.write_all(b"1"))?;
        io(file.sync_all())
    }

    pub fn capture(&self) -> SessionCapture {
        let mut capture = SessionCapture::current().unwrap_or_else(SessionCapture::empty);
        capture.add(self.clone());
        capture
    }

    fn read(&self, name: &str, size: u64) -> Result<Vec<u8>, Error> {
        let path = self.0.pointer.path.join(name);
        let stat = io(fs::symlink_metadata(&path))?;
        if !stat.is_file() || stat.file_type().is_symlink() || !private(&stat) || stat.len() != size {
            return Err(unavailable());
        }
        let file = io(OpenOptions::new().read(true).write(true).open(path))?;
        let mut bytes = Vec::new();
        io(file.take(size + 1).read_to_end(&mut bytes))?;
        if bytes.len() as u64 != size { return Err(unavailable()); }
        Ok(bytes)
    }

    fn pending_except(&self, own: Option<&Path>) -> Result<bool, Error> {
        for (count, item) in io(fs::read_dir(&self.0.pointer.path))?.enumerate() {
            if count >= 10_000 { return Err(unavailable()); }
            let item = io(item)?;
            if item.file_name().to_string_lossy().starts_with("pending-")
                && own != Some(item.path().as_path()) {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

fn create_private(path: &Path) -> Result<File, Error> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    io(options.open(path))
}

fn private(stat: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        stat.permissions().mode() & 0o077 == 0
    }
    #[cfg(not(unix))]
    { let _ = stat; false }
}

fn sync_directory(path: &Path) -> Result<(), Error> {
    io(io(File::open(path))?.sync_all())
}

tokio::task_local! {
    static CAPTURE: SessionCapture;
}

/// Retain the capture for deferred work. `scope` carries it across awaits and into
/// calls made through any client in that task. Spawned tasks must receive a clone.
#[derive(Clone)]
pub struct SessionCapture {
    families: Vec<SessionProtection>,
}

impl SessionCapture {
    fn empty() -> Self {
        Self { families: Vec::new() }
    }

    pub fn contribute(mut self, family: &SessionProtection) -> Self {
        if let Some(parent) = Self::current() {
            for contributor in parent.families { self.add(contributor); }
        }
        self.add(family.clone());
        self
    }

    fn add(&mut self, family: SessionProtection) {
        if !self.families.iter().any(|f| f.0.pointer.path == family.0.pointer.path
            && f.0.pointer.id == family.0.pointer.id) {
            self.families.push(family);
        }
    }

    pub(crate) fn current() -> Option<Self> {
        CAPTURE.try_with(Clone::clone).ok()
    }

    pub async fn scope<F: std::future::Future>(&self, work: F) -> Result<F::Output, Error> {
        let mut retained = self.clone();
        if let Some(parent) = Self::current() {
            for family in parent.families { retained.add(family); }
        }
        retained.guard(&[])?;
        Ok(CAPTURE.scope(retained, work).await)
    }

    fn guard(&self, attempts: &[(SessionProtection, PathBuf)]) -> Result<(), Error> {
        for family in &self.families {
            let own = attempts.iter().find(|(f, _)| f.0.pointer.path == family.0.pointer.path && f.0.pointer.id == family.0.pointer.id)
                .map(|(_, path)| path.as_path());
            if family.protected()? || family.pending_except(own)? {
                return Err(Error::Input("automatic capture is suppressed for this session".into()));
            }
        }
        Ok(())
    }
}

/// Dropping an incomplete attempt (including cancellation) latches every opening
/// family. A crash leaves pending markers for the next trusted reopen.
pub(crate) struct Observation {
    families: Vec<SessionProtection>,
    automatic: Option<SessionCapture>,
    attempts: Vec<(SessionProtection, PathBuf)>,
    complete: bool,
}

impl Observation {
    pub(crate) fn begin(bound: Option<&SessionProtection>) -> Result<Self, Error> {
        let automatic = SessionCapture::current();
        let mut retained = automatic.clone().unwrap_or_else(SessionCapture::empty);
        if let Some(bound) = bound { retained.add(bound.clone()); }
        let automatic = automatic.map(|_| retained.clone());
        let mut observation = Self { families: retained.families, automatic, attempts: vec![], complete: false };
        if let Some(capture) = &observation.automatic { capture.guard(&[])?; }
        for family in &observation.families {
            family.require_ready()?;
            let path = family.0.pointer.path.join(format!("pending-{}", uuid::Uuid::new_v4()));
            let file = create_private(&path)?;
            observation.attempts.push((family.clone(), path));
            io(file.sync_all())?;
            sync_directory(&family.0.pointer.path)?;
        }
        observation.before_send()?;
        Ok(observation)
    }

    pub(crate) fn installed(&self) -> bool { !self.families.is_empty() }

    pub(crate) fn before_send(&self) -> Result<(), Error> {
        for family in &self.families { family.require_ready()?; }
        for (family, path) in &self.attempts {
            let name = path.file_name().and_then(|v| v.to_str()).ok_or_else(unavailable)?;
            family.read(name, 0)?;
        }
        if let Some(capture) = &self.automatic { capture.guard(&self.attempts)?; }
        Ok(())
    }

    pub(crate) fn check_media(&self, media: &str, status: u16) -> Result<(), Error> {
        if !self.installed() { return Ok(()); }
        let media = media.split(';').next().unwrap_or("").trim().to_ascii_lowercase();
        if media != "application/json" && !media.ends_with("+json") && ![204, 304].contains(&status) {
            return Err(unavailable());
        }
        Ok(())
    }

    pub(crate) fn observe(&mut self, bytes: &[u8], media: &str, status: u16) -> Result<(), Error> {
        if !self.installed() { self.complete = true; return Ok(()); }
        if bytes.len() > MAX_BODY { return Err(unavailable()); }
        self.check_media(media, status)?;
        let value = if bytes.is_empty() && [204, 304].contains(&status) {
            serde_json::json!({})
        } else { serde_json::from_slice::<Value>(bytes).map_err(|_| unavailable())? };
        if !value.is_object() { return Err(unavailable()); }
        let protected = marked(&value)?;
        for family in &self.families {
            family.require_ready()?;
            if protected { family.mark()?; }
        }
        for (family, path) in &self.attempts {
            io(fs::remove_file(path))?;
            sync_directory(&family.0.pointer.path)?;
        }
        self.complete = true;
        Ok(())
    }
}

impl Drop for Observation {
    fn drop(&mut self) {
        if !self.complete {
            for family in &self.families {
                // Pending markers preserve refusal if storage cannot acknowledge the latch.
                let _ = family.mark();
            }
        }
    }
}

fn marked(value: &Value) -> Result<bool, Error> {
    let mut pending = vec![value.clone()];
    let mut remaining = 10_000usize;
    let mut text_bytes = MAX_BODY;
    while let Some(value) = pending.pop() {
        remaining = remaining.checked_sub(1).ok_or_else(unavailable)?;
        let Some(item) = value.as_object() else { continue; };
        if let Some(flag) = item.get("mirrored") {
            match flag.as_bool() { Some(true) => return Ok(true), Some(false) => {}, None => return Err(unavailable()) }
        }
        for key in ["result", "structuredContent"] {
            if let Some(value) = item.get(key).filter(|v| v.is_object()) { pending.push(value.clone()); }
        }
        if let Some(data) = item.get("error").and_then(|v| v.get("data")).filter(|v| v.is_object()) {
            pending.push(data.clone());
        }
        if let Some(items) = item.get("items").and_then(Value::as_array) {
            if items.len() > remaining.saturating_sub(pending.len()) { return Err(unavailable()); }
            pending.extend(items.iter().cloned());
        }
        if let Some(content) = item.get("content").and_then(Value::as_array) {
            remaining = remaining.checked_sub(content.len()).ok_or_else(unavailable)?;
            for block in content {
                if block.get("type").and_then(Value::as_str) != Some("text") { continue; }
                if let Some(text) = block.get("text").and_then(Value::as_str) {
                    text_bytes = text_bytes.checked_sub(text.len()).ok_or_else(unavailable)?;
                    if text.trim_start().starts_with('{') {
                        pending.push(serde_json::from_str(text).map_err(|_| unavailable())?);
                    }
                }
            }
        }
        if pending.len() > remaining { return Err(unavailable()); }
    }
    Ok(false)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    struct Store(PathBuf);
    impl Store {
        fn family() -> (Self, SessionProtection) {
            let path = std::env::temp_dir().join(format!("loom-protection-{}", uuid::Uuid::new_v4()));
            let family = SessionProtection::create(&path).unwrap();
            (Self(path), family)
        }
    }
    impl Drop for Store {
        fn drop(&mut self) { fs::remove_dir_all(&self.0).unwrap(); }
    }

    #[test]
    fn durable_latch_and_pending_resume_refuse_missing_or_rolled_back_state() {
        let (_store, family) = Store::family();
        assert!(!family.protected().unwrap());
        let pending = Observation::begin(Some(&family)).unwrap();
        let restored = SessionProtection::open(family.pointer()).unwrap();
        assert!(restored.protected().unwrap());
        drop(pending);
        io(fs::write(family.0.pointer.path.join("state"), b"0")).unwrap();
        assert!(family.require_ready().is_err());
        fs::remove_file(family.0.pointer.path.join("state")).unwrap();
        assert!(SessionProtection::open(family.pointer()).is_err());
    }

    #[test]
    fn explicit_error_metadata_latches_before_typed_decoding() {
        let (_store, family) = Store::family();
        let mut observation = Observation::begin(Some(&family)).unwrap();
        observation.observe(br#"{"error":{"data":{"items":[{"mirrored":true}]}}}"#,
            "application/json", 403).unwrap();
        assert!(family.protected().unwrap());
        assert!(!family.pending_except(None).unwrap());
        assert!(SessionProtection::open(family.pointer()).unwrap().protected().unwrap());
    }

    #[test]
    fn malformed_envelopes_streams_and_body_limits_latch_without_payload_errors() {
        for (bytes, media) in [
            (br#"{"mirrored":"false"}"#.to_vec(), "application/json"),
            (br#"{"content":[{"type":"text","text":"{private"}]}"#.to_vec(), "application/json"),
            (b"private".to_vec(), "text/event-stream"),
            (vec![b'x'; MAX_BODY + 1], "application/json"),
        ] {
            let (_store, family) = Store::family();
            let mut observation = Observation::begin(Some(&family)).unwrap();
            let error = observation.observe(&bytes, media, 500).unwrap_err();
            assert!(!error.to_string().contains("private"));
            drop(observation);
            assert!(family.protected().unwrap());
        }
    }

    #[test]
    fn clean_observation_finishes_pending_and_empty_statuses_are_valid() {
        let (_store, family) = Store::family();
        for (bytes, media, status) in [(b"{}".as_slice(), "application/json; charset=utf-8", 500),
            (b"".as_slice(), "", 204)] {
            let mut observation = Observation::begin(Some(&family)).unwrap();
            assert!(observation.installed());
            observation.observe(bytes, media, status).unwrap();
            assert!(!family.pending_except(None).unwrap());
            assert!(!family.protected().unwrap());
        }
    }

    #[test]
    fn incomplete_attempt_drop_marks_every_opening_family() {
        let (_store, family) = Store::family();
        drop(Observation::begin(Some(&family)).unwrap());
        assert!(family.protected().unwrap());
        assert!(family.pending_except(None).unwrap());
    }

    #[tokio::test]
    async fn automatic_capture_carries_into_new_clients_and_clone_shared_policy() {
        let (_store, family) = Store::family();
        let client = crate::Client::builder("o", "n", "a").session_protection(family.clone()).build().unwrap();
        let cloned = client.with_retry(crate::RetryPolicy::default()).unwrap();
        let capture = family.capture();
        family.mark().unwrap();
        let other = crate::Client::builder("other", "other", "other").build().unwrap();
        assert!(other.call_automatic::<Value>(&capture, "memory.set", &serde_json::json!({"value":"private"})).await.is_err());
        assert!(cloned.call_automatic::<Value>(&capture, "memory.set", &serde_json::json!({})).await.is_err());
        assert!(SessionCapture::current().is_none());
    }

    #[tokio::test]
    async fn clean_capture_cannot_omit_a_bound_familys_protection_or_pending_response() {
        for pending in [false, true] {
            let (_clean_store, clean) = Store::family();
            let (_bound_store, bound) = Store::family();
            let opening = if pending {
                Some(Observation::begin(Some(&bound)).unwrap())
            } else {
                bound.mark().unwrap();
                None
            };
            let capture = clean.capture();
            let result = capture.scope(async { Observation::begin(Some(&bound)) }).await.unwrap();
            assert!(matches!(result, Err(Error::Input(message)) if message.contains("automatic capture is suppressed")));
            let client = crate::Client::builder("o", "n", "a")
                .api_key("synthetic-token").session_protection(bound.clone()).build().unwrap();
            let cloned = client.with_retry(crate::RetryPolicy::default()).unwrap();
            for target in [&client, &cloned] {
                let (_contributor_store, contributor) = Store::family();
                let capture = contributor.capture();
                let result = target.call_automatic::<Value>(
                    &capture, "memory.set", &serde_json::json!({"namespace":"other", "value":"private"})
                ).await;
                assert!(matches!(result, Err(Error::Input(message)) if message.contains("automatic capture is suppressed")));
            }
            drop(opening);
        }
    }

    #[tokio::test]
    async fn response_retains_contributors_and_final_attempt_barrier_rechecks() {
        let (_first_store, first) = Store::family();
        let (_second_store, second) = Store::family();
        let capture = first.capture();
        let retained = capture.scope(async {
            let retained = second.capture();
            let mut observation = Observation::begin(Some(&second)).unwrap();
            observation.observe(br#"{"structuredContent":{"mirrored":true}}"#, "application/json", 200).unwrap();
            retained
        }).await.unwrap();
        assert!(first.protected().unwrap());
        assert!(second.protected().unwrap());
        assert!(retained.scope(async { panic!("automatic write escaped"); }).await.is_err());
        assert!(SessionCapture::current().is_none());

        let (_store, clean) = Store::family();
        let capture = clean.capture();
        capture.scope(async {
            let first_attempt = Observation::begin(Some(&clean)).unwrap();
            clean.mark().unwrap();
            assert!(first_attempt.before_send().is_err());
            drop(first_attempt);
            assert!(Observation::begin(Some(&clean)).is_err());
        }).await.unwrap();
    }

    #[tokio::test]
    async fn cancelled_scope_keeps_durable_pending_and_restores_context() {
        let (_store, family) = Store::family();
        let capture = family.capture();
        let (admitted, ready) = tokio::sync::oneshot::channel();
        let task = tokio::spawn({
            let family = family.clone();
            async move {
                capture.scope(async {
                    let _observation = Observation::begin(Some(&family)).unwrap();
                    admitted.send(()).unwrap();
                    std::future::pending::<()>().await;
                }).await
            }
        });
        tokio::time::timeout(std::time::Duration::from_secs(5), ready).await.unwrap().unwrap();
        assert!(family.pending_except(None).unwrap());
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert!(family.protected().unwrap());
        assert!(SessionProtection::open(family.pointer()).unwrap().protected().unwrap());
        assert!(SessionCapture::current().is_none());
    }

    #[test]
    fn missing_pending_proof_refuses_at_final_boundary() {
        let (_store, family) = Store::family();
        let observation = Observation::begin(Some(&family)).unwrap();
        fs::remove_file(&observation.attempts[0].1).unwrap();
        assert!(observation.before_send().is_err());
        drop(observation);
        assert!(family.protected().unwrap());
    }

    #[test]
    fn nested_text_and_budgeted_envelopes_follow_wire_contract() {
        assert!(marked(&serde_json::json!({"content":[{"type":"text","text":"{\"result\":{\"mirrored\":true}}"}]})).unwrap());
        assert!(marked(&serde_json::json!({"items": vec![serde_json::json!({}); 10_001]})).is_err());
        assert!(!marked(&serde_json::json!({"value":"mirrored: true", "mirrored":false})).unwrap());
    }
}

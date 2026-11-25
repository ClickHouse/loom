use std::collections::{BTreeMap, BTreeSet};

use crate::OpsCapabilitiesOutput;

/// Operations and request fields an application needs from the server.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct ProbeRequest {
    pub ops: Vec<String>,
    pub fields: BTreeMap<String, Vec<String>>,
}

impl ProbeRequest {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn ops(mut self, ops: impl IntoIterator<Item = impl Into<String>>) -> Self {
        self.ops = ops.into_iter().map(Into::into).collect();
        self
    }

    /// Set the required request fields for one operation.
    pub fn fields(
        mut self,
        op: impl Into<String>,
        fields: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        self.fields
            .insert(op.into(), fields.into_iter().map(Into::into).collect());
        self
    }
}

/// A capability comparison. Missing capabilities do not produce an error.
#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub struct ProbeReport {
    /// True when all requested operations and request fields are served.
    pub ok: bool,
    pub build_commit: String,
    pub schema_version: String,
    /// Preserves the requested order and duplicates.
    pub missing_ops: Vec<String>,
    /// Includes every requested field when its operation is not served.
    pub missing_fields: BTreeMap<String, Vec<String>>,
    /// Server operations absent from this SDK's registry, sorted by name.
    pub unknown_to_sdk: Vec<String>,
}

pub(crate) fn compare(request: ProbeRequest, capabilities: OpsCapabilitiesOutput) -> ProbeReport {
    let served: BTreeMap<_, _> = capabilities
        .ops
        .iter()
        .map(|op| (op.name.as_str(), op))
        .collect();
    let missing_ops: Vec<_> = request
        .ops
        .into_iter()
        .filter(|op| !served.contains_key(op.as_str()))
        .collect();
    let missing_fields: BTreeMap<_, _> = request
        .fields
        .into_iter()
        .filter_map(|(name, fields)| {
            let missing: Vec<_> = fields
                .into_iter()
                .filter(|field| {
                    !served
                        .get(name.as_str())
                        .is_some_and(|op| op.request_fields.contains(field))
                })
                .collect();
            (!missing.is_empty()).then_some((name, missing))
        })
        .collect();
    let known: BTreeSet<_> = crate::wire_models::OP_POLICIES
        .iter()
        .map(|policy| policy.0)
        .collect();
    let unknown_to_sdk = served
        .keys()
        .filter(|name| !known.contains(**name))
        .map(|name| (*name).to_owned())
        .collect();
    ProbeReport {
        ok: missing_ops.is_empty() && missing_fields.is_empty(),
        build_commit: capabilities.build_commit,
        schema_version: capabilities.schema_version,
        missing_ops,
        missing_fields,
        unknown_to_sdk,
    }
}

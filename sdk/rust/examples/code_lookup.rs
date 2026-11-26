//! Dispatch a selected code lookup with the Python example's fixed request shapes.
mod common;

use clickhouse_loom::{
    CallOptions, Client, CodeCallersInput, CodeImpactInput, CodeSearchInput, CodeSymbolInput,
};
use common::{require, Args, Result};
use serde_json::{json, Value};

enum Selected {
    Symbol(CodeSymbolInput),
    Search(CodeSearchInput),
    Callers(CodeCallersInput),
    Impact(CodeImpactInput),
}

fn selected(args: &Args) -> Result<Selected> {
    let kind = args.optional("kind").unwrap_or("symbol");
    let target = args.required("target")?.trim();
    require(
        ["symbol", "search", "callers", "impact"].contains(&kind),
        "Invalid lookup kind",
    )?;
    let depth: u32 = args
        .optional("depth")
        .unwrap_or(if kind == "impact" { "2" } else { "1" })
        .parse()?;
    require(
        (1..=3).contains(&depth) && (kind != "impact" || depth <= 2),
        "Invalid lookup depth",
    )?;
    require(
        args.optional("depth").is_none() || ["callers", "impact"].contains(&kind),
        "Depth applies only to callers and impact",
    )?;
    if kind != "search" {
        let mut bytes = target.bytes();
        require(
            bytes
                .next()
                .is_some_and(|c| c.is_ascii_alphanumeric() || b"_$~".contains(&c))
                && bytes
                    .all(|c| c.is_ascii_alphanumeric() || b"_$~.:/<>,()[]*&+=!%|^-".contains(&c)),
            "Use a symbol reference or path:line; use search for prose",
        )?;
    }
    Ok(match kind {
        "symbol" => Selected::Symbol(
            CodeSymbolInput::new()
                .name(target)
                .include_lessons(false)
                .include_body(true)
                .include_history(false)
                .include_examples(false)
                .uses(false)
                .uses_brief(false),
        ),
        "search" => Selected::Search(CodeSearchInput::new(target).include_lessons(false).top_k(3)),
        "callers" => Selected::Callers(
            CodeCallersInput::new()
                .name(target)
                .include_lessons(false)
                .depth(depth)
                .max_nodes(20),
        ),
        _ => Selected::Impact(
            CodeImpactInput::new()
                .name(target)
                .include_lessons(false)
                .depth(depth)
                .per_hop_limit(20),
        ),
    })
}

async fn lookup(code: &Client, selected: Selected) -> Result<Value> {
    // Serialize only at the output boundary, after typed response decoding.
    Ok(match selected {
        Selected::Symbol(input) => serde_json::to_value(common::received(
            code.code_symbol_with_options(input, CallOptions::default())
                .await?,
        ))?,
        Selected::Search(input) => serde_json::to_value(common::received(
            code.code_search_with_options(input, CallOptions::default())
                .await?,
        ))?,
        Selected::Callers(input) => serde_json::to_value(common::received(
            code.code_callers_with_options(input, CallOptions::default())
                .await?,
        ))?,
        Selected::Impact(input) => serde_json::to_value(common::received(
            code.code_impact_with_options(input, CallOptions::default())
                .await?,
        ))?,
    })
}

// Keep uncertainty metadata and whole rows. If it cannot fit, omit all evidence.
fn bounded(mut data: Value) -> Result<String> {
    let mut out = json!({"data": data, "client_truncation": null});
    out.as_object_mut()
        .ok_or("Invalid envelope")?
        .remove("client_truncation");
    let fits = |value: &Value| -> Result<bool> {
        Ok(serde_json::to_string(value)?.encode_utf16().count() <= 12_000)
    };
    if fits(&out)? {
        return Ok(serde_json::to_string(&out)?);
    }
    data = out["data"].take();
    let mut cut = json!({"reason": "max_result_chars", "insufficient": true, "omitted_bodies": 0, "omitted_rows": {}});
    for key in ["results", "definitions"] {
        if let Some(rows) = data[key].as_array_mut() {
            for row in rows.iter_mut().rev() {
                if row["body"].as_str().is_some_and(|body| !body.is_empty()) {
                    row["body"] = json!("");
                    row["elided"] = json!(true);
                    cut["omitted_bodies"] = json!(cut["omitted_bodies"].as_u64().unwrap_or(0) + 1);
                }
            }
        }
    }
    out = json!({"data": data, "client_truncation": cut});
    if fits(&out)? {
        return Ok(serde_json::to_string(&out)?);
    }
    for key in [
        "reverts",
        "owners",
        "co_change_files",
        "impacted_tests",
        "documented_in",
        "dispatch_seeds",
        "candidates",
        "impacted_symbols",
        "edges",
        "results",
        "definitions",
    ] {
        while out["data"][key]
            .as_array()
            .is_some_and(|rows| !rows.is_empty())
        {
            out["data"][key]
                .as_array_mut()
                .ok_or("Invalid row collection")?
                .pop();
            let count = out["client_truncation"]["omitted_rows"][key]
                .as_u64()
                .unwrap_or(0);
            out["client_truncation"]["omitted_rows"][key] = json!(count + 1);
            if fits(&out)? {
                return Ok(serde_json::to_string(&out)?);
            }
        }
    }
    Ok(serde_json::to_string(&json!({"client_truncation": {
        "reason": "max_result_chars", "insufficient": true, "metadata_omitted": true
    }}))?)
}

async fn run() -> Result<()> {
    let Some(args) = Args::parse("code_lookup TARGET --org ORG --memory-namespace NS --code-namespace NS [--kind symbol|search|callers|impact] [--depth N]", &["target", "org", "memory-namespace", "code-namespace", "kind", "depth"])? else { return Ok(()); };
    let selected = selected(&args)?;
    args.required("memory-namespace")?;
    // The transport stamps the bound namespace; code calls need their own client.
    let code = common::client(
        args.required("org")?,
        args.required("code-namespace")?,
        "code-assistant",
        "-",
        "-",
        None,
    )?;
    println!("{}", bounded(lookup(&code, selected).await?)?);
    Ok(())
}

#[tokio::main]
async fn main() {
    common::finish(run().await);
}

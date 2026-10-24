// Copyright (c) 2026 FYWinds

//! Render one Proxmox notification the way proxmox-notify does.
//!
//!     proxmox-render <template-dir> <type> <data.json>
//!
//! Prints {"subject", "html", "text"} as JSON. Same handlebars major version,
//! escaping and helpers as proxmox-notify (proxmox-notify/src/renderer/mod.rs).
//! Two deliberate differences: a template that fails to render is an error
//! here instead of a silent fallback to the stock template, and the `table`
//! and `object` block helpers are not provided (the templates do not use them).

use std::{fs, path::Path, process::ExitCode};

use handlebars::{
    Context, Handlebars, Helper, HelperResult, Output, RenderContext, RenderErrorReason,
};
use serde_json::{json, Value};

/// proxmox-human-byte `HumanByte::new_binary` Display.
fn human_bytes(size: f64) -> String {
    const UNITS: [&str; 6] = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let bits = 64 - (size as u64).leading_zeros();
    let exp = match bits {
        51.. => 5,
        41..=50 => 4,
        31..=40 => 3,
        21..=30 => 2,
        11..=20 => 1,
        _ => 0,
    };
    let scaled = size / 1024f64.powi(exp);
    let rounded = (scaled * 1000.0).round() / 1000.0;
    format!("{} {}", rounded, UNITS[exp as usize])
}

/// proxmox-time `TimeSpan` Display for whole seconds.
fn duration(secs: u64) -> String {
    const MIN: u64 = 60;
    const HOUR: u64 = 60 * MIN;
    const DAY: u64 = 24 * HOUR;
    const WEEK: u64 = 7 * DAY;
    const MONTH: u64 = DAY * 3044 / 100;
    const YEAR: u64 = DAY * 36525 / 100;
    let mut rem = secs;
    let mut parts = Vec::new();
    for (size, unit) in [(YEAR, "y"), (MONTH, "M"), (WEEK, "w"), (DAY, "d"), (HOUR, "h"), (MIN, "m")] {
        let n = rem / size;
        rem %= size;
        if n > 0 {
            parts.push(format!("{n}{unit}"));
        }
    }
    if rem > 0 || parts.is_empty() {
        parts.push(format!("{rem}s"));
    }
    parts.join(" ")
}

fn as_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.parse().ok(),
        _ => None,
    }
}

fn as_u64(v: &Value) -> Option<u64> {
    match v {
        Value::Number(n) => n.as_u64(),
        Value::String(s) => s.parse().ok(),
        _ => None,
    }
}

fn as_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.parse().ok(),
        _ => None,
    }
}

fn value_helper(
    name: &'static str,
    h: &Helper,
    out: &mut dyn Output,
    render: fn(&Value) -> Option<String>,
) -> HelperResult {
    let param = h.param(0).ok_or(RenderErrorReason::ParamNotFoundForIndex(name, 0))?;
    // proxmox-notify writes "ERROR" for values it cannot convert.
    out.write(&render(param.value()).unwrap_or_else(|| "ERROR".into()))?;
    Ok(())
}

fn human_bytes_helper(h: &Helper, _: &Handlebars, _: &Context, _: &mut RenderContext, out: &mut dyn Output) -> HelperResult {
    value_helper("human-bytes", h, out, |v| as_f64(v).map(human_bytes))
}

fn duration_helper(h: &Helper, _: &Handlebars, _: &Context, _: &mut RenderContext, out: &mut dyn Output) -> HelperResult {
    value_helper("duration", h, out, |v| as_u64(v).map(duration))
}

fn timestamp_helper(h: &Helper, _: &Handlebars, _: &Context, _: &mut RenderContext, out: &mut dyn Output) -> HelperResult {
    value_helper("timestamp", h, out, |v| {
        use chrono::TimeZone;
        let ts = as_i64(v)?;
        let local = chrono::Local.timestamp_opt(ts, 0).single()?;
        Some(local.format("%F %H:%M:%S").to_string())
    })
}

fn relative_percentage_helper(h: &Helper, _: &Handlebars, _: &Context, _: &mut RenderContext, out: &mut dyn Output) -> HelperResult {
    let p0 = h
        .param(0)
        .and_then(|v| v.value().as_f64())
        .ok_or(RenderErrorReason::ParamNotFoundForIndex("relative-percentage", 0))?;
    let p1 = h
        .param(1)
        .and_then(|v| v.value().as_f64())
        .ok_or(RenderErrorReason::ParamNotFoundForIndex("relative-percentage", 1))?;
    if p1 == 0.0 {
        out.write("-")?;
    } else {
        out.write(&format!("{:.2}%", (p0 * 100.0) / p1))?;
    }
    Ok(())
}

fn render(template: &str, data: &Value, html: bool) -> Result<String, String> {
    let mut hb = Handlebars::new();
    hb.register_escape_fn(if html { handlebars::html_escape } else { handlebars::no_escape });
    hb.register_helper("human-bytes", Box::new(human_bytes_helper));
    hb.register_helper("duration", Box::new(duration_helper));
    hb.register_helper("timestamp", Box::new(timestamp_helper));
    hb.register_helper("relative-percentage", Box::new(relative_percentage_helper));
    hb.render_template(template, data).map_err(|e| e.to_string())
}

fn render_file(dir: &Path, name: &str, data: &Value, html: bool) -> Result<String, String> {
    let path = dir.join(name);
    let template = fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    render(&template, data, html).map_err(|e| format!("{}: {e}", path.display()))
}

fn run(dir: &Path, ty: &str, data_path: &Path) -> Result<Value, String> {
    let data: Value = serde_json::from_str(
        &fs::read_to_string(data_path).map_err(|e| format!("{}: {e}", data_path.display()))?,
    )
    .map_err(|e| format!("{}: {e}", data_path.display()))?;

    let subject = render_file(dir, &format!("{ty}-subject.txt.hbs"), &data, false)?.replace('\n', " ");
    let text = render_file(dir, &format!("{ty}-body.txt.hbs"), &data, false)?;
    let html_name = format!("{ty}-body.html.hbs");
    let html = if dir.join(&html_name).exists() {
        render_file(dir, &html_name, &data, true)?
    } else {
        // No HTML template: proxmox-notify wraps the plain-text body.
        format!("<html><body><pre>{}</pre></body></html>", handlebars::html_escape(&text))
    };
    Ok(json!({ "subject": subject, "html": html, "text": text }))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        eprintln!("usage: proxmox-render <template-dir> <type> <data.json>");
        return ExitCode::from(2);
    }
    match run(Path::new(&args[1]), &args[2], Path::new(&args[3])) {
        Ok(v) => {
            println!("{v}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Expected strings from proxmox-human-byte and proxmox-time tests.
    #[test]
    fn formats_match_proxmox() {
        assert_eq!(human_bytes(9_000.0), "8.789 KiB");
        assert_eq!(human_bytes(1024.0), "1 KiB");
        assert_eq!(human_bytes(0.0), "0 B");
        assert_eq!(duration(0), "0s");
        assert_eq!(duration(125), "2m 5s");
        assert_eq!(duration(3600), "1h");
        assert_eq!(duration(90061), "1d 1h 1m 1s");
    }
}

//! The sentence a person reads (interface document, section 9). It is computed from
//! the token alone; for a narrowed token it describes what every level allows together.

use super::model::*;
use super::token::Mandate;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Description {
    pub sentence: String,
    pub lines: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    En,
    ZhTw,
}

impl Lang {
    pub fn from_tag(tag: &str) -> Option<Lang> {
        match tag {
            "en" => Some(Lang::En),
            "zh-TW" => Some(Lang::ZhTw),
            _ => None,
        }
    }
}

fn action_phrase(action: &str, lang: Lang) -> String {
    let (en, zh) = match action {
        "files.read" => ("read files", "讀取檔案"),
        "files.write" => ("save and change files", "儲存與修改檔案"),
        "mail.read" => ("read mail", "讀取郵件"),
        "mail.draft" => ("draft mail", "草擬郵件"),
        "mail.send" => ("send mail", "寄出郵件"),
        "calendar.read" => ("read calendar events", "讀取行事曆"),
        "calendar.write" => ("add and change calendar events", "新增與修改行事曆"),
        "llm.call" => ("ask AI services", "使用 AI 服務"),
        "llm.paid" => ("use paid AI services", "使用付費 AI 服務"),
        "pay.spend" => ("spend money", "付款"),
        "health.read" => ("read health records", "讀取健康紀錄"),
        "records.read" => ("read records", "讀取紀錄"),
        "home.admin" => ("change the settings of your home", "變更家用主機的設定"),
        other => {
            return match lang {
                Lang::En => format!("do \"{other}\""),
                Lang::ZhTw => format!("執行「{other}」"),
            }
        }
    };
    match lang {
        Lang::En => en.to_string(),
        Lang::ZhTw => zh.to_string(),
    }
}

fn resource_phrase(r: &Resource, lang: Lang) -> String {
    let any = r.value == "*";
    let v = &r.value;
    match (lang, r.kind.as_str(), any) {
        (Lang::En, "folder", false) => format!("the folder {v}"),
        (Lang::En, "folder", true) => "any folder".to_string(),
        (Lang::En, "host", false) => format!("the service host {v}"),
        (Lang::En, "host", true) => "any service host".to_string(),
        (Lang::En, "calendar", false) => format!("the calendar {v}"),
        (Lang::En, "calendar", true) => "any calendar".to_string(),
        (Lang::En, "record_kind", false) => format!("records of the kind {v}"),
        (Lang::En, "record_kind", true) => "records of any kind".to_string(),
        (Lang::En, "provider", false) => format!("the provider {v}"),
        (Lang::En, "provider", true) => "any provider".to_string(),
        (Lang::En, _, false) => format!("the home {v}"),
        (Lang::En, _, true) => "any home".to_string(),
        (Lang::ZhTw, "folder", false) => format!("資料夾 {v}"),
        (Lang::ZhTw, "folder", true) => "任何資料夾".to_string(),
        (Lang::ZhTw, "host", false) => format!("服務主機 {v}"),
        (Lang::ZhTw, "host", true) => "任何服務主機".to_string(),
        (Lang::ZhTw, "calendar", false) => format!("行事曆 {v}"),
        (Lang::ZhTw, "calendar", true) => "任何行事曆".to_string(),
        (Lang::ZhTw, "record_kind", false) => format!("「{v}」類紀錄"),
        (Lang::ZhTw, "record_kind", true) => "任何類別的紀錄".to_string(),
        (Lang::ZhTw, "provider", false) => format!("服務商 {v}"),
        (Lang::ZhTw, "provider", true) => "任何服務商".to_string(),
        (Lang::ZhTw, _, false) => format!("家用主機 {v}"),
        (Lang::ZhTw, _, true) => "任何家用主機".to_string(),
    }
}

fn holder_phrase(holder: &str, lang: Lang) -> String {
    let (kind, id) = holder.split_once(':').unwrap_or(("", holder));
    match (lang, kind) {
        (Lang::En, "helper") => format!("Your helper \"{id}\""),
        (Lang::En, "device") => format!("Your device \"{id}\""),
        (Lang::En, "app") => format!("The app \"{id}\""),
        (Lang::En, _) => format!("The clinician \"{id}\""),
        (Lang::ZhTw, "helper") => format!("你的助手「{id}」"),
        (Lang::ZhTw, "device") => format!("你的裝置「{id}」"),
        (Lang::ZhTw, "app") => format!("應用程式「{id}」"),
        (Lang::ZhTw, _) => format!("醫師「{id}」"),
    }
}

/// `a`, `a and b`, `a, b and c`; in Chinese `a、b、c`.
fn join(items: &[String], lang: Lang) -> String {
    match (lang, items) {
        (Lang::ZhTw, _) => items.join("、"),
        (Lang::En, [rest @ .., last]) if !rest.is_empty() => format!("{} and {last}", rest.join(", ")),
        (Lang::En, _) => items.concat(),
    }
}

/// An amount in minor units as people write it: `NT$20`, `NT$20.50`, `1.50 USDC`.
pub fn format_amount(kind: &str, unit: &str, decimals: i64, amount: i64, lang: Lang) -> String {
    if kind != "spend" {
        return match (lang, kind) {
            (Lang::En, "requests") => format!("{amount} requests"),
            (Lang::En, _) => format!("{amount} pushed messages"),
            (Lang::ZhTw, "requests") => format!("{amount} 次請求"),
            (Lang::ZhTw, _) => format!("{amount} 則推送訊息"),
        };
    }
    let digits = format!("{:0>width$}", amount, width = decimals as usize + 1);
    let (whole, frac) = digits.split_at(digits.len() - decimals as usize);
    let frac = frac.trim_end_matches('0');
    let number = if frac.is_empty() { whole.to_string() } else { format!("{whole}.{frac:0<width$}", width = 2.min(decimals as usize)) };
    match unit {
        "TWD" => format!("NT${number}"),
        "USD" => format!("US${number}"),
        "EUR" => format!("€{number}"),
        "JPY" => format!("JP¥{number}"),
        _ => format!("{number} {unit}"),
    }
}

/// `2026-11-30 00:00:00 UTC`.
pub fn format_time(t: i64) -> String {
    let text = super::token::rfc3339(t);
    format!("{} {} UTC", &text[..10], &text[11..19])
}

/// The sentence and its lines, in `lang`.
pub fn describe(m: &Mandate, lang: Lang) -> Description {
    let en = lang == Lang::En;
    let holder = holder_phrase(&m.draft.holder, lang);
    let actions: Vec<String> = m.actions().iter().map(|a| action_phrase(a, lang)).collect();
    let resources: Vec<String> = m.resources().iter().map(|r| resource_phrase(r, lang)).collect();
    let sentence = if actions.is_empty() || resources.is_empty() {
        if en {
            format!("{holder} may do nothing: this narrowed copy allows no action.")
        } else {
            format!("{holder}不能做任何事：這份縮小後的許可不允許任何動作。")
        }
    } else if en {
        format!("{holder} may {}, limited to {}.", join(&actions, lang), join(&resources, lang))
    } else {
        format!("{holder}可以{}，範圍限於{}。", join(&actions, lang), join(&resources, lang))
    };

    let mut lines = Vec::new();
    let limits = m.limits();
    for l in &limits {
        let amount = format_amount(&l.kind, &l.unit, l.decimals, l.amount, lang);
        lines.push(match (en, l.period.as_str()) {
            (true, "day") => format!("Up to {amount} a day"),
            (true, _) => format!("Up to {amount} a month"),
            (false, "day") => format!("每日最多 {amount}"),
            (false, _) => format!("每月最多 {amount}"),
        });
    }
    if !limits.is_empty() {
        let zone = &m.draft.period_zone;
        lines.push(if en { format!("Days and months are counted in {zone} time") } else { format!("每日與每月以 {zone} 時間計算") });
    }
    for c in m.caps() {
        let amount = format_amount(&c.kind, &c.unit, m.decimals(&c.kind, &c.unit), c.amount, lang);
        lines.push(if en { format!("No more than {amount} at a time") } else { format!("每次最多 {amount}") });
    }
    let (from, until) = (format_time(m.not_before()), format_time(m.not_after()));
    lines.push(if en { format!("From {from} until {until}") } else { format!("自 {from} 起，至 {until} 止") });

    let rules = m.confirm_rules();
    if rules.contains(&Confirm::EachWrite) {
        lines.push(if en { "Asks you before every change or payment".to_string() } else { "每次變更或付款前都會先問你".to_string() });
    } else {
        // One line per unit, with the lowest threshold in the chain.
        let mut over: Vec<(String, i64, i64)> = Vec::new();
        for rule in &rules {
            if let Confirm::OverAmount { unit, decimals, amount } = rule {
                match over.iter_mut().find(|o| o.0 == *unit) {
                    Some(o) => o.2 = o.2.min(*amount),
                    None => over.push((unit.clone(), *decimals, *amount)),
                }
            }
        }
        over.sort();
        for (unit, decimals, amount) in &over {
            let money = format_amount("spend", unit, *decimals, *amount, lang);
            lines.push(if en { format!("Asks you before any payment over {money}, and before every other change") } else { format!("超過 {money} 的付款，以及其他每次變更，都會先問你") });
        }
        if over.is_empty() {
            lines.push(if en { "Does not ask before acting".to_string() } else { "行動前不會先問你".to_string() });
        }
    }
    let effective = m.actions();
    let routine: Vec<String> = m.draft.routine.iter().filter(|a| effective.contains(a)).map(|a| action_phrase(a, lang)).collect();
    if !routine.is_empty() && rules != [Confirm::Never] {
        lines.push(if en { format!("Without asking each time: {}", join(&routine, lang)) } else { format!("不必每次詢問：{}", join(&routine, lang)) });
    }
    lines.push(
        match (en, m.privacy()) {
            (true, Privacy::Local) => "Nothing may leave your home: local models only",
            (true, Privacy::Private) => "May send data only to services rated private",
            (true, Privacy::Standard) => "May send data to services rated standard or private",
            (true, Privacy::Public) => "May send data to any service",
            (false, Privacy::Local) => "資料不得離開你的家用主機：只用本機模型",
            (false, Privacy::Private) => "資料只能送往「私密」等級的服務",
            (false, Privacy::Standard) => "資料可送往「標準」或「私密」等級的服務",
            (false, Privacy::Public) => "資料可送往任何服務",
        }
        .to_string(),
    );
    let (text, code) = (&m.draft.purpose_text, &m.draft.purpose_code);
    if !text.is_empty() || !code.is_empty() {
        lines.push(match (en, code.is_empty()) {
            (true, true) => format!("Purpose: {text}"),
            (true, false) => format!("Purpose: {text} ({code})"),
            (false, true) => format!("用途：{text}"),
            (false, false) => format!("用途：{text}（{code}）"),
        });
    }
    for id in m.jobs() {
        lines.push(if en { format!("Only for the job {id}") } else { format!("只限工作 {id}") });
    }
    for id in m.tools() {
        lines.push(if en { format!("Only for one tool call ({id})") } else { format!("只限一次工具呼叫（{id}）") });
    }
    for id in m.cards() {
        lines.push(if en { format!("Only for one approval ({id})") } else { format!("只限一次核准（{id}）") });
    }
    Description { sentence, lines }
}

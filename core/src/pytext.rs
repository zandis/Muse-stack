//! Python's text rules, where they differ from Rust's and the difference is observable.
//!
//! `str.strip()`/`split()`, `int()`, `float()`, `str.isdigit()` and `repr(float)`
//! are part of the behaviour of the functions ported here (a header value goes
//! through them), so they are reproduced rather than approximated.

/// First code point of every run of ten Unicode decimal digits (category Nd),
/// from CPython 3.13's unicodedata (Unicode 15.1; identical in 15.0).
const DECIMAL_ZEROS: [u32; 68] = [
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6,
    0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90, 0x1b50,
    0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10, 0x104a0,
    0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0, 0x11650, 0x116c0,
    0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60, 0x16ac0, 0x16b50,
    0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e950, 0x1fbf0,
];

/// Code points with Numeric_Type=Digit that are not decimal digits (superscripts,
/// circled digits, ...): `str.isdigit()` is true for them, `int()` rejects them.
const OTHER_DIGITS: [(u32, u32); 20] = [
    (0xb2, 0xb3), (0xb9, 0xb9), (0x1369, 0x1371), (0x19da, 0x19da), (0x2070, 0x2070),
    (0x2074, 0x2079), (0x2080, 0x2089), (0x2460, 0x2468), (0x2474, 0x247c), (0x2488, 0x2490),
    (0x24ea, 0x24ea), (0x24f5, 0x24fd), (0x24ff, 0x24ff), (0x2776, 0x277e), (0x2780, 0x2788),
    (0x278a, 0x2792), (0x10a40, 0x10a43), (0x10e60, 0x10e68), (0x11052, 0x1105a), (0x1f100, 0x1f10a),
];

/// `str.isspace()` for one character: Unicode White_Space plus U+001C..U+001F.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `str.split()` with no argument.
pub fn split_ws(s: &str) -> Vec<&str> {
    s.split(is_space).filter(|part| !part.is_empty()).collect()
}

/// The value of a Unicode decimal digit, as `int()` reads it.
pub fn decimal_value(c: char) -> Option<u32> {
    let cp = c as u32;
    if cp < 0x80 {
        return c.to_digit(10);
    }
    let at = DECIMAL_ZEROS.partition_point(|zero| *zero <= cp);
    let zero = DECIMAL_ZEROS[at.checked_sub(1)?];
    (cp - zero < 10).then_some(cp - zero)
}

/// `str.isdigit()` for one character.
pub fn is_digit(c: char) -> bool {
    let cp = c as u32;
    decimal_value(c).is_some() || OTHER_DIGITS.iter().any(|(lo, hi)| (*lo..=*hi).contains(&cp))
}

/// Strip, map Unicode decimal digits to ASCII and drop the underscores Python
/// allows between two digits. None when an underscore is misplaced.
///
/// `int()` and `float()` strip a narrower set than `str.strip()`: U+001C..U+001F
/// are not skipped, which is exactly Rust's `char::is_whitespace`.
fn numeric_ascii(s: &str) -> Option<String> {
    let mapped: Vec<char> = s
        .trim()
        .chars()
        .map(|c| match decimal_value(c) {
            Some(d) => char::from_digit(d, 10).unwrap_or('?'),
            None => c,
        })
        .collect();
    let mut out = String::with_capacity(mapped.len());
    for (i, c) in mapped.iter().enumerate() {
        if *c == '_' {
            let before = i.checked_sub(1).map(|j| mapped[j].is_ascii_digit()).unwrap_or(false);
            let after = mapped.get(i + 1).map(|n| n.is_ascii_digit()).unwrap_or(false);
            if !(before && after) {
                return None;
            }
        } else {
            out.push(*c);
        }
    }
    Some(out)
}

/// `int(s)` in base 10. None stands for ValueError. Values beyond i128 saturate;
/// every caller rejects numbers that large anyway.
pub fn py_int(s: &str) -> Option<i128> {
    let text = numeric_ascii(s)?;
    let (negative, digits) = match text.as_bytes().first()? {
        b'-' => (true, &text[1..]),
        b'+' => (false, &text[1..]),
        _ => (false, &text[..]),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let mut value: i128 = 0;
    for b in digits.bytes() {
        value = value.saturating_mul(10).saturating_add(i128::from(b - b'0'));
    }
    Some(if negative { -value } else { value })
}

/// `float(s)`. None stands for ValueError.
pub fn py_float(s: &str) -> Option<f64> {
    let text = numeric_ascii(s)?;
    let body = text.strip_prefix(['+', '-']).unwrap_or(&text);
    let lower = body.to_ascii_lowercase();
    let negative = text.starts_with('-');
    match lower.as_str() {
        "inf" | "infinity" => return Some(if negative { f64::NEG_INFINITY } else { f64::INFINITY }),
        "nan" => return Some(f64::NAN),
        _ => {}
    }
    {
        // [digits][.[digits]][e[sign]digits], with a digit before or after the point.
        let bytes = body.as_bytes();
        let mut i = 0;
        let digits = |i: &mut usize| {
            let start = *i;
            while *i < bytes.len() && bytes[*i].is_ascii_digit() {
                *i += 1;
            }
            *i - start
        };
        let mut mantissa = digits(&mut i);
        if i < bytes.len() && bytes[i] == b'.' {
            i += 1;
            mantissa += digits(&mut i);
        }
        if mantissa == 0 {
            return None;
        }
        if i < bytes.len() && (bytes[i] == b'e' || bytes[i] == b'E') {
            i += 1;
            if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') {
                i += 1;
            }
            if digits(&mut i) == 0 {
                return None;
            }
        }
        if i != bytes.len() {
            return None;
        }
    }
    text.parse::<f64>().ok()
}

/// `repr(x)` / `str(x)` of a float: the shortest digits that round-trip, fixed
/// notation for exponents -4..16, otherwise `d.ddde+XX`.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_string();
    }
    if x.is_infinite() {
        return if x < 0.0 { "-inf" } else { "inf" }.to_string();
    }
    let sign = if x.is_sign_negative() { "-" } else { "" };
    if x == 0.0 {
        return format!("{sign}0.0");
    }
    // Rust's {:e} gives the same shortest digits; only the layout differs.
    let sci = format!("{:e}", x.abs());
    let (mantissa, exponent) = sci.split_once('e').unwrap_or((&sci, "0"));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let exp: i32 = exponent.parse().unwrap_or(0);
    let point = exp + 1; // value = 0.DIGITS * 10^point
    let n = digits.len() as i32;
    if point <= -4 || point > 16 {
        let mut out = format!("{sign}{}", &digits[..1]);
        if n > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let exp_sign = if exp < 0 { '-' } else { '+' };
        out.push_str(&format!("e{exp_sign}{:02}", exp.abs()));
        out
    } else if point <= 0 {
        format!("{sign}0.{}{digits}", "0".repeat((-point) as usize))
    } else if point >= n {
        format!("{sign}{digits}{}.0", "0".repeat((point - n) as usize))
    } else {
        format!("{sign}{}.{}", &digits[..point as usize], &digits[point as usize..])
    }
}

/// Python's `max(a, b)` for floats: the first argument wins ties and NaN comparisons.
pub fn fmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's `min(a, b)` for floats: the first argument wins ties and NaN comparisons.
pub fn fmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's `x % y` for floats (the result takes the sign of `y`).
pub fn fmod(x: f64, y: f64) -> f64 {
    let m = x % y;
    if m != 0.0 {
        if (y < 0.0) != (m < 0.0) { m + y } else { m }
    } else {
        0.0_f64.copysign(y)
    }
}

/// Python truthiness of an optional float: None and 0.0 are false, NaN is true.
pub fn truthy(value: Option<f64>) -> bool {
    matches!(value, Some(v) if v != 0.0)
}

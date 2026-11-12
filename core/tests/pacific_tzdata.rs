//! The Python computes US Pacific time from the post-2007 rule (2nd Sunday of
//! March, 1st Sunday of November) without tzdata, and the Rust port does the
//! same. This checks that rule against the tz database for the years it is
//! meant for, and pins the one place it knowingly differs: before 2007.

use chrono::{Days, NaiveDateTime, TimeZone, Utc};
use chrono_tz::America::Los_Angeles;
use muse_core::{quota, ration};

fn utc(text: &str) -> f64 {
    NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S").unwrap().and_utc().timestamp() as f64
}

/// (start, end) of the Los Angeles calendar day containing `now`, from tzdata.
fn la_day(now: i64) -> (f64, f64) {
    let date = Utc.timestamp_opt(now, 0).unwrap().with_timezone(&Los_Angeles).date_naive();
    let midnight = |d: chrono::NaiveDate| {
        Los_Angeles.from_local_datetime(&d.and_hms_opt(0, 0, 0).unwrap()).single().unwrap().timestamp() as f64
    };
    (midnight(date), midnight(date.checked_add_days(Days::new(1)).unwrap()))
}

#[test]
fn pacific_midnights_match_tzdata_from_2007_to_2050() {
    let (from, to) = (utc("2007-01-01T00:00:00") as i64, utc("2051-01-01T00:00:00") as i64);
    let mut checked = 0;
    let mut now = from;
    while now < to {
        // Every 37 minutes: all hours and many minutes of every day, both switch days of every year.
        let (start, end) = la_day(now);
        assert_eq!(quota::next_midnight_pacific(now as f64), Ok(end), "next midnight at {now}");
        assert_eq!(ration::previous_midnight_pacific(now as f64), Ok(start), "previous midnight at {now}");
        checked += 1;
        now += 37 * 60;
    }
    assert!(checked > 600_000);
}

#[test]
fn the_seconds_around_each_switch_match_tzdata() {
    for year in 2007..=2050 {
        let (from, to) = (utc(&format!("{year}-03-07T00:00:00")) as i64, utc(&format!("{year}-03-16T00:00:00")) as i64);
        let (from2, to2) = (utc(&format!("{year}-10-31T00:00:00")) as i64, utc(&format!("{year}-11-09T00:00:00")) as i64);
        for hour_start in (from..to).chain(from2..to2).step_by(3600) {
            for now in [hour_start - 1, hour_start, hour_start + 1] {
                let (start, end) = la_day(now);
                assert_eq!(quota::next_midnight_pacific(now as f64), Ok(end), "next midnight at {now}");
                assert_eq!(ration::previous_midnight_pacific(now as f64), Ok(start), "previous midnight at {now}");
            }
        }
    }
}

#[test]
fn before_2007_the_rule_is_not_the_historical_one() {
    // 20 March 2006: the US was still on standard time (DST began 2 April), but
    // the 2007 rule says daylight time. Python answers 07:00Z; so does the port.
    let now = utc("2006-03-20T12:00:00");
    assert_eq!(quota::next_midnight_pacific(now), Ok(utc("2006-03-21T07:00:00")));
    assert_eq!(la_day(now as i64).1, utc("2006-03-21T08:00:00"));
}

#[test]
fn gemini_day_start_is_an_hour_off_on_switch_days() {
    // quota.day_start is "next Pacific midnight - 24 h" (Python's comment: "close
    // enough on DST days"); the exact start is ration.previous_midnight_pacific.
    let spring = utc("2026-03-08T20:00:00"); // 13:00 PDT on the 23-hour day
    assert_eq!(quota::day_start(spring, Some("gemini")), Ok(utc("2026-03-08T07:00:00")));
    assert_eq!(ration::previous_midnight_pacific(spring), Ok(utc("2026-03-08T08:00:00")));
    let autumn = utc("2026-11-01T20:00:00"); // 12:00 PST on the 25-hour day
    assert_eq!(quota::day_start(autumn, Some("gemini")), Ok(utc("2026-11-01T08:00:00")));
    assert_eq!(ration::previous_midnight_pacific(autumn), Ok(utc("2026-11-01T07:00:00")));
}

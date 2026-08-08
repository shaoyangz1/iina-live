//! B 站扫码登录 + cookie 管理。
//!
//! 流程：生成二维码 → 终端 Unicode 渲染 → 轮询确认 → cookie 落盘。
//! 刷新：使用 refresh_token 换新 cookie。

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use rand::rngs::OsRng;
use rsa::{Oaep, RsaPublicKey};
use sha2::Sha256;

use crate::common;

const UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15";

const QR_GENERATE: &str = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate";
const QR_POLL: &str = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll";
const COOKIE_INFO: &str = "https://passport.bilibili.com/x/passport-login/web/cookie/info";
const COOKIE_REFRESH: &str = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh";
const CONFIRM_REFRESH: &str = "https://passport.bilibili.com/x/passport-login/web/confirm/refresh";
const NAV: &str = "https://api.bilibili.com/x/web-interface/nav";
const CORRESPOND: &str = "https://www.bilibili.com/correspond/1/";
const RSA_N: &str = "y4HdjgJHBlbaBN04VERG4qNBIFHP6a3GozCl75AihQloSWCXC5HDNgyinEnhaQ_4-gaMud_GF50elYXLlCToR9se9Z8z433U3KjM-3Yx7ptKkmQNAMggQwAVKgq3zYAoidNEWuxpkY_mAitTSRLnsJW-NCTa0bqBFF6Wm1MxgfE";
const RSA_E: u32 = 65537;
const COOKIE_KEYS: [&str; 4] = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"];

/// cookie 文件路径（项目根目录下的 .cookie/bilibili）
fn cookie_path() -> PathBuf {
    project_root().join(".cookie").join("bilibili")
}

fn refresh_token_path() -> PathBuf {
    project_root()
        .join(".cookie")
        .join("bilibili.refresh_token")
}

fn project_root() -> PathBuf {
    common::data_dir()
}

#[cfg(unix)]
fn set_private_permissions(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o600);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn set_private_permissions(_path: &std::path::Path) -> std::io::Result<()> {
    Ok(())
}

fn api_headers() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA.into()),
        ("Referer".into(), "https://www.bilibili.com/".into()),
    ])
}

// ---- Cookie 文件读写 ----

/// 读取本地 cookie 文件内容（SESSDATA=xxx; bili_jct=xxx; ...），不存在返回 None。
pub fn load_cookie() -> Option<String> {
    let path = cookie_path();
    fs::read_to_string(&path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// 保存 cookie 字符串到文件（上级目录自动创建）。
pub fn save_cookie(cookie: &str) -> std::io::Result<()> {
    let path = cookie_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&path, cookie)?;
    set_private_permissions(&path)?;
    Ok(())
}

fn load_refresh_token() -> Option<String> {
    let path = refresh_token_path();
    fs::read_to_string(&path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn save_refresh_token(token: &str) -> std::io::Result<()> {
    let path = refresh_token_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&path, token)?;
    set_private_permissions(&path)?;
    Ok(())
}

/// 从 cookie 字符串中取某字段的值（无 → None）。
fn cookie_value(cookie: &str, key: &str) -> Option<String> {
    for part in cookie.split(';') {
        let part = part.trim();
        if let Some((_, v)) = part.split_once('=') {
            let k = part[..part.find('=').unwrap()].trim();
            if k == key {
                return Some(v.to_string());
            }
        }
    }
    None
}

/// 估算 cookie 到期时间戳。
fn cookie_expiry(cookie: &str) -> Option<i64> {
    let sess = cookie_value(cookie, "SESSDATA")?;
    let decoded = sess.replace("%2C", ",").replace("%2c", ",");
    decoded.split(',').nth(1)?.parse::<i64>().ok()
}

// ---- QR 终端渲染 ----

/// 用 Unicode 字符在终端渲染 QR 码。
pub fn render_qr(content: &str) -> String {
    let code = qrcode::QrCode::new(content.as_bytes()).unwrap();
    let dark = "\u{2588}\u{2588}"; // full block ×2
    let light = "  ";
    let mut out = String::new();
    let width = code.width();
    for y in 0..width {
        for x in 0..width {
            if code[(x, y)] != qrcode::Color::Light {
                out.push_str(dark);
            } else {
                out.push_str(light);
            }
        }
        out.push('\n');
    }
    out
}

// ---- 扫码登录 ----

/// 生成二维码 key 和 URL。
async fn generate_qr(client: &reqwest::Client) -> anyhow::Result<(String, String)> {
    let raw = common::http_get(client, QR_GENERATE, Some(&api_headers())).await?;
    let v: serde_json::Value = serde_json::from_slice(&raw)?;
    if v["code"].as_i64() != Some(0) {
        anyhow::bail!(
            "获取二维码失败: {}",
            v["message"].as_str().unwrap_or("未知错误")
        );
    }
    let data = &v["data"];
    Ok((
        data["qrcode_key"].as_str().unwrap_or("").to_string(),
        data["url"].as_str().unwrap_or("").to_string(),
    ))
}

fn cookie_string_from_values(values: &[String]) -> Option<String> {
    let mut cookies = HashMap::new();
    for value in values {
        let Some(pair) = value.split(';').next() else {
            continue;
        };
        let Some((key, value)) = pair.split_once('=') else {
            continue;
        };
        if COOKIE_KEYS.contains(&key) {
            cookies.insert(key, value);
        }
    }
    let cookie = COOKIE_KEYS
        .iter()
        .filter_map(|key| cookies.get(key).map(|value| format!("{key}={value}")))
        .collect::<Vec<_>>()
        .join("; ");
    (!cookie.is_empty()).then_some(cookie)
}

fn cookie_string_from_url(value: &str) -> Option<String> {
    let parsed = url::Url::parse(value).ok()?;
    let mut cookies = HashMap::new();
    for (key, value) in parsed.query_pairs() {
        if COOKIE_KEYS.contains(&key.as_ref()) {
            cookies.insert(key.to_string(), value.to_string());
        }
    }
    let cookie = COOKIE_KEYS
        .iter()
        .filter_map(|key| cookies.get(*key).map(|value| format!("{key}={value}")))
        .collect::<Vec<_>>()
        .join("; ");
    (!cookie.is_empty()).then_some(cookie)
}

/// 扫码获取 cookie（从响应 Set-Cookie 头或成功 URL 提取）。
async fn poll_with_cookie(
    client: &reqwest::Client,
    qrcode_key: &str,
) -> anyhow::Result<(i64, Option<String>, Option<String>)> {
    let url = format!("{QR_POLL}?qrcode_key={qrcode_key}&source=main-fe-header");
    let mut headers = api_headers();
    headers.insert("Origin".into(), "https://www.bilibili.com".into());

    let resp = client
        .get(&url)
        .headers({
            let mut hm = reqwest::header::HeaderMap::new();
            for (k, v) in &headers {
                if let (Ok(k), Ok(v)) = (
                    reqwest::header::HeaderName::from_bytes(k.as_bytes()),
                    reqwest::header::HeaderValue::from_str(v),
                ) {
                    hm.insert(k, v);
                }
            }
            hm
        })
        .send()
        .await?;
    let set_cookie_headers: Vec<String> = resp
        .headers()
        .get_all("set-cookie")
        .iter()
        .filter_map(|v| v.to_str().ok().map(|s| s.to_string()))
        .collect();

    let raw = resp.bytes().await?;
    let v: serde_json::Value = serde_json::from_slice(&raw)?;
    let code = v["data"]["code"].as_i64().unwrap_or(-1);

    if code != 0 {
        return Ok((code, None, None));
    }

    let cookie_str = v["data"]["url"]
        .as_str()
        .and_then(cookie_string_from_url)
        .or_else(|| cookie_string_from_values(&set_cookie_headers));
    let token = v["data"]["refresh_token"].as_str().map(str::to_string);
    Ok((code, cookie_str, token))
}

/// 执行扫码登录，返回 cookie 字符串。
pub async fn login(client: &reqwest::Client) -> anyhow::Result<String> {
    let (key, url) = generate_qr(client).await?;
    let qr_text = render_qr(&url);

    println!("{}", qr_text);
    println!("请用「哔哩哔哩」手机 App 扫描上面的二维码并确认登录。(Ctrl+C 取消)\n");

    let poll_client = reqwest::Client::new();
    let deadline = SystemTime::now() + Duration::from_secs(180);
    let mut last_msg = String::new();

    while SystemTime::now() < deadline {
        let (code, cookie, token) = poll_with_cookie(&poll_client, &key).await?;

        match code {
            0 => {
                let cookie = cookie.ok_or_else(|| anyhow::anyhow!("登录成功但未取到 cookie"))?;
                save_cookie(&cookie)?;
                if let Some(t) = token {
                    save_refresh_token(&t)?;
                }
                println!("登录成功");
                print_login_info(&cookie).await?;
                return Ok(cookie);
            }
            86038 => {
                anyhow::bail!("二维码已失效，请重新运行登录。");
            }
            86101 => {
                let msg = "等待扫码…";
                if msg != last_msg {
                    println!("{}", msg);
                    last_msg = msg.to_string();
                }
            }
            86090 => {
                let msg = "已扫码，请在手机上确认…";
                if msg != last_msg {
                    println!("{}", msg);
                    last_msg = msg.to_string();
                }
            }
            c => {
                let msg = format!("状态 {c}");
                if msg != last_msg {
                    println!("{}", msg);
                    last_msg = msg;
                }
            }
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    anyhow::bail!("登录超时（180s），请重试。")
}

/// 打印登录用户信息。
pub async fn print_login_info(cookie: &str) -> anyhow::Result<()> {
    let client = reqwest::Client::new();
    let mut h = api_headers();
    h.insert("Cookie".into(), cookie.to_string());
    let raw = common::http_get(&client, NAV, Some(&h)).await?;
    let v: serde_json::Value = serde_json::from_slice(&raw)?;
    let data = &v["data"];
    if data.get("isLogin").and_then(|x| x.as_bool()) != Some(true) {
        println!("cookie 已失效（接口返回未登录）。请重新登录。");
        return Ok(());
    }
    let uname = data["uname"].as_str().unwrap_or("?");
    let vip = match data["vipType"].as_i64() {
        Some(0) => "非大会员",
        Some(1) => "大会员",
        Some(2) => "年度大会员",
        _ => "未知",
    };
    println!("已登录: {uname}");
    println!("会员  : {vip}");
    if let Some(exp) = cookie_expiry(cookie) {
        let left = (exp
            - SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64) as f64
            / 86400.0;
        println!("cookie: {} 天", left.round() as i64);
    }
    Ok(())
}

// ---- 登录刷新 ----

fn correspond_path(timestamp: i64) -> anyhow::Result<String> {
    let modulus = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(RSA_N)?;
    let key = RsaPublicKey::new(
        rsa::BigUint::from_bytes_be(&modulus),
        rsa::BigUint::from(RSA_E),
    )?;
    let encrypted = key.encrypt(
        &mut OsRng,
        Oaep::new::<Sha256>(),
        format!("refresh_{timestamp}").as_bytes(),
    )?;
    Ok(encrypted.iter().map(|byte| format!("{byte:02x}")).collect())
}

async fn refresh_csrf(
    client: &reqwest::Client,
    cookie: &str,
    timestamp: i64,
) -> anyhow::Result<String> {
    let path = correspond_path(timestamp)?;
    let url = format!("{CORRESPOND}{path}");
    let mut headers = api_headers();
    headers.insert("Cookie".into(), cookie.to_string());
    let body = common::http_get_text(client, &url, Some(&headers)).await?;
    let marker = "id=\"1-name\">";
    let value = body
        .split_once(marker)
        .and_then(|(_, rest)| rest.split_once("</div>"))
        .map(|(value, _)| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow::anyhow!("未取得 refresh_csrf"))?;
    Ok(value
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\""))
}

fn header_map(headers: &HashMap<String, String>) -> reqwest::header::HeaderMap {
    let mut result = reqwest::header::HeaderMap::new();
    for (key, value) in headers {
        if let (Ok(key), Ok(value)) = (
            reqwest::header::HeaderName::from_bytes(key.as_bytes()),
            reqwest::header::HeaderValue::from_str(value),
        ) {
            result.insert(key, value);
        }
    }
    result
}

/// 刷新登录态：用 refresh_token 换新 cookie。
pub async fn refresh(client: &reqwest::Client) -> anyhow::Result<String> {
    let cookie = load_cookie().ok_or_else(|| anyhow::anyhow!("缺少登录凭据，请先扫码登录"))?;
    let token = load_refresh_token()
        .ok_or_else(|| anyhow::anyhow!("缺少 refresh_token，请重新扫码登录"))?;

    let mut info_headers = api_headers();
    info_headers.insert("Cookie".into(), cookie.clone());
    let raw = common::http_get(client, COOKIE_INFO, Some(&info_headers)).await?;
    let info: serde_json::Value = serde_json::from_slice(&raw)?;
    if !info["data"]["refresh"].as_bool().unwrap_or(false) {
        println!("当前无需刷新");
        print_login_info(&cookie).await?;
        return Ok(cookie);
    }

    let timestamp = info["data"]["timestamp"].as_i64().unwrap_or_else(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis() as i64)
            .unwrap_or_default()
    });
    let refresh_csrf = refresh_csrf(client, &cookie, timestamp).await?;
    let csrf = cookie_value(&cookie, "bili_jct")
        .ok_or_else(|| anyhow::anyhow!("cookie 缺少 bili_jct，请重新扫码登录"))?;

    let response = client
        .post(COOKIE_REFRESH)
        .headers(header_map(&api_headers()))
        .header("Cookie", &cookie)
        .form(&[
            ("csrf", csrf.as_str()),
            ("refresh_csrf", refresh_csrf.as_str()),
            ("source", "main_web"),
            ("refresh_token", token.as_str()),
        ])
        .send()
        .await?;
    let set_cookie: Vec<String> = response
        .headers()
        .get_all("set-cookie")
        .iter()
        .filter_map(|value| value.to_str().ok().map(str::to_string))
        .collect();
    let result: serde_json::Value = response.json().await?;
    if result["code"].as_i64() != Some(0) {
        anyhow::bail!(
            "刷新失败: {}",
            result["message"].as_str().unwrap_or("未知错误")
        );
    }

    let new_cookie = cookie_string_from_values(&set_cookie)
        .ok_or_else(|| anyhow::anyhow!("接口未返回新 cookie，请重新扫码登录"))?;
    let new_token = result["data"]["refresh_token"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("接口未返回新 refresh_token，请重新扫码登录"))?;
    let new_csrf = cookie_value(&new_cookie, "bili_jct").unwrap_or_default();

    let confirm = client
        .post(CONFIRM_REFRESH)
        .headers(header_map(&api_headers()))
        .header("Cookie", &new_cookie)
        .form(&[
            ("csrf", new_csrf.as_str()),
            ("refresh_token", token.as_str()),
        ])
        .send()
        .await?
        .json::<serde_json::Value>()
        .await?;
    if confirm["code"].as_i64() != Some(0) {
        anyhow::bail!(
            "刷新确认失败: {}",
            confirm["message"].as_str().unwrap_or("未知错误")
        );
    }

    save_cookie(&new_cookie)?;
    save_refresh_token(new_token)?;
    println!("刷新成功");
    print_login_info(&new_cookie).await?;
    Ok(new_cookie)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_only_cookie_pairs_from_set_cookie() {
        let values = vec![
            "SESSDATA=session; Path=/; HttpOnly".to_string(),
            "bili_jct=csrf; Path=/".to_string(),
            "ignored=value; Path=/".to_string(),
        ];
        assert_eq!(
            cookie_string_from_values(&values).as_deref(),
            Some("SESSDATA=session; bili_jct=csrf")
        );
    }

    #[test]
    fn reads_expiry_from_second_sessdata_field() {
        assert_eq!(
            cookie_expiry("SESSDATA=a%2C1700000000%2Cb"),
            Some(1700000000)
        );
    }

    #[test]
    fn builds_rsa_correspond_path() {
        let path = correspond_path(1700000000000).expect("RSA encryption");
        assert_eq!(path.len(), 256);
        assert!(path.bytes().all(|byte| byte.is_ascii_hexdigit()));
    }
}

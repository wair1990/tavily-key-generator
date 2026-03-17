"""
使用 Camoufox + Solver 完成注册
思路：从真实注册页开始，自动处理 Turnstile、邮箱验证码和密码设置
"""
import os
import re
import threading
import time
import requests as std_requests
from camoufox.sync_api import Camoufox
from config import (
    API_KEY_TIMEOUT,
    EMAIL_CODE_TIMEOUT,
    LOCAL_SOLVER_URL,
    REGISTER_HEADLESS,
)
from mail_provider import get_email_code, get_verification_link

TURNSTILE_SITEKEY = "0x4AAAAAAAQFNSW6xordsuIq"
_HERE = os.path.dirname(os.path.abspath(__file__))
_SAVE_FILE = os.path.join(_HERE, "accounts.txt")
_SAVE_LOCK = threading.Lock()

def extract_signup_url(html):
    """从登录密码页里提取注册入口"""
    match = re.search(r'href="(/u/signup/identifier[^"]*)"', html)
    if not match:
        return None
    return f"https://auth.tavily.com{match.group(1)}"


def has_unified_identifier_entry(page):
    """新版 Tavily 已改成登录/注册合一入口，当前页可直接作为注册起点。"""
    selectors = [
        'input[name="username"]',
        'input[name="email"]',
        'input[type="email"]',
    ]
    has_email_input = any(page.query_selector(selector) for selector in selectors)
    has_continue = any(
        page.query_selector(selector)
        for selector in (
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Sign Up")',
        )
    )
    return has_email_input and has_continue

def fill_first_input(page, selectors, value):
    """填充第一个存在的输入框"""
    for selector in selectors:
        if page.query_selector(selector):
            page.fill(selector, value)
            return selector
    return None

def close_marketing_dialog(page):
    """关闭首页营销弹窗，避免遮挡 API Key 区域"""
    close_button = page.query_selector('button[aria-label="Close"]')
    if close_button:
        close_button.click()
        time.sleep(1)

def extract_api_key(page):
    """从当前页面 HTML 里提取明文 API Key"""
    html = page.content()
    api_key_matches = re.findall(r'tvly-[a-zA-Z0-9_-]{20,}', html)
    api_keys = [k for k in api_key_matches if k != "tvly-YOUR_API_KEY"]
    if not api_keys:
        return None
    return max(api_keys, key=len)

def wait_for_api_key(page, timeout=20):
    """等待首页 API Key 模块渲染出明文 key"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        close_marketing_dialog(page)
        api_key = extract_api_key(page)
        if api_key:
            return api_key
        time.sleep(1)
    return None

def save_account(email, password, api_key):
    """并发注册时串行写入 accounts.txt，避免落盘交叉。"""
    with _SAVE_LOCK:
        with open(_SAVE_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{email},{password},{api_key}\n")

def verify_api_key(api_key, timeout=30):
    """真实调用 Tavily API，验证新 key 可用。"""
    try:
        response = std_requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": "api key verification",
                "max_results": 1,
            },
            timeout=timeout,
        )
    except Exception as exc:
        print(f"❌ API Key 调用测试失败: {exc}")
        return False

    if response.status_code == 200:
        print("✅ API Key 调用测试通过")
        return True

    preview = response.text.strip().replace("\n", " ")[:160]
    print(f"❌ API Key 调用测试失败: HTTP {response.status_code}")
    if preview:
        print(f"   响应: {preview}")
    return False

def submit_primary_action(page, input_selector=None):
    """优先提交默认 Continue 动作，避免误点 Resend / Go back"""
    button_selectors = [
        'button[data-action-button-primary="true"]',
        'button[type="submit"][name="action"][value="default"]:not([aria-hidden="true"])',
        'button[type="submit"]:not([aria-hidden="true"])',
    ]

    for selector in button_selectors:
        if page.query_selector(selector):
            try:
                page.click(selector, no_wait_after=True, timeout=3000)
                return True
            except Exception:
                continue

    if input_selector and page.query_selector(input_selector):
        try:
            page.press(input_selector, 'Enter')
            return True
        except Exception:
            return False

    return False

def extract_page_feedback(page):
    """提取页面上的高价值提示，便于定位卡点。"""
    selectors = [
        '[role="alert"]',
        '[data-error-visible="true"]',
        '.ulp-input-error-message',
        '.auth0-global-message',
        '.cf-turnstile-error',
    ]
    messages = []
    for selector in selectors:
        for node in page.query_selector_all(selector):
            text = (node.inner_text() or "").strip()
            if text and text not in messages:
                messages.append(text)
    return " | ".join(messages)

def print_feedback_hint(feedback):
    """针对常见失败原因补一条更直接的提示。"""
    if not feedback:
        return

    lowered = feedback.lower()
    if "suspicious activity detected" in lowered:
        print("   提示: Tavily 当前会拦截公开 DuckMail 域名，建议改用私有 DuckMail 域名/API Key 或 Cloudflare。")
    elif "security challenge" in lowered:
        print("   提示: 当前卡在密码页二次安全挑战，程序已尝试自动重提，但目标站仍可触发额外风控。")

def wait_for_post_signup_target(page, timeout):
    """等待注册后跳转到 Tavily 或验证页。"""
    deadline = time.time() + (timeout / 1000)
    while time.time() < deadline:
        current_url = page.url.lower()
        if "app.tavily.com" in current_url or "/verify" in current_url or "/continue" in current_url:
            return True
        time.sleep(0.5)
    return False

def normalize_feedback(feedback):
    """统一页面提示文案，方便做关键字判断。"""
    return (feedback or "").replace("’", "'").strip().lower()

def get_turnstile_sitekey(page):
    """优先从当前页面提取 sitekey，拿不到再回退默认值。"""
    try:
        sitekey = page.evaluate(
            """
            () => {
                const node = document.querySelector(
                    '[data-captcha-sitekey], .cf-turnstile, [data-sitekey]'
                );
                if (!node) {
                    return '';
                }
                return (
                    node.getAttribute('data-captcha-sitekey') ||
                    node.getAttribute('data-sitekey') ||
                    ''
                );
            }
            """
        )
    except Exception:
        sitekey = ""

    if sitekey:
        return sitekey.strip()

    html = page.content()
    match = re.search(
        r'(?:data-captcha-sitekey|data-sitekey)=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    return TURNSTILE_SITEKEY

def collect_turnstile_state(page):
    """收集密码页 challenge 相关 DOM 状态，便于判断恢复策略。"""
    try:
        state = page.evaluate(
            """
            () => {
                const passwordInput = document.querySelector('input[name="password"]');
                const widget = document.querySelector(
                    'div[data-captcha-sitekey], .cf-turnstile, [data-sitekey]'
                );
                const iframe = document.querySelector(
                    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
                );
                const captchaInput = document.querySelector(
                    'input[name="captcha"], input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                );
                return {
                    hasCaptchaDiv: !!widget,
                    hasChallengeIframe: !!iframe,
                    hasCaptchaInput: !!captchaInput,
                    hasTurnstile: typeof window.turnstile !== 'undefined',
                    hasPasswordInput: !!passwordInput,
                    passwordValueLength: passwordInput ? passwordInput.value.length : 0,
                    sitekey: widget
                        ? (widget.getAttribute('data-captcha-sitekey') || widget.getAttribute('data-sitekey') || '')
                        : '',
                };
            }
            """
        )
    except Exception:
        state = {}

    return {
        "hasCaptchaDiv": bool(state.get("hasCaptchaDiv")),
        "hasChallengeIframe": bool(state.get("hasChallengeIframe")),
        "hasCaptchaInput": bool(state.get("hasCaptchaInput")),
        "hasTurnstile": bool(state.get("hasTurnstile")),
        "hasPasswordInput": bool(state.get("hasPasswordInput")),
        "passwordValueLength": int(state.get("passwordValueLength") or 0),
        "sitekey": (state.get("sitekey") or "").strip(),
    }

def has_password_challenge_signal(feedback=None, state=None):
    """判断当前是否出现了密码页安全挑战。"""
    lowered = normalize_feedback(feedback)
    if any(
        keyword in lowered
        for keyword in (
            "security challenge",
            "captcha",
            "turnstile",
            "cloudflare",
            "couldn't load the security challenge",
        )
    ):
        return True

    state = state or {}
    return any(
        (
            state.get("hasCaptchaDiv"),
            state.get("hasChallengeIframe"),
            state.get("hasCaptchaInput"),
            state.get("hasTurnstile"),
        )
    )

def format_turnstile_state(state):
    """压缩输出 challenge 状态，便于终端观察。"""
    return (
        f"captchaDiv={'Y' if state.get('hasCaptchaDiv') else 'N'}, "
        f"iframe={'Y' if state.get('hasChallengeIframe') else 'N'}, "
        f"input={'Y' if state.get('hasCaptchaInput') else 'N'}, "
        f"turnstile={'Y' if state.get('hasTurnstile') else 'N'}, "
        f"pwdLen={state.get('passwordValueLength', 0)}"
    )

def refill_password(page, password):
    """挑战失败后密码输入框经常会被清空，重填一次。"""
    selector = 'input[name="password"]'
    if not page.query_selector(selector):
        return False
    page.fill(selector, password)
    return True

def refresh_password_page_if_needed(page, feedback, state):
    """如果挑战脚本没加载出来，刷新当前密码页拿一遍新的 DOM。"""
    lowered = normalize_feedback(feedback)
    if "couldn't load the security challenge" not in lowered:
        return False

    # 新版 Auth0 页面经常会先渲染占位 captcha div / hidden input，
    # 但真正的 Cloudflare iframe 还没挂起来；这种情况也需要刷新。
    if state.get("hasChallengeIframe") or state.get("hasTurnstile"):
        return False

    print("🔄 检测到安全挑战加载失败，刷新密码页后重试...")
    try:
        page.reload(wait_until="networkidle", timeout=30000)
        page.wait_for_selector('input[name="password"]', timeout=15000)
        time.sleep(2)
        return True
    except Exception as exc:
        print(f"⚠️  刷新密码页失败: {exc}")
        return False


def wait_for_password_challenge_ready(page, timeout=8):
    """等待密码页的 Cloudflare challenge 尽量挂起。"""
    deadline = time.time() + timeout
    latest_state = {}
    while time.time() < deadline:
        latest_state = collect_turnstile_state(page)
        if latest_state.get("hasChallengeIframe") or latest_state.get("hasTurnstile"):
            return latest_state
        time.sleep(0.5)
    return latest_state


def ensure_password_challenge_ready(page):
    """密码页刚出现时先给 challenge 一个渲染窗口，必要时主动刷新一次。"""
    state = wait_for_password_challenge_ready(page, timeout=6)
    if not state.get("hasPasswordInput"):
        return state

    if state.get("hasChallengeIframe") or state.get("hasTurnstile"):
        return state

    if state.get("hasCaptchaDiv") or state.get("hasCaptchaInput"):
        print("🔄 密码页 challenge 未完全渲染，先刷新一次页面...")
        try:
            page.reload(wait_until="networkidle", timeout=30000)
            page.wait_for_selector('input[name="password"]', timeout=15000)
            time.sleep(2)
            state = wait_for_password_challenge_ready(page, timeout=6)
        except Exception as exc:
            print(f"⚠️  密码页预刷新失败: {exc}")

    return state

def recover_password_challenge(page, password, max_attempts=3):
    """密码页未跳转时，尝试等待 challenge 渲染并恢复提交流程。"""
    print("⚠️  密码页未完成跳转，开始处理安全挑战...")

    for attempt in range(1, max_attempts + 1):
        if wait_for_post_signup_target(page, timeout=5000):
            return True

        time.sleep(2)
        feedback = extract_page_feedback(page)
        state = collect_turnstile_state(page)

        print(f"🔁 密码页恢复尝试 {attempt}/{max_attempts}")
        print(f"   DOM: {format_turnstile_state(state)}")
        if feedback:
            print(f"   提示: {feedback}")

        if wait_for_post_signup_target(page, timeout=2000):
            return True

        if refresh_password_page_if_needed(page, feedback, state):
            feedback = extract_page_feedback(page)
            state = collect_turnstile_state(page)
            print(f"   刷新后 DOM: {format_turnstile_state(state)}")
            if feedback:
                print(f"   刷新后提示: {feedback}")

            if wait_for_post_signup_target(page, timeout=2000):
                return True

        if has_password_challenge_signal(feedback, state):
            sitekey = state.get("sitekey") or get_turnstile_sitekey(page)
            print(f"🔐 尝试恢复 Turnstile challenge (sitekey={sitekey})")
            token = solve_turnstile(page.url, sitekey=sitekey)
            if token:
                if inject_turnstile_token(page, token):
                    print("✅ 已注入密码页 challenge token")
                else:
                    print("⚠️  Token 已拿到，但页面未确认注入成功，继续重提")
            else:
                print("⚠️  密码页 challenge token 获取失败，继续执行普通重提")
        else:
            print("⏳ 未检测到显式 challenge DOM，执行延迟重提")

        if not refill_password(page, password):
            if wait_for_post_signup_target(page, timeout=5000):
                return True
            print("❌ 密码输入框丢失，无法继续恢复")
            return False

        time.sleep(1)
        submit_primary_action(page, 'input[name="password"]')
        time.sleep(4)

    return wait_for_post_signup_target(page, timeout=5000)

def submit_password_with_recovery(page, password):
    """提交密码，并在随机 challenge 场景下自动恢复。"""
    initial_state = ensure_password_challenge_ready(page)
    if initial_state:
        print(f"🔍 密码页 challenge 状态: {format_turnstile_state(initial_state)}")

    if not refill_password(page, password):
        return False

    time.sleep(1)
    submit_primary_action(page, 'input[name="password"]')
    time.sleep(5)

    if wait_for_post_signup_target(page, timeout=15000):
        return True

    return recover_password_challenge(page, password)

def solve_turnstile(url, sitekey=TURNSTILE_SITEKEY):
    """调用 Solver 获取 token"""
    try:
        r = std_requests.get(
            f"{LOCAL_SOLVER_URL}/turnstile",
            params={"url": url, "sitekey": sitekey or TURNSTILE_SITEKEY},
            timeout=10
        )

        if r.status_code != 200:
            print(f"❌ Solver 请求失败: {r.status_code}")
            return None

        task_id = r.json().get("taskId")
        if not task_id:
            print("❌ 未获取到 Task ID")
            return None

        # 轮询结果
        for i in range(60):
            time.sleep(2)
            res = std_requests.get(
                f"{LOCAL_SOLVER_URL}/result",
                params={"id": task_id},
                timeout=10
            )

            if res.status_code == 200:
                data = res.json()
                status = data.get("status")

                if status == "ready":
                    token = data.get("solution", {}).get("token")
                    if token:
                        return token
                elif status == "CAPTCHA_FAIL":
                    return None

        return None
    except Exception as e:
        print(f"❌ Solver 异常: {e}")
        return None

def inject_turnstile_token(page, token):
    """注入 Turnstile token 到页面"""
    safe_token = token.replace("\\", "\\\\").replace("'", "\\'")
    script = f"""
    (function() {{
        const token = '{safe_token}';
        const form = document.querySelector('form') || document.body;
        const names = ['captcha', 'cf-turnstile-response'];

        const ensureField = (name) => {{
            let field = document.querySelector(`input[name="${{name}}"], textarea[name="${{name}}"]`);
            if (field) {{
                return field;
            }}

            field = document.createElement(name.includes('response') ? 'textarea' : 'input');
            if (field.tagName === 'INPUT') {{
                field.type = 'hidden';
            }}
            field.name = name;
            form.appendChild(field);
            return field;
        }};

        names.forEach((name) => {{
            const field = ensureField(name);
            field.value = token;
            field.dispatchEvent(new Event('input', {{ bubbles: true }}));
            field.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }});

        if (typeof window._turnstileTokenCallback === 'function') {{
            window._turnstileTokenCallback(token);
        }}
        if (typeof window.turnstileCallback === 'function') {{
            window.turnstileCallback(token);
        }}
        return true;
    }})();
    """
    return page.evaluate(script)

def register_with_browser_solver(email, password):
    """使用浏览器 + Solver 注册"""
    print(f"🌐 使用浏览器模式注册: {email}")

    try:
        with Camoufox(headless=REGISTER_HEADLESS) as browser:
            page = browser.new_page()

            # 1. 访问 Tavily 统一登录/注册入口
            page.goto("https://app.tavily.com/sign-in", wait_until="networkidle", timeout=30000)
            time.sleep(2)

            signup_url = extract_signup_url(page.content())
            if signup_url:
                print("🧭 进入注册页...")
                page.goto(signup_url, wait_until="networkidle", timeout=30000)
                time.sleep(2)
            elif has_unified_identifier_entry(page):
                print("ℹ️  检测到 Tavily 新版登录/注册合一入口，直接复用当前页面...")
            else:
                print(f"❌ 未找到注册入口: {page.url}")
                return None

            email_selector = fill_first_input(page, ['input[name="email"]', 'input[name="username"]'], email)
            if not email_selector:
                print("❌ 注册页未找到邮箱输入框")
                return None

            # 2. 注册页 Turnstile
            print("🔐 处理注册页 Turnstile...")
            token1 = solve_turnstile(page.url, sitekey=get_turnstile_sitekey(page))
            if not token1:
                print("❌ Token 获取失败")
                return None
            print(f"✅ Token: {token1[:50]}...")

            if inject_turnstile_token(page, token1):
                print("✅ Token 已注入")
            else:
                print("⚠️  未找到 captcha 输入框")

            # 3. 提交邮箱，进入验证码页
            submit_primary_action(page, email_selector)
            time.sleep(6)

            try:
                page.wait_for_selector('input[name="code"], input[name="password"]', timeout=15000)
            except:
                print("⚠️  首次提交后未跳转，重试点击 Continue...")
                submit_primary_action(page)
                time.sleep(3)

                try:
                    page.wait_for_selector('input[name="code"], input[name="password"]', timeout=20000)
                except:
                    feedback = extract_page_feedback(page)
                    print(f"❌ 未进入验证码/密码页面: {page.url}")
                    if feedback:
                        print(f"   页面提示: {feedback}")
                        print_feedback_hint(feedback)
                    return None

            if page.query_selector('input[name="code"]'):
                print("✅ 到达邮箱验证码页")
                code = get_email_code(email, timeout=EMAIL_CODE_TIMEOUT)
                if not code:
                    return None

                page.fill('input[name="code"]', code)
                submit_primary_action(page, 'input[name="code"]')
                time.sleep(3)

            # 4. 进入密码页
            try:
                page.wait_for_selector('input[name="password"]', timeout=30000)
                print("✅ 到达注册密码页")
            except:
                print(f"❌ 未到达注册密码页: {page.url}")
                return None

            # 5. 设置密码
            if not submit_password_with_recovery(page, password):
                feedback = extract_page_feedback(page)
                print(f"❌ 登录失败: {page.url}")
                if feedback:
                    print(f"   页面提示: {feedback}")
                    print_feedback_hint(feedback)
                return None

            print("✅ 登录成功")

            # 7. 检查是否还需要额外验证
            time.sleep(3)
            if 'verify' in page.url.lower():
                print("📧 需要邮件验证")
                verify_url = get_verification_link(email, timeout=60)
                if not verify_url:
                    return None

                page.goto(verify_url, wait_until="networkidle", timeout=60000)
                page.wait_for_url("**/app.tavily.com/**", timeout=60000)
                time.sleep(3)

            # 8. 获取 API Key
            print("🔑 获取 API Key...")
            time.sleep(3)
            api_key = wait_for_api_key(page, timeout=API_KEY_TIMEOUT)
            if not api_key:
                print("⚠️  未找到 API Key")
                return None

            print("🧪 验证 API Key 可用性...")
            if not verify_api_key(api_key):
                return None

            save_account(email, password, api_key)

            print(f"🎉 注册成功")
            print(f"   邮箱: {email}")
            print(f"   密码: {password}")
            print(f"   Key : {api_key}")
            return api_key

    except Exception as e:
        print(f"❌ 注册失败: {e}")
        return None

if __name__ == "__main__":
    from tavily_core import create_email
    email, password = create_email()
    register_with_browser_solver(email, password)

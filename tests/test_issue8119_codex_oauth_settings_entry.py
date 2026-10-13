"""Codex OAuth login reachable from Settings -> Providers, not only the first-run wizard (#8119)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ONBOARDING_JS = (ROOT / "static" / "onboarding.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _codex_flow_block() -> str:
    start = ONBOARDING_JS.index("/* ── Codex OAuth device-code flow ── */")
    end = ONBOARDING_JS.index("/* ── Anthropic / Claude Code credential-link flow ── */")
    return ONBOARDING_JS[start:end]


_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2]);
const src = fs.readFileSync(process.argv[3], 'utf8');

class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {};
    this.className = ''; this.textContent = ''; this.innerHTML = ''; this.disabled = false;
    this.listeners = {};
    this.classList = { toggle() {} };
  }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
}
function walk(el, out = []) { out.push(el); el.children.forEach((c) => walk(c, out)); return out; }

globalThis.document = { createElement: (tag) => new El(tag) };
globalThis.t = (key, ...args) => (args.length ? key + ':' + args.join(',') : key);
globalThis.esc = (s) => String(s);
globalThis.jsArg = (s) => JSON.stringify(s);
globalThis.showToast = () => {};
globalThis.S = { activeProfile: scenario.activeProfile || 'default' };
const wizardFlow = new El('div');
const wizardBtn = new El('button');
globalThis.$ = (id) => (id === 'codexOAuthFlow' ? wizardFlow : id === 'codexOAuthBtn' ? wizardBtn : null);

const calls = [];
let pollStatus = 'pending';
const startResponse = Object.assign({
  flow_id: 'f1', user_code: 'ABCD-1234', verification_uri: 'https://auth.openai.com/codex/device', poll_interval_seconds: 1,
}, scenario.startExtra || {});
globalThis.api = async (url, opts) => {
  calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (url === '/api/onboarding/oauth/start') return startResponse;
  if (url.startsWith('/api/onboarding/oauth/poll')) return { status: pollStatus };
  return { ok: true };
};
const timers = [];
globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
globalThis.clearTimeout = () => {};
let providersReloads = 0;
let wizardReloads = 0;
globalThis.loadProvidersPanel = async () => { providersReloads += 1; };
let modelRefreshes = 0;
globalThis._refreshModelDropdownsAfterProviderChange = () => { modelRefreshes += 1; };
globalThis.loadOnboardingWizard = async () => { wizardReloads += 1; };

eval(src);

(async () => {
  const result = { cards: {} };
  for (const p of scenario.providers) {
    const card = _buildProviderCard(p);
    const login = walk(card).find((el) => el.dataset.codexOauthLogin === '1');
    result.cards[p.id] = login
      ? {
          login: true,
          label: walk(login).find((el) => el.tag === 'button').textContent,
          note: walk(login).find((el) => el.className === 'provider-card-hint').textContent,
        }
      : { login: false };
  }

  if (scenario.clickCodex) {
    const card = _buildProviderCard(scenario.providers.find((p) => p.id === 'openai-codex'));
    const login = walk(card).find((el) => el.dataset.codexOauthLogin === '1');
    const btn = walk(login).find((el) => el.tag === 'button');
    const note = walk(login).find((el) => el.className === 'provider-card-hint');
    if (scenario.profileAtClick) S.activeProfile = scenario.profileAtClick;
    await btn.listeners.click();
    const flow = login.children[login.children.length - 1];
    result.noteAtClick = note.textContent;
    result.flowShowsCode = flow.innerHTML.includes('ABCD-1234');
    result.wizardFlowTouched = wizardFlow.innerHTML !== '';
    result.btnDisabledWhilePending = btn.disabled;
    if (scenario.cancel) {
      await cancelCodexOAuth();
      result.pendingTimersAfterCancel = timers.length;
      // A poll tick already scheduled before cancel must not resume the flow.
      while (timers.length) await timers.shift()();
      result.flowShowsCancelled = flow.innerHTML.includes('cancelled');
    } else {
      pollStatus = scenario.pollStatus || 'success';
      await timers.shift()();
      result.flowHtml = flow.innerHTML;
    }
    result.btnLabelAfter = btn.textContent;
    result.btnDisabledAfter = btn.disabled;
    result.domText = walk(card).map((el) => el.innerHTML + '|' + el.textContent).join('\n');
  }

  if (scenario.race) {
    // Flow f1 (profile "default") has a poll in flight when the user switches to
    // "work" and starts f2 from the rebuilt card; then f1's poll answers late.
    // With sameFlow, the restart happens in the same profile and the server's
    // single-flight hands back the same pending flow (f1) instead of a new one.
    let starts = 0;
    let releaseF1 = null;
    let f1Polls = 0;
    globalThis.api = async (url, opts) => {
      calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
      if (url === '/api/onboarding/oauth/start') {
        starts += 1;
        if (scenario.sameFlow || starts === 1) return Object.assign({}, startResponse, { flow_id: 'f1', user_code: 'CODE-ONE' });
        return Object.assign({}, startResponse, { flow_id: 'f2', user_code: 'CODE-TWO' });
      }
      if (url === '/api/onboarding/oauth/poll?flow_id=f1') {
        f1Polls += 1;
        if (f1Polls > 1) return { status: 'success' };
        return new Promise((resolve) => { releaseF1 = () => resolve({ status: scenario.lateStatus }); });
      }
      if (url === '/api/onboarding/oauth/poll?flow_id=f2') return { status: 'success' };
      return { ok: true };
    };
    const codex = scenario.providers.find((p) => p.id === 'openai-codex');
    const open = async () => {
      const card = _buildProviderCard(codex);
      const login = walk(card).find((el) => el.dataset.codexOauthLogin === '1');
      await walk(login).find((el) => el.tag === 'button').listeners.click();
      return login.children[login.children.length - 1];
    };
    await open();
    const f1Poll = timers.shift()();  // f1 poll now awaiting the server
    if (!scenario.sameFlow) S.activeProfile = 'work';
    const flowB = await open();
    releaseF1();
    await f1Poll;
    result.flowBAfterLate = flowB.innerHTML;
    result.reloadsAfterLate = providersReloads;
    result.timersAfterLate = timers.length;
    while (timers.length) await timers.shift()();
    result.flowBFinal = flowB.innerHTML;
  }

  if (scenario.cancelRace) {
    // Cancel f1 from card A; while the cancel request is in flight the panel is
    // rebuilt and f2 is started from card B; then the cancel response arrives.
    let starts = 0;
    let releaseCancel = null;
    globalThis.api = async (url, opts) => {
      calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
      if (url === '/api/onboarding/oauth/start') {
        starts += 1;
        return Object.assign({}, startResponse, starts === 1
          ? { flow_id: 'f1', user_code: 'CODE-ONE' }
          : { flow_id: 'f2', user_code: 'CODE-TWO' });
      }
      if (url === '/api/onboarding/oauth/cancel') {
        return new Promise((resolve) => { releaseCancel = () => resolve({ ok: true, status: 'cancelled' }); });
      }
      return { status: 'pending' };
    };
    const codex = scenario.providers.find((p) => p.id === 'openai-codex');
    const open = async () => {
      const card = _buildProviderCard(codex);
      const login = walk(card).find((el) => el.dataset.codexOauthLogin === '1');
      const btn = walk(login).find((el) => el.tag === 'button');
      await btn.listeners.click();
      return { btn, flow: login.children[login.children.length - 1] };
    };
    await open();
    const cancelling = cancelCodexOAuth();
    const b = await open();
    releaseCancel();
    await cancelling;
    result.newBtnDisabled = b.btn.disabled;
    result.newFlowHtml = b.flow.innerHTML;
  }

  if (scenario.wizard) {
    await startCodexOAuth();
    result.wizardShowsCode = wizardFlow.innerHTML.includes('ABCD-1234');
    pollStatus = 'success';
    await timers.shift()();
  }

  result.calls = calls;
  result.modelRefreshes = modelRefreshes;
  result.providersReloads = providersReloads;
  result.wizardReloads = wizardReloads;
  process.stdout.write(JSON.stringify(result));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(tmp_path, scenario):
    if NODE is None:
        pytest.skip("node is required to execute the provider card harness")
    src = "\n".join([
        _codex_flow_block(),
        extract_function(PANELS_JS, "_buildCodexOAuthLogin", prefix="function"),
        extract_function(PANELS_JS, "_buildProviderCard", prefix="function"),
    ])
    src_path = tmp_path / "src.js"
    src_path.write_text(src, encoding="utf-8")
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(_DRIVER, encoding="utf-8")
    out = subprocess.run(
        [NODE, str(driver_path), json.dumps(scenario), str(src_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _oauth_provider(pid, **extra):
    base = {"id": pid, "display_name": pid, "is_oauth": True, "has_key": False, "key_source": "none", "models": []}
    base.update(extra)
    return base


def test_codex_card_offers_login_without_credentials_and_reconnect_with_them(tmp_path):
    # The no-credential state renders through the auth_error branch of the card.
    result = _run(tmp_path, {"providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")]})
    assert result["cards"]["openai-codex"] == {
        "login": True,
        "label": "oauth_login_codex",
        "note": "providers_codex_profile_note:default",
    }

    result = _run(tmp_path, {
        "activeProfile": "work",
        "providers": [_oauth_provider("openai-codex", has_key=True, key_source="oauth")],
    })
    assert result["cards"]["openai-codex"] == {
        "login": True,
        "label": "providers_codex_reconnect",
        "note": "providers_codex_profile_note:work",
    }


def test_login_action_is_codex_only_and_skips_config_yaml_tokens(tmp_path):
    result = _run(tmp_path, {
        "providers": [
            _oauth_provider("copilot"),
            _oauth_provider("nous"),
            _oauth_provider("anthropic"),
            _oauth_provider("openai-codex", has_key=True, key_source="config_yaml"),
        ],
    })
    assert result["cards"] == {
        "copilot": {"login": False},
        "nous": {"login": False},
        "anthropic": {"login": False},
        "openai-codex": {"login": False},
    }


def test_settings_login_runs_device_flow_and_refreshes_providers_not_wizard(tmp_path):
    result = _run(tmp_path, {
        "clickCodex": True,
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    assert result["calls"][0] == {"url": "/api/onboarding/oauth/start", "body": {"provider": "openai-codex"}}
    assert result["calls"][1]["url"] == "/api/onboarding/oauth/poll?flow_id=f1"
    assert result["flowShowsCode"] is True
    assert result["wizardFlowTouched"] is False
    assert "oauth_codex_success" in result["flowHtml"]
    assert result["providersReloads"] == 1
    assert result["modelRefreshes"] == 1
    assert result["wizardReloads"] == 0


def test_wizard_login_still_renders_in_wizard_and_reloads_it(tmp_path):
    result = _run(tmp_path, {"wizard": True, "providers": []})
    assert result["wizardShowsCode"] is True
    assert result["wizardReloads"] == 1
    assert result["providersReloads"] == 0
    assert result["modelRefreshes"] == 0


def test_profile_note_is_refreshed_when_the_flow_starts(tmp_path):
    # The card was built while "default" was active; the user switched to "work"
    # before clicking. The note must name the profile the flow is bound to.
    result = _run(tmp_path, {
        "clickCodex": True,
        "profileAtClick": "work",
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    assert result["cards"]["openai-codex"]["note"] == "providers_codex_profile_note:default"
    assert result["noteAtClick"] == "providers_codex_profile_note:work"


def test_cancel_from_settings_cancels_server_flow_and_stops_polling(tmp_path):
    result = _run(tmp_path, {
        "clickCodex": True,
        "cancel": True,
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    assert result["btnDisabledWhilePending"] is True
    urls = [c["url"] for c in result["calls"]]
    assert urls[0] == "/api/onboarding/oauth/start"
    assert {"url": "/api/onboarding/oauth/cancel", "body": {"flow_id": "f1"}} in result["calls"]
    # The poll tick scheduled by start ran after cancel and must not have polled.
    assert not any(u.startswith("/api/onboarding/oauth/poll") for u in urls)
    assert result["flowShowsCancelled"] is True
    assert result["btnDisabledAfter"] is False
    assert result["btnLabelAfter"] == "oauth_login_codex"
    assert result["providersReloads"] == 0
    assert result["wizardReloads"] == 0


@pytest.mark.parametrize("status,marker", [("expired", "oauth_codex_expired"), ("error", "oauth_codex_error")])
def test_expired_or_failed_flow_re_enables_the_button_without_refreshing(tmp_path, status, marker):
    result = _run(tmp_path, {
        "clickCodex": True,
        "pollStatus": status,
        "providers": [_oauth_provider("openai-codex", has_key=True, key_source="oauth")],
    })
    assert marker in result["flowHtml"]
    assert result["btnDisabledAfter"] is False
    assert result["btnLabelAfter"] == "providers_codex_reconnect"
    assert result["providersReloads"] == 0
    assert result["wizardReloads"] == 0


def test_settings_flow_never_renders_or_sends_fields_beyond_the_public_payload(tmp_path):
    # Even if a start response carried provider-owned secrets, the card must only
    # use flow_id / user_code / verification_uri and poll by flow_id alone.
    result = _run(tmp_path, {
        "clickCodex": True,
        "startExtra": {"device_auth_id": "DEVICE-SECRET", "code_verifier": "VERIFIER-SECRET"},
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    assert "DEVICE-SECRET" not in result["domText"]
    assert "VERIFIER-SECRET" not in result["domText"]
    for call in result["calls"]:
        assert "SECRET" not in call["url"]
        assert "SECRET" not in json.dumps(call["body"])
    assert result["calls"][1]["url"] == "/api/onboarding/oauth/poll?flow_id=f1"


def test_flow_started_under_one_profile_persists_there_after_a_profile_switch(monkeypatch, tmp_path):
    """The server binds the flow to the profile active at start; switching profiles
    while the user authorizes must not redirect the credential."""
    import threading

    import api.oauth as oauth

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    oauth._OAUTH_FLOWS.clear()

    active = {"home": home_a}
    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: active["home"])
    monkeypatch.setattr(oauth, "_request_codex_user_code", lambda: {
        "user_code": "ABCD-1234", "device_auth_id": "device-secret", "interval": 1, "expires_in": 600,
    })
    monkeypatch.setattr(oauth, "_spawn_codex_oauth_worker", lambda flow_id: None)

    authorized = threading.Event()

    def _poll(device_auth_id, user_code):
        assert authorized.wait(timeout=5)
        return {"authorization_code": "auth-code", "code_verifier": "verifier"}

    monkeypatch.setattr(oauth, "_poll_codex_authorization", _poll)
    monkeypatch.setattr(oauth, "_exchange_codex_authorization", lambda code, verifier: {
        "access_token": "ACCESS", "refresh_token": "REFRESH",
    })
    monkeypatch.setattr(oauth.time, "sleep", lambda _s: None)

    payload = oauth.start_onboarding_oauth_flow({"provider": "openai-codex"})
    flow_id = payload["flow_id"]

    worker = threading.Thread(target=oauth._run_codex_oauth_worker, args=(flow_id,), daemon=True)
    worker.start()
    active["home"] = home_b  # user switches profile while the code is being entered
    authorized.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert oauth.poll_onboarding_oauth_flow(flow_id)["status"] == "success"
    assert (home_a / "auth.json").exists()
    assert not (home_b / "auth.json").exists()
    stored = json.loads((home_a / "auth.json").read_text(encoding="utf-8"))
    assert stored["credential_pool"]["openai-codex"][0]["access_token"] == "ACCESS"


@pytest.mark.parametrize("late_status", ["success", "pending", "expired"])
def test_late_poll_from_an_abandoned_flow_does_not_touch_the_new_one(tmp_path, late_status):
    result = _run(tmp_path, {
        "race": True,
        "lateStatus": late_status,
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    # The new flow (f2) still shows its own code and nothing reloaded.
    assert "CODE-TWO" in result["flowBAfterLate"]
    assert result["reloadsAfterLate"] == 0
    # Only f2's poll tick is scheduled: no orphan timer from f1.
    assert result["timersAfterLate"] == 1
    # f2 keeps polling and completes on its own.
    assert "oauth_codex_success" in result["flowBFinal"]
    assert result["providersReloads"] == 1
    polled = [c["url"] for c in result["calls"] if "/oauth/poll" in c["url"]]
    assert polled == ["/api/onboarding/oauth/poll?flow_id=f1", "/api/onboarding/oauth/poll?flow_id=f2"]


def test_restart_in_the_same_profile_keeps_a_single_poll_loop(tmp_path):
    # Single-flight returns the same pending flow id on restart, so a late
    # "pending" from the earlier poll must not schedule a second loop.
    result = _run(tmp_path, {
        "race": True,
        "sameFlow": True,
        "lateStatus": "pending",
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    assert "CODE-ONE" in result["flowBAfterLate"]
    assert result["timersAfterLate"] == 1
    assert "oauth_codex_success" in result["flowBFinal"]
    assert result["providersReloads"] == 1


def test_late_cancel_response_does_not_re_enable_a_newer_flow(tmp_path):
    result = _run(tmp_path, {
        "cancelRace": True,
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    assert "CODE-TWO" in result["newFlowHtml"]
    assert "cancelled" not in result["newFlowHtml"]
    assert result["newBtnDisabled"] is True


# ── Persistence safety once Reconnect sits in front of existing credentials ──

def _install_fake_agent_auth_lock(monkeypatch):
    """Stand-in for hermes_cli.auth._auth_store_lock: one real mutex per auth.json path."""
    import contextlib
    import sys
    import threading
    import types

    locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()
    targets: list[Path] = []
    timeouts: list[float] = []

    @contextlib.contextmanager
    def _auth_store_lock(timeout_seconds=15.0, *, target_path=None):
        targets.append(Path(target_path))
        timeouts.append(timeout_seconds)
        with guard:
            lock = locks.setdefault(str(target_path), threading.Lock())
        with lock:
            yield

    fake = types.ModuleType("hermes_cli.auth")
    fake._auth_store_lock = _auth_store_lock
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake)
    return _auth_store_lock, targets, timeouts


def _cli_entry(entry_id: str, access: str, refresh: str, priority: int) -> dict:
    # Shape written by `hermes auth add openai-codex` (6-hex id, same source as WebUI).
    return {
        "id": entry_id, "label": entry_id, "auth_type": "oauth", "priority": priority,
        "source": "manual:device_code", "access_token": access, "refresh_token": refresh,
        "base_url": "https://chatgpt.com/backend-api/codex",
    }


def test_settings_login_does_not_overwrite_a_refresh_committed_under_the_agent_lock(monkeypatch, tmp_path):
    """The Agent refreshes a CLI account between WebUI's read and write: both survive."""
    import threading

    import api.oauth as oauth

    agent_lock, targets, timeouts = _install_fake_agent_auth_lock(monkeypatch)
    auth_path = tmp_path / "auth.json"
    own = {"id": "codex-oauth-0123456789ab", "source": "manual:device_code", "auth_type": "oauth",
           "priority": 0, "access_token": "OWN-OLD", "refresh_token": "OWN-OLD"}
    auth_path.write_text(json.dumps({
        "version": 1,
        "credential_pool": {"openai-codex": [own, _cli_entry("a1b2c3", "OLD-ACCESS", "OLD-REFRESH", 1)]},
    }), encoding="utf-8")

    def _agent_refresh():
        with agent_lock(target_path=auth_path):
            store = json.loads(auth_path.read_text(encoding="utf-8"))
            cli = next(e for e in store["credential_pool"]["openai-codex"] if e["id"] == "a1b2c3")
            cli.update({"access_token": "NEW-ACCESS", "refresh_token": "NEW-REFRESH"})
            auth_path.write_text(json.dumps(store), encoding="utf-8")
        refreshed.set()

    refreshed = threading.Event()
    refresher = threading.Thread(target=_agent_refresh, daemon=True)
    real_read = oauth._read_auth_json

    locked_at_read: list[Path] = []

    def _read_then_race(path=None):
        locked_at_read.extend(targets)
        data = real_read(path)
        refresher.start()
        # Unlocked, the refresh lands before WebUI writes; locked, it waits for WebUI.
        refreshed.wait(timeout=1)
        return data

    monkeypatch.setattr(oauth, "_read_auth_json", _read_then_race)
    oauth._persist_codex_credentials(tmp_path, {"access_token": "WEBUI-ACCESS", "refresh_token": "WEBUI-REFRESH"})
    refresher.join(timeout=5)
    assert not refresher.is_alive()

    entries = json.loads(auth_path.read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]
    by_id = {e["id"]: e for e in entries}
    assert by_id["a1b2c3"]["access_token"] == "NEW-ACCESS"
    assert by_id["a1b2c3"]["refresh_token"] == "NEW-REFRESH"
    assert by_id["codex-oauth-0123456789ab"]["access_token"] == "WEBUI-ACCESS"
    assert len(entries) == 2
    assert locked_at_read == [auth_path]  # WebUI held this profile's lock before reading
    # Outwaits the Agent's own Codex refresh (20 s + 5 s), not the 15 s default.
    assert timeouts[0] == 25.0  # [1] is the simulated Agent refresh


def test_reconnect_leaves_cli_codex_accounts_alone_and_reuses_its_own_entry(monkeypatch, tmp_path):
    import api.oauth as oauth

    _install_fake_agent_auth_lock(monkeypatch)
    auth_path = tmp_path / "auth.json"
    native = [_cli_entry("a1b2c3", "A-ACCESS", "A-REFRESH", 0), _cli_entry("d4e5f6", "B-ACCESS", "B-REFRESH", 1)]
    auth_path.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": native}}), encoding="utf-8")

    oauth._persist_codex_credentials(tmp_path, {"access_token": "W1", "refresh_token": "R1"})
    entries = json.loads(auth_path.read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]
    assert entries[:2] == native  # both CLI accounts untouched, priorities included
    assert len(entries) == 3
    own = entries[2]
    assert own["id"].startswith("codex-oauth-")
    assert own["priority"] == 2
    assert own["access_token"] == "W1"

    # A second Reconnect updates WebUI's own entry instead of adding another.
    oauth._persist_codex_credentials(tmp_path, {"access_token": "W2", "refresh_token": "R2"})
    entries = json.loads(auth_path.read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]
    assert entries[:2] == native
    assert len(entries) == 3
    assert entries[2]["id"] == own["id"]
    assert entries[2]["access_token"] == "W2"


def test_reconnect_reuses_a_legacy_oauth_device_entry_from_older_webui(monkeypatch, tmp_path):
    import api.oauth as oauth

    _install_fake_agent_auth_lock(monkeypatch)
    auth_path = tmp_path / "auth.json"
    legacy = {"id": "codex-oauth-1a2b3c4d", "source": "oauth_device", "auth_type": "oauth",
              "priority": 0, "access_token": "OLD", "refresh_token": "OLD"}
    auth_path.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": [legacy]}}), encoding="utf-8")

    oauth._persist_codex_credentials(tmp_path, {"access_token": "NEW", "refresh_token": "NEW-R"})
    entries = json.loads(auth_path.read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]
    assert len(entries) == 1
    assert entries[0]["id"] == "codex-oauth-1a2b3c4d"
    assert entries[0]["source"] == "manual:device_code"
    assert entries[0]["access_token"] == "NEW"


def test_persist_still_works_without_an_importable_agent_lock(monkeypatch, tmp_path):
    import sys
    import types

    import api.oauth as oauth

    monkeypatch.setitem(sys.modules, "hermes_cli.auth", types.ModuleType("hermes_cli.auth"))
    path = oauth._persist_codex_credentials(tmp_path, {"access_token": "X", "refresh_token": "Y"})
    entry = json.loads(path.read_text(encoding="utf-8"))["credential_pool"]["openai-codex"][0]
    assert entry["access_token"] == "X"
    assert entry["priority"] == 0


def test_cancel_during_the_wait_for_the_agent_lock_wins(monkeypatch, tmp_path):
    """A cancel accepted while WebUI waits for the Agent auth lock must stay
    cancelled, and the aborted credential must not be written."""
    import threading
    import time as real_time

    import api.oauth as oauth

    agent_lock, targets, _timeouts = _install_fake_agent_auth_lock(monkeypatch)
    auth_path = tmp_path / "auth.json"
    cli = _cli_entry("a1b2c3", "CLI-ACCESS", "CLI-REFRESH", 0)
    auth_path.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": [cli]}}), encoding="utf-8")
    oauth._OAUTH_FLOWS.clear()
    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(oauth, "_request_codex_user_code", lambda: {
        "user_code": "ABCD-1234", "device_auth_id": "device-secret", "interval": 1, "expires_in": 600,
    })
    monkeypatch.setattr(oauth, "_spawn_codex_oauth_worker", lambda flow_id: None)
    monkeypatch.setattr(oauth, "_poll_codex_authorization", lambda device_auth_id, user_code: {
        "authorization_code": "auth-code", "code_verifier": "verifier",
    })
    monkeypatch.setattr(oauth, "_exchange_codex_authorization", lambda code, verifier: {
        "access_token": "ABORTED-ACCESS", "refresh_token": "ABORTED-REFRESH",
    })
    monkeypatch.setattr(oauth.time, "sleep", lambda _s: None)

    flow_id = oauth.start_onboarding_oauth_flow({"provider": "openai-codex"})["flow_id"]
    worker = threading.Thread(target=oauth._run_codex_oauth_worker, args=(flow_id,), daemon=True)
    with agent_lock(target_path=auth_path):  # the Agent is mid-refresh
        worker.start()
        deadline = real_time.monotonic() + 5
        while len(targets) < 2 and real_time.monotonic() < deadline:
            real_time.sleep(0.01)
        assert len(targets) == 2  # WebUI is now waiting for the Agent lock
        assert oauth.cancel_onboarding_oauth_flow({"flow_id": flow_id})["status"] == "cancelled"
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert oauth.poll_onboarding_oauth_flow(flow_id)["status"] == "cancelled"
    entries = json.loads(auth_path.read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]
    assert entries == [cli]
